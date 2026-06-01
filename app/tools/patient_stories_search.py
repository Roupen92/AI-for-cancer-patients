"""Patient-stories search: pulls podcast episodes from a curated allowlist of
patient-voice cancer podcasts (iTunes Search API, no key required) and written
narratives from trusted-patient-story sites (Brave Search, existing key).

Stage-aware ranking + keyword denylist at the tool layer so the agent gets
results that won't traumatize the patient. iTunes results are cached per
collectionId for 1 hour to stay under undocumented rate limits.

Copyright note: we link out only; we do not scrape transcripts.
"""
import asyncio
import datetime
import logging
import os
import re
import time
import httpx

from app.config import SPECIALIST_CONFIGS
from app.tools import _perplexity

log = logging.getLogger(__name__)

_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
_BRAVE_API = "https://api.search.brave.com/res/v1/web/search"

# Per-show TTL cache: collectionId -> (timestamp, episodes_list).
# Guarded by an asyncio.Lock so concurrent requests for the same show don't
# race on the dict (cheap insurance under the GIL).
_ITUNES_CACHE: dict[int, tuple[float, list[dict]]] = {}
_ITUNES_CACHE_LOCK = asyncio.Lock()
_ITUNES_TTL_SECONDS = 3600  # 1 hour
_ITUNES_EPISODE_LIMIT = 200  # iTunes lookup supports up to 200; older episodes can be topical

# Generic cancer-domain words that match too many episodes if used as required filter terms.
# We use these only as ranking boosts, not as required matches.
_CANCER_DOMAIN_STOPWORDS = {
    "cancer", "care", "treatment", "patient", "patients", "story", "stories",
    "chemo", "chemotherapy", "stage", "stages", "diagnosed", "diagnosis",
    "tumor", "tumour", "oncology", "oncologist",
}

# Words in non-English (or specifically Spanish) titles that signal the episode
# is in a non-English language. When the patient asked for English, drop these.
_NON_ENGLISH_TITLE_MARKERS = re.compile(
    r"\b(cáncer|cancer\s+de|en\s+español|comunidad\s+hispana|mama|sangre|"
    r"chinois|en\s+français|deutsch|українськ|русск)\b",
    re.IGNORECASE,
)


SCHEMA = {
    "name": "patient_stories_search",
    "description": (
        "Find patient-voice stories (podcast episodes and written narratives) about "
        "people who went through a similar cancer. Pulls from a curated allowlist of "
        "trusted sources (Cancer.Net, Macmillan, MSKCC, Dana-Farber, ACS, Stupid Cancer, "
        "CancerCare, The Patient Story, etc.) — NOT general podcast search. "
        "Pass the cancer type, stage, and treatment phase so results can be matched and "
        "older/mismatched stories can be flagged. Each result is registered in the "
        "evidence ledger so you can cite it as [N]."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Short search phrase using cancer-type and treatment concepts only — "
                    "NOT the patient's raw case text. Examples: 'stage 2 breast cancer chemo', "
                    "'esophageal cancer radiation swallowing', 'glioblastoma treatment'."
                ),
            },
            "cancer_type": {
                "type": "string",
                "description": "Cancer type extracted from the case (e.g., 'breast', 'esophageal', 'colorectal').",
            },
            "stage": {
                "type": "string",
                "description": (
                    "Stage if known: 'I', 'II', 'III', 'IV', or empty if unknown. Used for "
                    "matching and to filter out terminal-stage content for curative-intent patients."
                ),
            },
            "treatment_phase": {
                "type": "string",
                "description": (
                    "One of: 'just diagnosed', 'about to start', 'currently in treatment', "
                    "'post-treatment', 'survivorship'. Empty if unknown."
                ),
            },
            "max_results": {
                "type": "integer",
                "default": 5,
                "description": "Number of stories to return (default 5, max 8).",
            },
        },
        "required": ["query", "cancer_type"],
    },
}


# Re-traumatization denylist: when stage is curative-intent (I-III), filter
# results whose title/description contains any of these terms unless the
# patient explicitly asked. The agent prompt is the second line of defense.
_CURATIVE_DENYLIST_TERMS = re.compile(
    r"\b("
    r"hospice|"
    r"end[- ]of[- ]life|"
    r"terminal|"
    r"palliative\s+(care\s+only|only)|"
    r"dying|"
    r"final\s+(days|weeks|months)"
    r")\b",
    re.IGNORECASE,
)


def _is_curative_intent(stage: str) -> bool:
    s = (stage or "").strip().upper()
    return s in ("I", "II", "III", "1", "2", "3", "STAGE I", "STAGE II", "STAGE III")


def _stage_match_score(case_stage: str, story_text: str) -> int:
    """Return 2 for exact stage match, 1 for unknown/unclear, 0 for different stage.

    Looks for "stage X" patterns in the story title+description.
    """
    case = (case_stage or "").strip().upper().lstrip("STAGE ").strip()
    if not case:
        return 1
    case_num = case.replace("IV", "4").replace("III", "3").replace("II", "2").replace("I", "1")
    text = story_text.lower()
    found_stages = set()
    for m in re.finditer(r"stage\s*(iv|iii|ii|i|1|2|3|4)\b", text):
        g = m.group(1).upper().replace("IV", "4").replace("III", "3").replace("II", "2").replace("I", "1")
        found_stages.add(g)
    if not found_stages:
        return 1
    if case_num in found_stages:
        return 2
    return 0


def _year_from_isodate(s: str | None) -> str:
    if not s:
        return ""
    m = re.match(r"^(\d{4})", s)
    return m.group(1) if m else ""


async def _itunes_episodes_for(client: httpx.AsyncClient, collection_id: int) -> list[dict]:
    """Fetch episodes for one podcast (cached). Returns up to 200 recent episodes."""
    now = time.time()
    async with _ITUNES_CACHE_LOCK:
        cached = _ITUNES_CACHE.get(collection_id)
        if cached and (now - cached[0]) < _ITUNES_TTL_SECONDS:
            return cached[1]

    try:
        r = await client.get(
            _ITUNES_LOOKUP,
            params={"id": collection_id, "entity": "podcastEpisode", "limit": _ITUNES_EPISODE_LIMIT},
            timeout=15.0,
        )
        if r.status_code != 200:
            log.warning("iTunes lookup HTTP %s for collectionId=%s", r.status_code, collection_id)
            return []
        data = r.json()
    except (httpx.RequestError, ValueError) as e:
        log.warning("iTunes lookup error for collectionId=%s: %s", collection_id, e)
        return []

    results = data.get("results") or []
    episodes = [
        r for r in results
        if r.get("wrapperType") == "podcastEpisode" or r.get("kind") == "podcast-episode"
    ]
    async with _ITUNES_CACHE_LOCK:
        _ITUNES_CACHE[collection_id] = (now, episodes)
    return episodes


def _format_episode(ep: dict, podcast_name: str) -> dict:
    """Normalize an iTunes episode dict into our internal story shape."""
    duration_ms = ep.get("trackTimeMillis") or 0
    duration_min = int(duration_ms / 60000) if duration_ms else 0
    return {
        "format": "podcast",
        "title": ep.get("trackName", "") or "",
        "source": podcast_name,
        "year": _year_from_isodate(ep.get("releaseDate")),
        "url": ep.get("trackViewUrl", "") or ep.get("episodeUrl", "") or "",
        "summary": (ep.get("description") or ep.get("shortDescription") or "")[:600],
        "duration_min": duration_min,
    }


async def _itunes_search_allowlisted(
    client: httpx.AsyncClient,
    podcasts: dict[str, int],
    query: str,
    cancer_type: str,
    case_stage: str,
    require_english: bool = True,
) -> list[dict]:
    """Fetch recent episodes from every allowlisted podcast in parallel, then
    filter by:
      1. cancer-type term must appear in the episode TITLE (not just description)
      2. drop non-English episodes when require_english=True
      3. drop curative-intent-incompatible content (terminal/hospice) when stage I-III
    Score remaining matches by title-hits (×3) + description-hits, return only score >= 2.
    """
    if not podcasts:
        return []

    # Required terms come from cancer_type; everything else is boost-only.
    cancer_tokens = [
        t.lower() for t in re.findall(r"\w+", cancer_type or "")
        if len(t) >= 3 and t.lower() not in _CANCER_DOMAIN_STOPWORDS
    ]
    boost_tokens = [
        t.lower() for t in re.findall(r"\w+", query or "")
        if len(t) >= 3 and t.lower() not in _CANCER_DOMAIN_STOPWORDS
        and t.lower() not in cancer_tokens
    ]

    name_by_id = {cid: name for name, cid in podcasts.items()}
    episodes_by_show = await asyncio.gather(
        *(_itunes_episodes_for(client, cid) for cid in podcasts.values()),
        return_exceptions=True,
    )

    curative = _is_curative_intent(case_stage)

    scored: list[tuple[int, dict]] = []
    for cid, episodes in zip(podcasts.values(), episodes_by_show):
        if isinstance(episodes, BaseException) or not episodes:
            continue
        pod_name = name_by_id[cid]
        for ep in episodes:
            title = (ep.get("trackName") or "").lower()
            desc = (ep.get("description") or "").lower()
            combined = f"{title} {desc}"

            # Language guard: drop non-English titles when patient asked English.
            if require_english and _NON_ENGLISH_TITLE_MARKERS.search(title):
                continue

            # Required: cancer-type token must appear in TITLE (not just desc).
            # If no cancer_type was given, allow any episode that matches any
            # boost term in title (looser fallback).
            if cancer_tokens:
                if not any(t in title for t in cancer_tokens):
                    continue
            else:
                if not any(t in title for t in boost_tokens):
                    continue

            # Curative-intent denylist (terminal/hospice for stage I-III patients).
            if curative and _CURATIVE_DENYLIST_TERMS.search(combined):
                continue

            # Score: 3x weight on title hits + 1x on description hits.
            title_hits = sum(1 for t in cancer_tokens + boost_tokens if t in title)
            desc_hits = sum(1 for t in cancer_tokens + boost_tokens if t in desc)
            score = title_hits * 3 + desc_hits
            if score < 2:
                continue

            scored.append((score, _format_episode(ep, pod_name)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in scored]


async def _perplexity_stories(
    domains: list[str],
    query: str,
    case_stage: str,
    count: int,
) -> list[dict]:
    """Perplexity Search restricted (post-filtered) to the written-story allowlist."""
    if not domains:
        return []
    results, _err = await _perplexity.search(
        query, allowed_domains=domains, max_results=count, fetch_count=count * 3
    )
    curative = _is_curative_intent(case_stage)
    out: list[dict] = []
    for hit in results:
        title = hit["title"]
        snippet = hit["snippet"]
        text = f"{title} {snippet}".lower()
        if curative and _CURATIVE_DENYLIST_TERMS.search(text):
            continue
        out.append({
            "format": "written",
            "title": title,
            "source": hit["domain"],
            "year": hit["year"],
            "url": hit["url"],
            "summary": snippet[:600],
            "duration_min": 0,
        })
    return out


async def _brave_stories(
    client: httpx.AsyncClient,
    api_key: str,
    domains: list[str],
    query: str,
    case_stage: str,
    count: int,
) -> list[dict]:
    """Brave search restricted to the written-story allowlist."""
    if not domains:
        return []

    # Brave returns HTTP 422 when too many site: clauses are chained. Cap at 6.
    sites = " OR ".join(f"site:{d}" for d in domains[:6])
    q = f"({sites}) {query}".strip()
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": q, "count": count, "result_filter": "web"}

    try:
        r = await client.get(_BRAVE_API, params=params, headers=headers, timeout=15.0)
        if r.status_code in (401, 422):
            # Hard auth failure — Brave key is invalid/expired. Skip silently;
            # iTunes podcast results still flow.
            log.warning("Brave key invalid for patient_stories_search (HTTP %s); using iTunes only", r.status_code)
            return []
        if r.status_code == 429:
            log.warning("Brave rate-limited patient_stories_search")
            return []
        if r.status_code != 200:
            log.warning("Brave HTTP %s for stories query %r", r.status_code, q[:120])
            return []
        data = r.json()
    except (httpx.RequestError, ValueError) as e:
        log.warning("Brave error for stories query %r: %s", q[:120], e)
        return []

    web_results = ((data.get("web") or {}).get("results") or [])
    curative = _is_curative_intent(case_stage)
    out: list[dict] = []
    for hit in web_results:
        title = hit.get("title") or ""
        url = hit.get("url") or ""
        snippet = hit.get("description") or ""
        text = f"{title} {snippet}".lower()
        if curative and _CURATIVE_DENYLIST_TERMS.search(text):
            continue
        try:
            domain = url.split("/")[2] if "://" in url else url.split("/")[0]
        except (IndexError, AttributeError):
            domain = ""
        out.append({
            "format": "written",
            "title": title,
            "source": domain,
            "year": _year_from_isodate(hit.get("page_age", "")),
            "url": url,
            "summary": snippet[:600],
            "duration_min": 0,
        })
    return out


def _format_badge(story: dict) -> str:
    if story["format"] == "podcast":
        d = story.get("duration_min") or 0
        return f"[Listen ~{d} min]" if d else "[Listen]"
    return "[Read]"


def _stale_flag(year: str) -> str:
    """Add a freshness flag for stories older than ~5 years."""
    if not year or not year.isdigit():
        return ""
    age = datetime.datetime.now().year - int(year)
    if age > 5:
        return " (older — some treatments may have changed since)"
    if age > 3:
        return " (a few years old)"
    return ""


async def run(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query. Pass cancer type + treatment concepts."

    cancer_type = (args.get("cancer_type") or "").strip()
    stage = (args.get("stage") or "").strip()
    treatment_phase = (args.get("treatment_phase") or "").strip()
    max_results = max(1, min(int(args.get("max_results") or 5), 8))

    # Build effective search query — append cancer type if not already in query.
    q = query
    if cancer_type and cancer_type.lower() not in q.lower():
        q = f"{cancer_type} {q}".strip()

    # Pull allowlists from config. Defensive: if the stories config isn't there
    # (e.g., during a partial deploy), fall back to sensible defaults.
    cfg = SPECIALIST_CONFIGS.get("stories", {})
    podcasts: dict[str, int] = cfg.get("podcast_allowlist") or {}
    domains: list[str] = cfg.get("trusted_sources") or []

    api_key = (
        os.getenv("Brave_API")
        or os.getenv("BRAVE_API_KEY")
        or os.getenv("BRAVE_SEARCH_API_KEY")
    )

    async with httpx.AsyncClient() as client:
        itunes_task = _itunes_search_allowlisted(
            client, podcasts, q, cancer_type, stage, require_english=True
        )

        # Written-stories backend: Perplexity first, Brave fallback.
        async def _written_task() -> list[dict]:
            pplx = await _perplexity_stories(domains, q, stage, max_results * 2)
            if pplx:
                return pplx
            if api_key and domains:
                return await _brave_stories(client, api_key, domains, q, stage, max_results * 2)
            return []

        itunes_results, brave_results = await asyncio.gather(itunes_task, _written_task())

    # Merge + dedupe by URL.
    all_stories: list[dict] = []
    seen_urls: set[str] = set()
    for s in brave_results + itunes_results:  # written first → preferred when ranking ties
        url = s.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        all_stories.append(s)

    if not all_stories:
        return (
            f"No patient-voice stories found in the trusted allowlist for: {q}. "
            "The allowlist is intentionally narrow (Cancer.Net, Macmillan, MSKCC, "
            "Dana-Farber, ACS, Stupid Cancer, CancerCare, healthtalk.org, "
            "The Patient Story, Bag It Cancer, Imerman Angels, LBBC, Young Survival "
            "Coalition). Recommend the patient ask their hospital social worker about "
            "in-person or virtual support groups."
        )

    # Rank: (stage match desc, format priority asc → written first, year desc).
    def _rank_key(s: dict) -> tuple:
        stage_score = _stage_match_score(stage, f"{s.get('title','')} {s.get('summary','')}")
        format_priority = 0 if s["format"] == "written" else 1
        year_num = int(s["year"]) if s.get("year", "").isdigit() else 0
        return (-stage_score, format_priority, -year_num)

    all_stories.sort(key=_rank_key)
    top = all_stories[:max_results]

    # Register in evidence ledger and emit a model-friendly result string.
    lines = [
        f"Patient stories found for: {q}",
        f"(stage: {stage or 'unknown'} · phase: {treatment_phase or 'unknown'})",
        "",
    ]
    for s in top:
        entry = ctx.ledger.add(
            source_kind="patient_story",
            source_id=s["url"],
            title=s["title"],
            journal=s["source"],
            year=s["year"],
            url=s["url"],
            summary=s["summary"],
            retrieved_by=ctx.specialist_id,
        )
        badge = _format_badge(s)
        year_bit = f" · {s['year']}{_stale_flag(s['year'])}" if s["year"] else ""

        # Flag stage mismatch so the agent can render the warning.
        stage_score = _stage_match_score(stage, f"{s['title']} {s['summary']}")
        mismatch_note = ""
        if stage and stage_score == 0:
            mismatch_note = (
                f"\n  STAGE_MISMATCH: patient case is stage {stage}, this story "
                "appears to be from a different stage — outcomes and experiences can differ."
            )

        lines.append(
            f"[{entry.label}] {badge} {s['title']}  ({s['source']}{year_bit})\n"
            f"  URL: {s['url']}\n"
            f"  Snippet: {s['summary'][:280]}{mismatch_note}\n"
        )

    lines.append(
        "(These are real people's experiences, not predictions about the patient's case. "
        "Sources are restricted to the curated patient-voice allowlist.)"
    )
    return "\n".join(lines)
