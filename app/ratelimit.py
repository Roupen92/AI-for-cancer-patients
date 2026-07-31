"""Per-client rate limiting for the endpoints that spend money.

Every chat turn costs real LLM and search credits, and the app has no accounts,
so an unauthenticated URL is an open tap. The limits here exist to stop a script
draining the balance — because when credits run out the failure is silent and
ugly: search dies, every specialist retrieves nothing, and real patients get
"I couldn't find anything" for every question they ask.

DELIBERATELY GENEROUS, and here is why. Rate limiting a health tool by IP is
blunter than it looks: a hospital, a clinic waiting room, a library, a care home
or a university can put hundreds of genuine patients behind one address. Limits
that would be normal for a SaaS app would silently lock out an entire ward. The
defaults are set well above what any one person would ever type, and every one
is overridable, so the failure mode is "a bot gets throttled" rather than "a
waiting room gets blocked".

In-memory and per-process, matching the rest of the app's state. That is enough
for the single-instance deployment this runs on; a multi-instance setup would
need shared storage to be exact.
"""
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Per-IP allowances. A determined patient might ask a dozen questions in a sitting;
# these sit above that and far below what a script would do.
TURNS_PER_HOUR = _int_env("CANCERPATIENT_RATE_PER_HOUR", 20)
TURNS_PER_DAY = _int_env("CANCERPATIENT_RATE_PER_DAY", 60)

HOUR = 3600
DAY = 86400

# 0 disables limiting entirely (useful for local development and load testing).
DISABLED = TURNS_PER_HOUR <= 0 and TURNS_PER_DAY <= 0


@dataclass
class _Bucket:
    hits: deque = field(default_factory=deque)


class RateLimiter:
    """Sliding-window counter keyed by client. Thread-safe; never raises."""

    def __init__(self, per_hour: int = TURNS_PER_HOUR, per_day: int = TURNS_PER_DAY):
        self.per_hour = per_hour
        self.per_day = per_day
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._last_sweep = time.time()

    def _sweep(self, now: float) -> None:
        """Drop clients with no activity in the last day so memory stays bounded."""
        if now - self._last_sweep < 600:
            return
        self._last_sweep = now
        stale = [
            key
            for key, b in self._buckets.items()
            if not b.hits or now - b.hits[-1] > DAY
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def check(self, key: str) -> tuple[bool, int, str]:
        """Record an attempt. Returns (allowed, retry_after_seconds, reason)."""
        if self.per_hour <= 0 and self.per_day <= 0:
            return True, 0, ""

        now = time.time()
        with self._lock:
            self._sweep(now)
            bucket = self._buckets.setdefault(key, _Bucket())
            hits = bucket.hits

            while hits and now - hits[0] > DAY:
                hits.popleft()

            in_hour = sum(1 for t in hits if now - t <= HOUR)

            if self.per_hour > 0 and in_hour >= self.per_hour:
                oldest_in_hour = next(t for t in hits if now - t <= HOUR)
                return False, max(1, int(HOUR - (now - oldest_in_hour))), "hour"

            if self.per_day > 0 and len(hits) >= self.per_day:
                return False, max(1, int(DAY - (now - hits[0]))), "day"

            hits.append(now)
            return True, 0, ""

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


def client_key(request) -> str:
    """Identify the caller behind Railway's proxy.

    X-Forwarded-For is a client-controlled header, so it is only trustworthy
    behind a proxy that overwrites it — which is the deployed setup. The FIRST
    entry is the original client; later entries are the proxy chain.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


# One shared limiter for the whole process.
limiter = RateLimiter()


def friendly_message(retry_after: int, reason: str) -> str:
    """Patient-facing text. Someone who is ill and worried should not be made to
    feel they did something wrong by asking too many questions."""
    if reason == "day":
        return (
            "You've asked a lot of questions today — more than this free service can "
            "research. Please come back tomorrow. If something feels urgent, contact "
            "your care team rather than waiting."
        )
    minutes = max(1, round(retry_after / 60))
    return (
        f"We need a short break to keep up — please try again in about {minutes} "
        f"minute{'s' if minutes != 1 else ''}. If something feels urgent, contact "
        "your care team rather than waiting."
    )
