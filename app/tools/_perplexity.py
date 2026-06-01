"""Perplexity Search API backend.

Shared helper used by `patient_source_search`, `social_resource_search`, and
`patient_stories_search` (Brave fallback path). Perplexity's `/search`
endpoint returns clean `{title, url, snippet, last_updated}` results without
the `site: OR site:...` brittleness we hit on Brave.

Important behaviors:
- We let Perplexity rank broadly, then POST-FILTER to allowed domains. Their
  Search API honors `site:` only loosely.
- Returns normalized story-like dicts matching the existing Brave-result shape
  so the calling tools can keep their downstream rendering unchanged.
- Treats invalid/expired keys as a hard offline signal — no retries.
"""
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

_API = "https://api.perplexity.ai/search"


def _domain_of(url: str) -> str:
    try:
        return url.split("/")[2] if "://" in url else url.split("/")[0]
    except (IndexError, AttributeError):
        return ""


def _domain_matches_allowlist(url: str, allowed_domains: list[str]) -> bool:
    """A URL is considered allowlisted if its host endswith any allowed domain.
    Handles `www.cancer.net` matching `cancer.net`."""
    if not allowed_domains:
        return True
    domain = _domain_of(url).lower()
    if not domain:
        return False
    domain = domain.removeprefix("www.")
    for d in allowed_domains:
        d_norm = d.strip().lower().removeprefix("www.")
        if not d_norm:
            continue
        if domain == d_norm or domain.endswith("." + d_norm):
            return True
    return False


def _year_from(s: str | None) -> str:
    if not s:
        return ""
    m = re.match(r"^(\d{4})", str(s))
    return m.group(1) if m else ""


async def search(
    query: str,
    *,
    allowed_domains: list[str] | None = None,
    max_results: int = 6,
    fetch_count: int = 20,
) -> tuple[list[dict], str | None]:
    """Run a Perplexity search. Returns (results, error_message).

    On success: error_message is None and results is a list of dicts with keys
        title, url, domain, snippet, year, last_updated.
    On hard failure (auth/quota): returns ([], "<helpful error string>"); the
    caller should surface this to the agent so it doesn't retry blindly.
    """
    api_key = os.getenv("PERPLEXITY_API_KEY") or os.getenv("PPLX_API_KEY")
    if not api_key:
        return [], (
            "Perplexity Search is OFFLINE (no API key configured). "
            "Set PERPLEXITY_API_KEY in .env."
        )

    if not (query or "").strip():
        return [], "Error: empty query."

    # If the caller passed an allowlist, hint Perplexity by appending one site:
    # operator (their parser handles a single one well). Post-filtering does
    # the strict allowlist enforcement.
    q = query.strip()
    if allowed_domains:
        first = (allowed_domains[0] or "").strip()
        if first and "." in first:
            q = f"{q} site:{first}"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": q}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(_API, headers=headers, json=payload)
            if r.status_code in (401, 403):
                return [], (
                    "Perplexity Search is OFFLINE (API key invalid or unauthorized). "
                    "Do not retry this tool. Use pubmed_search, europe_pmc_search, or "
                    "semantic_scholar_search instead."
                )
            if r.status_code == 429:
                return [], "Perplexity rate-limited this request. Try again in a moment."
            if r.status_code != 200:
                log.warning("Perplexity HTTP %s for %r", r.status_code, q[:120])
                return [], (
                    f"Perplexity Search failed: API returned {r.status_code}. "
                    "Try a different query or another tool."
                )[:200]
            data = r.json()
    except httpx.RequestError as e:
        log.warning("Perplexity request error for %r: %s", q[:120], e)
        return [], "Perplexity Search failed: network error. Try a different query."[:200]
    except ValueError as e:
        log.warning("Perplexity JSON decode error for %r: %s", q[:120], e)
        return [], "Perplexity Search failed: malformed response."[:200]

    raw = data.get("results") or []

    normalized: list[dict] = []
    for hit in raw[:fetch_count]:
        url = hit.get("url") or ""
        if not url:
            continue
        if not _domain_matches_allowlist(url, allowed_domains or []):
            continue
        normalized.append({
            "title": hit.get("title") or "",
            "url": url,
            "domain": _domain_of(url),
            "snippet": (hit.get("snippet") or "")[:1200],
            "year": _year_from(hit.get("last_updated") or hit.get("date")),
            "last_updated": hit.get("last_updated") or "",
        })
        if len(normalized) >= max_results:
            break

    return normalized, None
