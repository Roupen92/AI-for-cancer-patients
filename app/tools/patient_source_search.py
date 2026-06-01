"""Curated Brave search restricted to patient-facing trusted sources.

The agent passes its specialty's `trusted_sources` list (from config.py) and we
build `(site:a OR site:b OR ...) <query>`. Returns ranked hits and registers them
in the evidence ledger as `source_kind="patient_source"` so [N] citations work.
"""
import logging
import os
import httpx

from app.tools import _perplexity

log = logging.getLogger(__name__)

_API = "https://api.search.brave.com/res/v1/web/search"

SCHEMA = {
    "name": "patient_source_search",
    "description": (
        "Search a curated allowlist of patient-facing cancer education and support "
        "sites (Cancer.Net, NCI, ACS, Macmillan, your specialty bodies, and academic "
        "patient pages). Use this as your DEFAULT tool — it produces patient-friendly "
        "results, not clinician jargon. Pass the trusted-source list your role "
        "config gives you. Each result is registered in the evidence ledger so you "
        "can cite it as [N]."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Plain-English search query (e.g., 'exercise during chemo fatigue').",
            },
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Trusted-source domains to restrict the search to. Pass the list "
                    "your role config specifies. If empty, the search will use a small "
                    "default allowlist of authoritative patient-facing oncology orgs."
                ),
            },
            "max_results": {
                "type": "integer",
                "default": 6,
                "description": "Number of hits to return (default 6, max 10).",
            },
        },
        "required": ["query"],
    },
}


_DEFAULT_ALLOWLIST = [
    "cancer.net",
    "cancer.gov",
    "cancer.org",
    "macmillan.org.uk",
    "cancerresearchuk.org",
    "cancercare.org",
]


def _build_query(query: str, sources: list[str]) -> str:
    """Compose `(site:a OR site:b ...) query`. Brave caps query length, so we
    truncate the allowlist to the first ~15 domains, which still covers the
    common case for any one specialty."""
    domains = [s.strip().lstrip(".") for s in (sources or []) if s and isinstance(s, str)]
    # Brave returns HTTP 422 when too many site: clauses are chained. Cap at 6.
    domains = [d for d in domains if "." in d][:6]
    if not domains:
        domains = _DEFAULT_ALLOWLIST
    sites = " OR ".join(f"site:{d}" for d in domains)
    return f"({sites}) {query}".strip()


async def run(args: dict, ctx) -> str:
    raw_query = (args.get("query") or "").strip()
    if not raw_query:
        return "Error: empty query."

    sources = args.get("sources") or []
    count = max(1, min(int(args.get("max_results") or 6), 10))

    # Try Perplexity first (more reliable, cleaner allowlist via post-filter).
    pplx_results, pplx_err = await _perplexity.search(
        raw_query, allowed_domains=sources, max_results=count, fetch_count=count * 3
    )
    if pplx_results:
        lines = [f"Trusted patient-facing sources for: {raw_query}", ""]
        for hit in pplx_results:
            entry = ctx.ledger.add(
                source_kind="patient_source",
                source_id=hit["url"],
                title=hit["title"],
                journal=hit["domain"],
                year=hit["year"],
                url=hit["url"],
                summary=hit["snippet"],
                retrieved_by=ctx.specialist_id,
            )
            lines.append(
                f"[{entry.label}] {hit['title']}  ({hit['domain']})\n"
                f"  URL: {hit['url']}\n"
                f"  Snippet: {hit['snippet'][:300]}\n"
            )
        return "\n".join(lines)

    # If Perplexity is offline (no key) or quota-blocked, fall back to Brave.
    api_key = (
        os.getenv("Brave_API")
        or os.getenv("BRAVE_API_KEY")
        or os.getenv("BRAVE_SEARCH_API_KEY")
    )
    if not api_key:
        # No fallback available — surface the Perplexity error to the agent.
        return pplx_err or (
            "patient_source_search has no working search backend. "
            "Set PERPLEXITY_API_KEY or Brave_API in .env."
        )

    q = _build_query(raw_query, sources)

    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": q, "count": count, "result_filter": "web"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_API, params=params, headers=headers)
            if r.status_code in (401, 422):
                # 422 with SUBSCRIPTION_TOKEN_INVALID is how Brave signals an
                # invalid/expired key. Treat as a hard auth failure and tell the
                # agent to use other tools — don't waste retries.
                return (
                    "patient_source_search is OFFLINE (Brave Search API key is invalid "
                    "or expired). Do not retry this tool this turn. Use pubmed_search, "
                    "pubmed_search_and_fetch, europe_pmc_search, or semantic_scholar_search instead."
                )
            if r.status_code == 429:
                return "Brave rate-limited this request. Try again in a moment."
            if r.status_code != 200:
                log.warning("Brave HTTP %s for %r", r.status_code, q[:120])
                return (
                    f"patient_source_search failed: API returned {r.status_code}. "
                    "Try a different query or another tool."
                )[:200]
            data = r.json()
    except httpx.RequestError as e:
        log.warning("Brave request error for %r: %s", q[:120], e)
        return "patient_source_search failed: network error. Try a different query."[:200]
    except ValueError as e:
        log.warning("Brave JSON decode error for %r: %s", q[:120], e)
        return "patient_source_search failed: malformed response."[:200]

    try:
        web_results = ((data.get("web") or {}).get("results") or [])
    except (KeyError, TypeError, AttributeError):
        return "patient_source_search failed: unexpected response shape."[:200]
    if not web_results:
        return f"No trusted-source hits for: {raw_query}"

    lines = [f"Trusted patient-facing sources for: {raw_query}", ""]
    for hit in web_results[:count]:
        title = hit.get("title") or ""
        url = hit.get("url") or ""
        snippet = hit.get("description") or ""
        page_age = hit.get("page_age") or ""
        domain = ""
        try:
            domain = url.split("/")[2] if "://" in url else url.split("/")[0]
        except (IndexError, AttributeError):
            pass

        entry = ctx.ledger.add(
            source_kind="patient_source",
            source_id=url,
            title=title,
            journal=domain,
            year=str(page_age)[:4],
            url=url,
            summary=snippet[:1200],
            retrieved_by=ctx.specialist_id,
        )
        lines.append(
            f"[{entry.label}] {title}  ({domain})\n"
            f"  URL: {url}\n"
            f"  Snippet: {snippet[:300]}\n"
        )
    return "\n".join(lines)
