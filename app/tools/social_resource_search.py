"""Geography-aware patient-resource search for the Patient Navigator.

Forces the agent to think about country/region BEFORE searching, so we don't
recommend US-only programs to UK patients (and vice versa). Built on Brave with
a country-keyed Tier-4 directory allowlist.
"""
import logging
import os
import httpx

from app.tools import _perplexity

log = logging.getLogger(__name__)

_API = "https://api.search.brave.com/res/v1/web/search"

SCHEMA = {
    "name": "social_resource_search",
    "description": (
        "Search for practical patient-support resources (financial assistance, "
        "transportation, work rights, insurance, lodging) restricted to directories "
        "for a SPECIFIC country. Use this AFTER you have extracted the patient's "
        "country from their location. Pass the country (and region if known); the "
        "tool dispatches to the right Tier-4 directory list. Each result is registered "
        "in the evidence ledger so you can cite it as [N]."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Plain-English query (e.g., 'help paying for chemo drugs').",
            },
            "country": {
                "type": "string",
                "description": (
                    "Patient's country (full name, e.g., 'United States', 'United Kingdom', "
                    "'Canada', 'Australia', 'Germany'). REQUIRED — searches without a country "
                    "will be rejected to prevent mismatched recommendations."
                ),
            },
            "region": {
                "type": "string",
                "description": "Optional state/province/region (e.g., 'California', 'Ontario').",
            },
            "max_results": {
                "type": "integer",
                "default": 6,
                "description": "Number of hits to return (default 6, max 10).",
            },
        },
        "required": ["query", "country"],
    },
}


# Country-keyed directory allowlists. Keys normalized to lowercase.
# General-purpose benefits/assistance directories come FIRST in each list: the
# Brave fallback path truncates to 6 `site:` clauses, so the domains that serve
# any condition must survive truncation. Disease-specific charities follow.
_DIRECTORIES: dict[str, list[str]] = {
    "united states": [
        # Condition-agnostic first
        "benefits.gov", "findhelp.org", "211.org", "ssa.gov", "medicare.gov",
        "medicaid.gov", "healthcare.gov", "dol.gov", "eeoc.gov",
        "needymeds.org", "patientadvocate.org", "panfoundation.org",
        "healthwellfoundation.org", "copays.org", "ruralhealthinfo.org",
        "aging.gov", "eldercare.acl.gov",
        # Disease-specific programs
        "cancercare.org", "triagecancer.org", "cancerfac.org", "lls.org",
        "kidneyfund.org", "kidney.org", "heart.org", "diabetes.org",
        "lung.org", "alz.org", "arthritis.org",
    ],
    "united kingdom": [
        "gov.uk", "nhs.uk", "citizensadvice.org.uk", "turn2us.org.uk",
        "carersuk.org", "moneyhelper.org.uk",
        "macmillan.org.uk", "mariecurie.org.uk", "cancerresearchuk.org",
        "kidneycareuk.org", "bhf.org.uk", "diabetes.org.uk",
        "asthmaandlung.org.uk", "alzheimers.org.uk", "versusarthritis.org",
        "stroke.org.uk", "mssociety.org.uk",
    ],
    "canada": [
        "canada.ca", "211.ca", "cancer.ca", "wellspring.ca",
        "heartandstroke.ca", "diabetes.ca", "kidney.ca", "alzheimer.ca",
        "arthritis.ca", "lung.ca",
    ],
    "australia": [
        "servicesaustralia.gov.au", "healthdirect.gov.au", "carergateway.gov.au",
        "askizzy.org.au",
        "cancer.org.au", "cancercouncil.com.au", "canteen.org.au",
        "heartfoundation.org.au", "diabetesaustralia.com.au",
        "kidney.org.au", "lungfoundation.com.au", "dementia.org.au",
    ],
    "ireland": [
        "citizensinformation.ie", "hse.ie", "cancer.ie", "irishheart.ie",
        "diabetes.ie", "ika.ie",
    ],
    "new zealand": [
        "workandincome.govt.nz", "health.govt.nz", "cancer.org.nz",
        "heartfoundation.org.nz", "diabetes.org.nz", "kidney.health.nz",
    ],
    "germany": [
        "bundesgesundheitsministerium.de", "gesundheitsinformation.de",
        "krebsinformationsdienst.de", "krebshilfe.de",
        "herzstiftung.de", "diabetesde.org", "nierenstiftung.de",
    ],
    "france": [
        "service-public.fr", "ameli.fr", "sante.fr",
        "ligue-cancer.net", "e-cancer.fr", "fedecardio.org",
        "federationdesdiabetiques.org", "francerein.org",
    ],
    "india": [
        "nhp.gov.in", "pmjay.gov.in", "cancerindia.org.in",
        "diabetesindia.org.in", "kidneywarriors.org",
    ],
    "netherlands": [
        "rijksoverheid.nl", "thuisarts.nl", "kwf.nl", "hartstichting.nl",
        "nierstichting.nl", "diabetesfonds.nl",
    ],
}

# Common aliases / informal names.
_COUNTRY_ALIASES = {
    "us": "united states", "u.s.": "united states", "usa": "united states",
    "america": "united states", "u.s.a.": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
    "northern ireland": "united kingdom",
    "can": "canada",
    "aus": "australia", "au": "australia",
    "nz": "new zealand",
    "de": "germany", "deutschland": "germany",
    "fr": "france",
    "ie": "ireland", "eire": "ireland",
    "in": "india", "bharat": "india",
    "nl": "netherlands", "holland": "netherlands", "the netherlands": "netherlands",
}


def _normalize_country(country: str) -> str:
    c = (country or "").strip().lower()
    return _COUNTRY_ALIASES.get(c, c)


def _build_query(query: str, country: str, region: str, sources: list[str]) -> str:
    # Brave returns HTTP 422 when too many site: clauses are chained. Cap at 6.
    sites = " OR ".join(f"site:{d}" for d in sources[:6])
    region_bit = f" {region}" if region else ""
    return f"({sites}) {query} {country}{region_bit}".strip()


async def run(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."

    country_raw = (args.get("country") or "").strip()
    if not country_raw:
        return (
            "social_resource_search requires a `country`. Extract it from the "
            "patient's location text first, then call this tool. If you cannot "
            "determine the country, tell the patient and offer general framing only."
        )

    country = _normalize_country(country_raw)
    region = (args.get("region") or "").strip()
    count = max(1, min(int(args.get("max_results") or 6), 10))

    sources = _DIRECTORIES.get(country)
    if not sources:
        return (
            f"I do not have a curated resource directory for '{country_raw}'. "
            "Try a general patient_source_search restricted to international "
            "patient sites (medlineplus.gov, who.int, nhs.uk) plus the patient's "
            "own condition charity, and tell the patient plainly that you could "
            "not verify country-specific programs for where they live — then point "
            "them to their hospital's social worker, who will know the local system."
        )

    # Perplexity first (post-filtered to the country's directory allowlist).
    pplx_query = f"{query} {country_raw}" + (f" {region}" if region else "")
    pplx_results, pplx_err = await _perplexity.search(
        pplx_query, allowed_domains=sources, max_results=count, fetch_count=count * 3
    )
    if pplx_results:
        lines = [f"Patient resources in {country_raw} for: {query}", ""]
        for hit in pplx_results:
            entry = ctx.ledger.add(
                source_kind="resource_directory",
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

    # Brave fallback (unchanged below).
    api_key = (
        os.getenv("Brave_API")
        or os.getenv("BRAVE_API_KEY")
        or os.getenv("BRAVE_SEARCH_API_KEY")
    )
    if not api_key:
        return pplx_err or (
            "social_resource_search has no working search backend. "
            "Set PERPLEXITY_API_KEY or Brave_API in .env."
        )

    q = _build_query(query, country_raw, region, sources)
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": q, "count": count, "result_filter": "web"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_API, params=params, headers=headers)
            if r.status_code in (401, 422):
                return (
                    "social_resource_search is OFFLINE (Brave Search API key is invalid "
                    "or expired). Do not retry this tool. Recommend the patient ask their "
                    "hospital social worker / patient navigator for local resources instead."
                )
            if r.status_code == 429:
                return "Brave rate-limited this request. Try again in a moment."
            if r.status_code != 200:
                log.warning("Brave HTTP %s for %r", r.status_code, q[:120])
                return (
                    f"social_resource_search failed: API returned {r.status_code}."
                )[:200]
            data = r.json()
    except httpx.RequestError as e:
        log.warning("Brave request error for %r: %s", q[:120], e)
        return "social_resource_search failed: network error."[:200]
    except ValueError as e:
        log.warning("Brave JSON decode error for %r: %s", q[:120], e)
        return "social_resource_search failed: malformed response."[:200]

    try:
        web_results = ((data.get("web") or {}).get("results") or [])
    except (KeyError, TypeError, AttributeError):
        return "social_resource_search failed: unexpected response shape."[:200]
    if not web_results:
        return f"No directory hits in {country_raw} for: {query}"

    lines = [f"Patient resources in {country_raw} for: {query}", ""]
    for hit in web_results[:count]:
        title = hit.get("title") or ""
        url = hit.get("url") or ""
        snippet = hit.get("description") or ""
        domain = ""
        try:
            domain = url.split("/")[2] if "://" in url else url.split("/")[0]
        except (IndexError, AttributeError):
            pass

        entry = ctx.ledger.add(
            source_kind="resource_directory",
            source_id=url,
            title=title,
            journal=domain,
            year="",
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
