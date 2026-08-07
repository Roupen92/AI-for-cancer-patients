"""Thin LLM wrapper used by every model call in the tumor board.

Every provider (OpenRouter, Google's Gemini endpoint, OpenAI) is reached through
its OpenAI-compatible API, so the rest of the codebase keeps using the familiar
OpenAI SDK shape (tool calls, response_format, etc.) — only the base URL and API
key change. Set CANCERPATIENT_PROVIDER / MEDBOARD_PROVIDER to pick one explicitly.
"""
import json
import os
import re
import time
import logging
from typing import Any

from openai import (
    OpenAI,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    APIStatusError,
)

from app.config import MODEL_NAME, PROVIDER
from app.logsafe import scrub

# Sentinel exception so callers (specialist.py / board.py) can render a clean
# user-facing message instead of dumping the raw OpenAI JSON.
class QuotaExceeded(Exception):
    """Raised when the LLM provider returns a rate-limit error we couldn't retry past."""

log = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_client: OpenAI | None = None

# Per-request ceiling, and the reason it exists.
#
# Nothing here passed a timeout, so every call inherited the OpenAI SDK's default
# 600s read timeout — and the SDK's own `max_retries=2` MULTIPLIES the retry loop
# in chat() below, so one struggling provider could hold a single pass for
# 600s x 3 x 5. Observed live: a turn sat in the plain-language pass for over
# fifteen minutes with the browser showing a cheerful progress bar the whole time.
# For a patient waiting on a health answer that is indistinguishable from a hang.
#
# 240s is comfortably above the slowest legitimate call observed (a 152s self-check
# at high reasoning effort) and far below 600s. SDK retries are turned off because
# chat() already retries with backoff and reports what it is doing; two retry
# layers multiplying each other is how the worst case got to hours.
_REQUEST_TIMEOUT = float(
    os.getenv("CANCERPATIENT_LLM_TIMEOUT") or os.getenv("MEDBOARD_LLM_TIMEOUT") or 240.0
)
# Timeouts get their own, much smaller retry budget than connection errors — see
# the handler in chat(). Worst case per call is now bounded at roughly
# _REQUEST_TIMEOUT * _MAX_TIMEOUT_ATTEMPTS instead of being open-ended.
_MAX_TIMEOUT_ATTEMPTS = 2


def get_client() -> OpenAI:
    """Return a singleton client for the provider resolved in config (explicit
    *_PROVIDER, else inferred from whichever API key is present)."""
    global _client
    if _client is not None:
        return _client

    provider = PROVIDER
    if provider == "openrouter":
        # Accept OPEN_ROUTER too — that's the name the key was first saved under.
        api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Paste your key from "
                "https://openrouter.ai/keys into .env and restart."
            )
        _client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL,
                         timeout=_REQUEST_TIMEOUT, max_retries=0)
        log.info("LLM client: OpenRouter, model=%s", MODEL_NAME)
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Paste your key into .env and restart "
                "(or unset CANCERPATIENT_PROVIDER to use Gemini)."
            )
        _client = OpenAI(api_key=api_key, timeout=_REQUEST_TIMEOUT, max_retries=0)
        log.info("LLM client: OpenAI, model=%s", MODEL_NAME)
    else:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Paste your Google AI Studio key into .env "
                "and restart (or set MEDBOARD_PROVIDER=openai to use OpenAI)."
            )
        _client = OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL,
                         timeout=_REQUEST_TIMEOUT, max_retries=0)
        log.info("LLM client: Gemini via OpenAI-compat endpoint, model=%s", MODEL_NAME)

    return _client


def _provider_privacy_enabled() -> bool:
    """Whether to restrict OpenRouter routing to zero-retention providers.

    Defaults to ON. A privacy control that has to be remembered is not a control,
    and the account-level equivalent lives in a dashboard nobody reviews — setting
    it per request keeps it visible in the code and in review.
    """
    raw = os.getenv("CANCERPATIENT_PROVIDER_PRIVACY")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


_RETRY_DELAY_RE = re.compile(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+(?:\.\d+)?)s['\"]")


def _parse_retry_delay(err: Exception) -> float | None:
    """Try to extract Google's suggested retryDelay (seconds) from an error message."""
    m = _RETRY_DELAY_RE.search(str(err))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_retries: int = 5,
) -> Any:
    """Call the configured LLM with retry on transient errors.

    On rate-limit errors, prefer the provider's suggested retryDelay over
    blind exponential backoff (Google's Gemini API includes this in 429s).
    """
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model or MODEL_NAME,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if response_format:
        kwargs["response_format"] = response_format

    # OpenRouter checks affordability against max_tokens before routing, and an
    # unset max_tokens counts as the model's FULL output budget (65k for GLM 5.2)
    # — which 402s unless the credit balance covers all of it. Cap it. The cap
    # includes reasoning tokens, so keep it comfortably above the effort level.
    if PROVIDER == "openrouter":
        kwargs["max_tokens"] = int(
            os.getenv("CANCERPATIENT_MAX_TOKENS")
            or os.getenv("MEDBOARD_MAX_TOKENS")
            or 16384
        )

        # Pin routing to providers that neither retain nor train on the request.
        #
        # OpenRouter does not log content itself, but by default it will route to
        # whichever endpoint is cheapest/fastest — and for an open-weights model
        # that pool includes providers which retain prompts (for GLM 5.2: 11 of 34
        # endpoints, among them datacenters in jurisdictions a patient never chose).
        # The text being routed is someone describing their diagnosis, so the
        # promise on the privacy page has to hold for the upstream hop too, not
        # just for our own logs.
        #
        #   zdr             — only endpoints with zero data retention
        #   data_collection — refuse providers that store data non-transiently
        #
        # These cost nothing here: the cheapest ZDR endpoint is the price we were
        # already paying. They do shrink the fallback pool, so a provider outage
        # is slightly more visible — the right trade for health data.
        if _provider_privacy_enabled():
            kwargs["extra_body"] = {
                "provider": {"zdr": True, "data_collection": "deny"}
            }

    # Reasoning ("thinking") budget. Defaults to MAX ("high"); override with
    # MEDBOARD_REASONING_EFFORT (none | low | medium | high, or "default" to omit).
    # OpenRouter, Google's Gemini OpenAI-compat endpoint, and OpenAI's reasoning
    # models all accept reasoning_effort.
    #   - Gemini `-pro` models REQUIRE thinking ("none" → 'Budget 0 is invalid').
    #   - In thinking mode Gemini attaches a thought_signature to each tool call;
    #     specialist.py echoes it back (via the tool_call's extra_content) so the
    #     next turn doesn't 400 with 'Function call is missing a thought_signature'.
    # An explicit per-call `reasoning_effort` wins over the env default — lets a
    # single high-stakes call (e.g. the institution-glossary safety pass) think
    # harder than the cheap default the rest of the pipeline runs at.
    effort = (
        reasoning_effort
        or os.getenv("CANCERPATIENT_REASONING_EFFORT")
        or os.getenv("MEDBOARD_REASONING_EFFORT")
        or "high"
    ).strip().lower()
    if effort and effort != "default":
        kwargs["reasoning_effort"] = effort

    attempt = 0
    timeouts = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            attempt += 1
            if attempt >= max_retries:
                # Raise a clean sentinel so callers can render a user-facing message
                # instead of the raw JSON blob.
                raise QuotaExceeded(
                    "LLM quota exhausted. If you're on Google Gemini, check that "
                    "billing is enabled on your project at "
                    "https://console.cloud.google.com/billing — free-tier limits "
                    "(5 requests/minute) are too tight for this app."
                ) from e
            suggested = _parse_retry_delay(e)
            backoff = max(suggested or 0, min(2**attempt, 30))
            backoff = min(backoff, 60)   # cap at 60s
            log.warning(
                "LLM rate-limited (attempt %d/%d); waiting %.1fs%s",
                attempt, max_retries, backoff,
                f" (server suggested {suggested}s)" if suggested else "",
            )
            time.sleep(backoff)
        except APITimeoutError:
            # Retried far less than a connection error, and deliberately so. A
            # timeout means the provider accepted the request and then took longer
            # than _REQUEST_TIMEOUT to answer; a third four-minute wait helps
            # nobody and the patient is watching a spinner the whole time. Two
            # attempts, then let the caller degrade — every pass has a fallback
            # (keep the un-simplified draft, keep the English, abstain honestly).
            timeouts += 1
            if timeouts >= _MAX_TIMEOUT_ATTEMPTS:
                log.warning(
                    "LLM timed out %d times at %.0fs; giving up so the turn can finish.",
                    timeouts, _REQUEST_TIMEOUT,
                )
                raise
            log.warning(
                "LLM timed out after %.0fs (attempt %d/%d); retrying once.",
                _REQUEST_TIMEOUT, timeouts, _MAX_TIMEOUT_ATTEMPTS,
            )
        except APIConnectionError as e:
            attempt += 1
            if attempt >= max_retries:
                raise
            backoff = min(2**attempt, 16)
            log.warning("LLM connection error; retrying in %ds", backoff)
            time.sleep(backoff)
        except APIStatusError as e:
            if e.status_code in (500, 502, 503, 504) and attempt < max_retries:
                attempt += 1
                backoff = min(2**attempt, 16)
                log.warning("LLM %d error; retrying in %ds", e.status_code, backoff)
                time.sleep(backoff)
                continue
            raise


def _parse_json_payload(raw: str) -> dict | None:
    """Parse a model's JSON reply, tolerating the usual mangling. None if hopeless."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    # Markdown fences are the most common wrapper.
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    # Last resort: the outermost {...} span, which rescues a reply with prose
    # wrapped around otherwise-valid JSON.
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def chat_json(messages, *, model=None, max_retries=5, parse_attempts=2) -> dict:
    """Like chat() but enforces JSON output and parses defensively.

    A malformed reply is retried, not just re-parsed. GLM 5.2 returns
    unparseable JSON in roughly one call in thirty, and the router calls this on
    EVERY patient turn — without a retry those turns silently lose their routing
    and fall back to the generalist. Retrying costs one cheap call and recovers
    the turn; `chat()` already handles transport-level retries separately.
    """
    last_raw = ""
    for attempt in range(1, max(1, parse_attempts) + 1):
        resp = chat(
            messages,
            response_format={"type": "json_object"},
            model=model,
            max_retries=max_retries,
        )
        last_raw = (resp.choices[0].message.content or "").strip()
        parsed = _parse_json_payload(last_raw)
        if parsed is not None:
            return parsed
        # The router's JSON echoes the patient's condition and topic, so the raw
        # reply is patient-derived text and must not reach the log by default.
        log.warning(
            "LLM returned unparseable JSON (attempt %d/%d): %s",
            attempt, parse_attempts, scrub(last_raw),
        )
    raise ValueError(f"LLM returned unparseable JSON: {last_raw[:200]}...")
