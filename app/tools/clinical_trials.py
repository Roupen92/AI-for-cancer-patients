"""ClinicalTrials.gov v2 search for the Clinical Trial Navigator agent.

Patient-facing on purpose: the point of the product is that a patient can reach
the same trial registry their specialist uses. The guardrails against implying
eligibility live in prompts.TRIALS — this module's job is to return clean,
honest records and to refuse to hide the burdens (placebo arms, extra visits,
site distance).

No API key is required. Results are registered in the evidence ledger as
`source_kind="clinical_trial"` so `[N]` citations work like any other source.
"""
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API = "https://clinicaltrials.gov/api/v2/studies"

# Statuses worth showing a patient. Anything terminated, withdrawn, or completed
# is noise at best and false hope at worst.
_OPEN_STATUSES = "RECRUITING,NOT_YET_RECRUITING,ENROLLING_BY_INVITATION,AVAILABLE"

# Only the modules we actually render. Keeps a 10-study response in the tens of
# KB instead of hundreds. If the API rejects the field list we retry without it.
_FIELDS = ",".join(
    [
        "protocolSection.identificationModule",
        "protocolSection.statusModule",
        "protocolSection.designModule",
        "protocolSection.conditionsModule",
        "protocolSection.descriptionModule",
        "protocolSection.armsInterventionsModule",
        "protocolSection.contactsLocationsModule",
        "protocolSection.eligibilityModule",
    ]
)

SCHEMA = {
    "name": "clinical_trials_search",
    "description": (
        "Search ClinicalTrials.gov — the U.S. government's public registry of "
        "clinical trials worldwide — for studies a patient could ask their care "
        "team about. Returns only studies that are open (recruiting, not yet "
        "recruiting, or enrolling by invitation). Pass the patient's condition "
        "and, when you know it, their country/state so you can tell them how far "
        "the nearest site is. Each study is registered in the evidence ledger so "
        "you can cite it as [N]. This tool CANNOT determine eligibility — never "
        "tell a patient they qualify."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "condition": {
                "type": "string",
                "description": (
                    "The disease or condition (e.g., 'heart failure', "
                    "'metastatic pancreatic cancer', 'ulcerative colitis'). Start "
                    "specific; if you get no hits, retry once with a broader term."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "Optional. Country, and state/province or city when known "
                    "(e.g., 'Ontario, Canada', 'United Kingdom', 'Boston, "
                    "Massachusetts'). Filters to studies with a site there."
                ),
            },
            "other_terms": {
                "type": "string",
                "description": (
                    "Optional extra search terms — a drug name or an intervention "
                    "type (e.g., 'pembrolizumab', 'exercise', 'CAR-T'). For a "
                    "molecular marker use `biomarker` instead, not this."
                ),
            },
            "biomarker": {
                "type": "string",
                "description": (
                    "Optional. A molecular alteration the patient has TOLD YOU they "
                    "have, copied exactly as they wrote it ('BRAF V600E', 'KRAS "
                    "G12C', 'EGFR exon 19 deletion', 'MSI-high', 'HER2-low'). "
                    "NEVER infer one from the condition — do not deduce EGFR from "
                    "'lung cancer' or BRCA from 'breast cancer'. Leave this empty "
                    "if the patient did not state a marker. Searching this field "
                    "looks inside trial eligibility criteria, which is where "
                    "markers are usually written; a plain search misses them."
                ),
            },
            "max_results": {
                "type": "integer",
                "default": 8,
                "description": "Number of studies to return (default 8, max 15).",
            },
        },
        "required": ["condition"],
    },
}


_PHASE_PLAIN = {
    "EARLY_PHASE1": "Early Phase 1 (very early safety testing in a small group)",
    "PHASE1": "Phase 1 (mainly tests safety and dose in a small group)",
    "PHASE1, PHASE2": "Phase 1/2 (safety and early signs of whether it works)",
    "PHASE2": "Phase 2 (looks for early signs the treatment works)",
    "PHASE2, PHASE3": "Phase 2/3 (early signs of benefit, then comparison with standard care)",
    "PHASE3": "Phase 3 (compares it against the current standard treatment in a large group)",
    "PHASE4": "Phase 4 (studies a treatment already in normal use)",
    "NA": "Not applicable (this study is not testing a drug in the usual phases)",
}

_STATUS_PLAIN = {
    "RECRUITING": "Recruiting now",
    "NOT_YET_RECRUITING": "Not open yet — expected to start recruiting",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation only",
    "AVAILABLE": "Available outside a trial (expanded access)",
}


def _phase_label(phases: list[str]) -> str:
    key = ", ".join(phases or [])
    return _PHASE_PLAIN.get(key, key or "Phase not listed")


def _status_label(status: str) -> str:
    return _STATUS_PLAIN.get(status or "", (status or "Unknown status").replace("_", " ").title())


_LOCATION_STOPWORDS = {"the", "of", "near", "in", "and", "city", "county", "state", "province"}

# Words that are half of a multi-word place name and match the WRONG country on
# their own — "united" from "United Kingdom" happily matches "United States".
# These are only ever used as part of a full comma component, never alone.
_AMBIGUOUS_PLACE_WORDS = {
    "united", "states", "kingdom", "republic", "new", "north", "south", "east",
    "west", "central", "saint", "san", "santa", "great", "british", "island",
    "islands", "city", "port", "san't",
}


# --------------------------------------------------------------------------- #
# Biomarker search
# --------------------------------------------------------------------------- #
#
# `query.term` searches the registry's `BasicSearch` area, which does NOT include
# `EligibilityCriteria` — and that is where molecular alterations are almost
# always written. Verified against the live API: a UK patient with BRAF V600E
# melanoma got `totalCount: 0` through this tool, by either route:
#
#   query.cond="BRAF V600E melanoma" & query.locn="United Kingdom"  -> 0
#   query.cond=melanoma & query.term="BRAF V600E" & query.locn=UK   -> 0
#
# Aiming the biomarker at AREA[EligibilityCriteria] finds real recruiting studies.
# Same patient, any geography, open statuses: 4 the old way, 15 the new way.
#
# Two fields groups, because a match in each means a very different thing:
#   HIGH  — the study is ABOUT this alteration (it is in the title/condition/arm)
#   LOW   — the study's record merely MENTIONS it, which may well be an EXCLUSION
#           ("must not have a BRAF V600E mutation") or one item in a list of other
#           alterations. Never present a LOW match as "a trial for your marker".
_BIOMARKER_HIGH_FIELDS = ("BriefTitle", "OfficialTitle", "Condition", "Keyword",
                          "ArmGroupLabel", "InterventionName")
_BIOMARKER_LOW_FIELDS = ("EligibilityCriteria", "BriefSummary", "DetailedDescription")


def _essie_quote(term: str) -> str:
    """Quote a term for an Essie AREA[] expression."""
    return '"' + re.sub(r'["\\]', " ", term).strip() + '"'


def _biomarker_query(biomarker: str) -> str:
    """An Essie expression matching `biomarker` in any field that could carry it.

    The outer parentheses are load-bearing. Essie binds AND tighter than OR, so an
    unparenthesized OR-group followed by `AND AREA[LocationCountry]Canada` applies
    the location to only the LAST branch — verified live: 114 results
    unparenthesized vs 20 parenthesized, no error either way.
    """
    quoted = _essie_quote(biomarker)
    clauses = [
        f"AREA[{field}]{quoted}"
        for field in _BIOMARKER_HIGH_FIELDS + _BIOMARKER_LOW_FIELDS
    ]
    return "(" + " OR ".join(clauses) + ")"


def _biomarker_tier(study: dict, biomarker: str) -> str:
    """Where the biomarker actually matched: "selects" | "mentions" | "".

    Decided client-side from the record we already fetched, so one query serves
    both tiers. `selects` means the alteration is in the study's own identity;
    `mentions` means it appears somewhere in the prose and the sentence has to be
    read before anything is claimed.
    """
    if not biomarker:
        return ""
    needle = biomarker.lower()
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    arms = proto.get("armsInterventionsModule") or {}

    high_parts = [ident.get("briefTitle") or "", ident.get("officialTitle") or ""]
    high_parts += (proto.get("conditionsModule") or {}).get("conditions") or []
    high_parts += (proto.get("conditionsModule") or {}).get("keywords") or []
    for group in (arms.get("armGroups") or []):
        if isinstance(group, dict):
            high_parts.append(group.get("label") or "")
    for iv in (arms.get("interventions") or []):
        if isinstance(iv, dict):
            high_parts.append(iv.get("name") or "")
    if any(needle in str(p).lower() for p in high_parts):
        return "selects"
    return "mentions"


# The registry renders eligibility as markdown with these headers in ~97% of
# records. Splitting on the exclusion header is what lets us say whether the
# patient's marker appeared as something the trial WANTS or something it RULES OUT
# — the difference between a lead and a dead end.
_EXCLUSION_HEADER = re.compile(r"^\s*[*#\-\s]*exclusion criteria\s*:?\s*$", re.IGNORECASE | re.MULTILINE)


def _eligibility_mentions(criteria: str, biomarker: str, limit: int = 2) -> list[str]:
    """Sentences from the eligibility criteria naming `biomarker`, each labelled
    INCLUSION or EXCLUSION by position relative to the exclusion header."""
    if not criteria or not biomarker:
        return []

    split = _EXCLUSION_HEADER.search(criteria)
    boundary = split.start() if split else None
    needle = biomarker.lower()

    out: list[str] = []
    for line in re.split(r"(?:\r?\n)+|(?<=[.;])\s+", criteria):
        stripped = line.strip(" \t*-#")
        if not stripped or needle not in stripped.lower():
            continue
        at = criteria.find(line)
        if boundary is None:
            tag = "position in criteria unknown"
        else:
            tag = "EXCLUSION" if at >= boundary else "INCLUSION"
        if len(stripped) > 300:
            stripped = stripped[:297] + "…"
        out.append(f"[{tag}] {stripped}")
        if len(out) >= limit:
            break
    return out


# Per-site recruitment status. A study whose overall status is RECRUITING can list
# hundreds of sites of which most are WITHDRAWN, NOT_YET_RECRUITING or COMPLETED —
# so naming a site without its status sends a patient to a closed door.
_SITE_OPEN = {"RECRUITING", "NOT_YET_RECRUITING", "AVAILABLE"}


def _location_tokens(location: str) -> list[str]:
    """Match keys for deciding whether a trial site is where the patient is.

    Comma components come first and are matched whole ("united kingdom" will not
    match "United States"). Individual words are added only when they are
    unambiguous on their own, so "Boston Massachusetts" typed without a comma
    still matches "Boston, Massachusetts, United States".
    """
    raw = (location or "").lower()
    keys: list[str] = []
    for component in re.split(r"[,/]+", raw):
        component = component.strip()
        if len(component) < 3 or component in _LOCATION_STOPWORDS:
            continue
        keys.append(component)
        words = [w for w in re.split(r"\s+", component) if w]
        if len(words) > 1:
            for w in words:
                if (
                    len(w) >= 4
                    and w not in _LOCATION_STOPWORDS
                    and w not in _AMBIGUOUS_PLACE_WORDS
                    and w not in keys
                ):
                    keys.append(w)
    return keys


def _dedupe_locations(
    locations: list[dict], limit: int = 4, *, prefer: list[str] | None = None
) -> tuple[list[str], int, int]:
    """Collapse a study's site list into a few human-readable place strings.

    Returns (labels, n_matching_prefer, n_open_sites). Each label carries the
    site's own recruitment status when that status is not open, because a study
    listed as RECRUITING routinely has sites that are WITHDRAWN or not yet open,
    and sending a patient to one of those is the failure this exists to prevent.
    Open sites are hoisted above closed ones.

    Registry records often list 200+ sites, and they come back in the registry's
    own order — which for a big international study means the first four are
    usually in Alabama and Arizona. Showing those to a patient in Manchester is
    worse than useless, so sites matching the patient's own location are hoisted
    to the front and counted.
    """
    prefer = prefer or []
    seen: set[str] = set()
    matched_open: list[str] = []
    matched_shut: list[str] = []
    others_open: list[str] = []
    others_shut: list[str] = []
    n_matched = 0
    n_open = 0

    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        parts = [
            (loc.get("city") or "").strip(),
            (loc.get("state") or "").strip(),
            (loc.get("country") or "").strip(),
        ]
        place = ", ".join(p for p in parts if p)
        if not place:
            continue

        site_status = (loc.get("status") or "").strip().upper()
        is_open = site_status in _SITE_OPEN or not site_status
        if is_open:
            n_open += 1
        # Only annotate what the patient would get wrong by assuming: an open site
        # needs no note, a closed one does.
        label = place if is_open else f"{place} ({site_status.replace('_', ' ').lower()})"

        is_match = bool(prefer) and any(tok in place.lower() for tok in prefer)
        if is_match:
            n_matched += 1
        if label in seen:
            continue
        seen.add(label)

        bucket = (
            (matched_open if is_open else matched_shut)
            if is_match
            else (others_open if is_open else others_shut)
        )
        if len(bucket) < limit:
            bucket.append(label)

    ordered = matched_open + matched_shut + others_open + others_shut
    return ordered[:limit], n_matched, n_open


def _extract(
    study: dict,
    prefer_tokens: list[str] | None = None,
    biomarker: str = "",
) -> dict[str, Any]:
    """Pull the fields we render out of a v2 study record, defensively."""
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status_mod = proto.get("statusModule") or {}
    design = proto.get("designModule") or {}
    conditions = proto.get("conditionsModule") or {}
    desc = proto.get("descriptionModule") or {}
    arms = proto.get("armsInterventionsModule") or {}
    contacts = proto.get("contactsLocationsModule") or {}
    elig = proto.get("eligibilityModule") or {}

    nct = (ident.get("nctId") or "").strip()
    brief = (ident.get("briefTitle") or "").strip()
    official = (ident.get("officialTitle") or "").strip()

    start = ((status_mod.get("startDateStruct") or {}).get("date") or "").strip()
    year = start[:4] if start[:4].isdigit() else ""

    design_info = design.get("designInfo") or {}
    allocation = (design_info.get("allocation") or "").replace("_", " ").title()
    masking = ((design_info.get("maskingInfo") or {}).get("masking") or "").replace("_", " ").title()

    interventions = []
    for iv in (arms.get("interventions") or [])[:5]:
        if isinstance(iv, dict):
            name = (iv.get("name") or "").strip()
            itype = (iv.get("type") or "").replace("_", " ").title()
            if name:
                interventions.append(f"{name} ({itype})" if itype else name)

    # Placebo presence is a burden the patient must know about up front.
    arm_text = " ".join(
        str((g or {}).get("type", "")) + " " + str((g or {}).get("label", ""))
        for g in (arms.get("armGroups") or [])
        if isinstance(g, dict)
    )
    has_placebo = "PLACEBO" in arm_text.upper() or "placebo" in " ".join(interventions).lower()

    all_locations = contacts.get("locations") or []
    site_labels, n_nearby, n_open_sites = _dedupe_locations(
        all_locations, prefer=prefer_tokens
    )

    return {
        "biomarker_tier": _biomarker_tier(study, biomarker),
        "biomarker_criteria": _eligibility_mentions(
            elig.get("eligibilityCriteria") or "", biomarker
        ),
        "n_open_sites": n_open_sites,
        "status_verified": ((status_mod.get("statusVerifiedDate")) or "").strip(),
        "why_stopped": (status_mod.get("whyStopped") or "").strip(),
        "nct": nct,
        "title": brief or official or "(no title)",
        "official_title": official,
        "status": (status_mod.get("overallStatus") or "").strip(),
        "phases": design.get("phases") or [],
        "study_type": (design.get("studyType") or "").replace("_", " ").title(),
        "enrollment": ((design.get("enrollmentInfo") or {}).get("count")),
        "allocation": allocation,
        "masking": masking,
        "has_placebo": has_placebo,
        "conditions": conditions.get("conditions") or [],
        "brief_summary": (desc.get("briefSummary") or "").strip(),
        "interventions": interventions,
        "locations": site_labels,
        "n_locations": len(all_locations),
        "n_nearby": n_nearby,
        "min_age": (elig.get("minimumAge") or "").strip(),
        "max_age": (elig.get("maximumAge") or "").strip(),
        "sex": (elig.get("sex") or "").strip(),
        "healthy_volunteers": elig.get("healthyVolunteers"),
        "year": year,
        "start_date": start,
        "url": f"https://clinicaltrials.gov/study/{nct}" if nct else "",
    }


async def _fetch(params: dict) -> tuple[dict | None, str]:
    """GET the registry. Returns (json, error_message)."""
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.get(_API, params=params, headers={"Accept": "application/json"})
            if r.status_code == 400:
                return None, "bad_request"
            if r.status_code == 429:
                return None, "ClinicalTrials.gov rate-limited this request. Try again in a moment."
            if r.status_code != 200:
                log.warning("ClinicalTrials.gov HTTP %s for %r", r.status_code, params)
                return None, (
                    f"clinical_trials_search failed: the registry returned "
                    f"{r.status_code}. Try a broader condition term."
                )
            return r.json(), ""
    except httpx.RequestError as e:
        log.warning("ClinicalTrials.gov request error: %s", e)
        return None, "clinical_trials_search failed: network error reaching ClinicalTrials.gov."
    except ValueError as e:
        log.warning("ClinicalTrials.gov JSON decode error: %s", e)
        return None, "clinical_trials_search failed: malformed response from the registry."


async def run(args: dict, ctx) -> str:
    condition = (args.get("condition") or "").strip()
    if not condition:
        return (
            "Error: `condition` is required. Extract the patient's condition from "
            "their message first, then call this tool."
        )

    location = (args.get("location") or "").strip()
    other_terms = (args.get("other_terms") or "").strip()
    biomarker = (args.get("biomarker") or "").strip()
    try:
        count = max(1, min(int(args.get("max_results") or 8), 15))
    except (TypeError, ValueError):
        count = 8

    # The registry is deterministic: the same query returns the same studies.
    # Re-asking is always wasted time the patient is sitting through.
    memo_key = f"{condition}|{location}|{other_terms}|{biomarker}"
    if getattr(ctx, "already_asked", None) and ctx.already_asked("clinical_trials_search", memo_key):
        return (
            f"You already searched the registry for '{condition}'"
            + (f" in {location}" if location else "")
            + " this turn, and the registry is deterministic — the results are "
            "identical. Do not search again. Write your answer from what you already "
            "have. If nothing had a site near the patient, say that plainly and tell "
            "them their own specialist will know about trials that are not listed yet."
        )

    params: dict[str, Any] = {
        "format": "json",
        "query.cond": condition,
        "filter.overallStatus": _OPEN_STATUSES,
        "pageSize": count,
        "fields": _FIELDS,
        "sort": "@relevance",
    }
    if location:
        params["query.locn"] = location

    # The biomarker goes into `query.term` as an Essie AREA[] expression so it can
    # reach EligibilityCriteria; free-text extras keep their plain form. Both in
    # one term, ANDed, with each side parenthesized (see _biomarker_query).
    term_parts = []
    if biomarker:
        term_parts.append(_biomarker_query(biomarker))
    if other_terms:
        term_parts.append(f"({other_terms})" if biomarker else other_terms)
    if term_parts:
        params["query.term"] = " AND ".join(term_parts)

    data, err = await _fetch(params)

    # An Essie syntax error must not silently become a plain-text search: that
    # returns confident, wrong results (the old behaviour dropped the biomarker
    # entirely on retry). Fall back to the biomarker as free text, which is at
    # least honest about being a weaker match, and only then to no biomarker.
    if data is None and err == "bad_request" and biomarker:
        log.warning("Registry rejected the AREA[] biomarker query; falling back to free text.")
        retry = dict(params)
        retry["query.term"] = " ".join(p for p in (biomarker, other_terms) if p)
        data, err = await _fetch(retry)

    # Some deployments reject `fields` or `sort`; retry with the minimum that is
    # guaranteed to be accepted rather than failing the whole turn.
    if data is None and err == "bad_request":
        minimal = {
            "format": "json",
            "query.cond": condition,
            "filter.overallStatus": _OPEN_STATUSES,
            "pageSize": count,
        }
        if location:
            minimal["query.locn"] = location
        data, err = await _fetch(minimal)
        if err == "bad_request":
            err = (
                "clinical_trials_search failed: the registry rejected the query. "
                "Try a simpler condition term (e.g. 'heart failure' rather than a "
                "long phrase)."
            )

    if data is None:
        return err

    studies = data.get("studies") or []
    if not studies:
        loc_bit = f" with a site in {location}" if location else ""
        return (
            f"No open studies found on ClinicalTrials.gov for '{condition}'{loc_bit}. "
            "Try once more with a broader condition term (drop the subtype or stage), "
            "or drop the location to see whether studies exist elsewhere. If there is "
            "still nothing, tell the patient honestly and suggest they ask their "
            "specialist — specialists know about studies that are not listed yet."
        )

    total = data.get("totalCount")
    header = [
        f"Open clinical trials on ClinicalTrials.gov for: {condition}"
        + (f"  |  location filter: {location}" if location else "")
        + (f"  |  biomarker: {biomarker}" if biomarker else "")
        + (f"  |  extra terms: {other_terms}" if other_terms else ""),
        f"Showing {len(studies)}"
        + (f" of {total} matching open studies." if isinstance(total, int) else " matching open studies.")
        + "  REMINDER: you may not tell the patient they qualify. Eligibility is"
        " decided by the trial team after screening.",
        "",
    ]

    prefer_tokens = _location_tokens(location)

    lines: list[str] = []
    for study in studies:
        s = _extract(study, prefer_tokens, biomarker)
        if not s["nct"]:
            continue

        # What the biomarker match actually licenses you to say. A "mentions"
        # match is as likely to be an exclusion criterion, or one entry in a list
        # of other alterations, as it is to be a trial for this patient.
        if s["biomarker_tier"] == "selects":
            marker_note = (
                f"BIOMARKER: this study's own record is built around {biomarker} "
                "(it is in the title, condition, or arm). Still not eligibility."
            )
        elif s["biomarker_tier"] == "mentions":
            marker_note = (
                f"BIOMARKER: {biomarker} appears only in this study's prose, NOT in "
                "its title or arms. Read the criteria line below before you say "
                "anything — it is often an EXCLUSION, or one item in a list of other "
                "alterations. Do not describe this as a trial for their marker."
            )
        else:
            marker_note = ""

        # A study whose overall status is RECRUITING but which lists no open site
        # has no door the patient can walk through. Say so rather than listing
        # closed sites as though they were options.
        if s["n_locations"] == 0:
            sites_note = (
                "NO SITES ARE LISTED YET for this study. Say that plainly — do not "
                "leave the patient to assume there is somewhere to go."
            )
        elif s["n_open_sites"] == 0:
            sites_note = (
                f"WARNING: all {s['n_locations']} listed sites are closed, withdrawn, "
                "or not yet open. The study reads as open but has no recruiting site."
            )
        else:
            sites_note = ""

        # Say plainly whether any site is where the patient actually is. A study
        # with 117 sites and none of them in their country is a study they cannot
        # join, and the honest version of that is a sentence, not a silent omission.
        if prefer_tokens:
            if s["n_nearby"]:
                nearby_note = (
                    f"{s['n_nearby']} of these sites match your location filter "
                    f"({location}) — those are listed first below."
                )
            else:
                nearby_note = (
                    f"NONE of this study's listed sites appear to be in {location}. "
                    "Tell the patient this plainly rather than listing sites they "
                    "cannot reach."
                )
        else:
            nearby_note = ""

        summary_for_ledger = "\n".join(
            filter(
                None,
                [
                    f"Status: {_status_label(s['status'])}",
                    f"Phase: {_phase_label(s['phases'])}",
                    f"Study type: {s['study_type']}" if s["study_type"] else "",
                    f"Conditions studied: {', '.join(s['conditions'][:6])}" if s["conditions"] else "",
                    f"Interventions: {'; '.join(s['interventions'])}" if s["interventions"] else "",
                    f"Randomized: {s['allocation']}" if s["allocation"] else "",
                    f"Masking: {s['masking']}" if s["masking"] else "",
                    "Includes a placebo or dummy-treatment comparison." if s["has_placebo"] else "",
                    f"Planned enrollment: {s['enrollment']} participants" if s["enrollment"] else "",
                    f"Age range listed: {s['min_age'] or 'any'} to {s['max_age'] or 'any'}" if (s["min_age"] or s["max_age"]) else "",
                    f"Sites listed: {s['n_locations']}"
                    + (f" ({s['n_open_sites']} recruiting or about to)" if s["n_locations"] else "")
                    + (f" — including {', '.join(s['locations'])}" if s["locations"] else ""),
                    sites_note,
                    marker_note,
                    *(s["biomarker_criteria"] or []),
                    f"Status last verified by the sponsor: {s['status_verified']}" if s["status_verified"] else "",
                    f"Why it stopped: {s['why_stopped']}" if s["why_stopped"] else "",
                    nearby_note,
                    f"Started: {s['start_date']}" if s["start_date"] else "",
                    "",
                    (s["brief_summary"][:900] + "…") if len(s["brief_summary"]) > 900 else s["brief_summary"],
                ],
            )
        )

        entry = ctx.ledger.add(
            source_kind="clinical_trial",
            source_id=s["nct"],
            title=s["title"],
            journal="ClinicalTrials.gov",
            year=s["year"],
            url=s["url"],
            summary=summary_for_ledger,
            article_type=_phase_label(s["phases"]).split(" (")[0],
            retrieved_by=ctx.specialist_id,
        )

        lines.append(
            f"[{entry.label}] {s['title']}\n"
            f"  NCT: {s['nct']}   URL: {s['url']}\n"
            f"  Status: {_status_label(s['status'])}   |   {_phase_label(s['phases'])}\n"
            + (f"  Testing: {'; '.join(s['interventions'])}\n" if s["interventions"] else "")
            + (f"  Conditions: {', '.join(s['conditions'][:5])}\n" if s["conditions"] else "")
            + (
                f"  Sites: {s['n_locations']} listed"
                + (f", {s['n_open_sites']} recruiting or about to" if s["n_locations"] else "")
                + (f" — e.g. {', '.join(s['locations'])}" if s["locations"] else "")
                + "\n"
            )
            + (f"  {sites_note}\n" if sites_note else "")
            + (f"  {marker_note}\n" if marker_note else "")
            + "".join(f"    {line}\n" for line in s["biomarker_criteria"])
            + (f"  {nearby_note}\n" if nearby_note else "")
            + ("  NOTE FOR THE PATIENT: this study includes a placebo (dummy treatment) comparison.\n" if s["has_placebo"] else "")
            + (
                f"  Age range listed: {s['min_age'] or 'any'} to {s['max_age'] or 'any'}\n"
                if (s["min_age"] or s["max_age"])
                else ""
            )
            + f"  What it's about: {s['brief_summary'][:400]}\n"
        )

    if not lines:
        return (
            f"The registry returned records for '{condition}' but none had a usable "
            "study ID. Try a different condition term."
        )

    return "\n".join(header + lines)
