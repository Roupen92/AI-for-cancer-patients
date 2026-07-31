"""Configuration for the patient-facing care team — all conditions, not just cancer.

The roster is DYNAMIC: `app/router.py` picks which specialists run for a given
patient message, so a question about salt in heart failure spins one dietitian
instead of nine agents. `SPECIALIST_CONFIGS` is the catalogue they're chosen from.

The translator is not a "specialist" in the parallel gather — it runs once, after
synthesis, via board._translate().
"""
import os

from app import prompts


# Env prefixes: CANCERPATIENT_* (this app's original prefix) and MEDBOARD_* (the
# AI Tumor Board's, so an .env copied from there works unchanged). CANCERPATIENT_*
# takes precedence when both are set. Both are kept for backwards compatibility
# with existing deployments; there is no third prefix.


def _resolve_provider() -> str:
    """Pick the LLM provider.

    An explicit CANCERPATIENT_PROVIDER / MEDBOARD_PROVIDER always wins. When none
    is set, infer it from whichever API key is actually present so a deployment
    that only supplies one key (e.g. OPENROUTER_API_KEY on Railway) just works
    without a separate provider flag. OpenRouter is checked first (cheapest),
    then Gemini (free tier), with OpenAI as the fallback.
    """
    explicit = (
        os.getenv("CANCERPATIENT_PROVIDER")
        or os.getenv("MEDBOARD_PROVIDER")
        or ""
    ).strip().lower()
    if explicit:
        return explicit
    if os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER"):
        return "openrouter"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "gemini"  # nothing set: keep the documented default; get_client() raises a clear error


PROVIDER = _resolve_provider()

# The default model has to match the resolved provider — a Gemini model name sent
# to OpenAI (or vice versa) fails. An explicit *_MODEL override always wins.
_DEFAULT_MODELS = {
    "openai": "gpt-5.1",
    "openrouter": "z-ai/glm-5.2",
}
_DEFAULT_MODEL = _DEFAULT_MODELS.get(PROVIDER, "gemini-2.5-flash")
MODEL_NAME = (
    os.getenv("CANCERPATIENT_MODEL")
    or os.getenv("MEDBOARD_MODEL")
    or _DEFAULT_MODEL
)

# Single round of parallel specialists; no judge, no consensus loop.
SINGLE_ROUND = True
PARALLEL_SPECIALISTS = 5
MAX_TOOL_ITERATIONS = 5

# Hard ceiling on how many specialists the router may spin for one message. The
# router prompt asks for 2-4 in team mode; this is the enforcement.
MAX_TEAM_SIZE = 4


# Tools available to every research agent. The translator gets none.
PATIENT_BASE_TOOLS: set[str] = {
    "pubmed_search_and_fetch",
    "pubmed_search",
    "pubmed_fetch",
    "europe_pmc_search",
    "semantic_scholar_search",
    "patient_source_search",
    "web_search",
}


# --------------------------------------------------------------------------- #
# Trusted patient-facing source allowlists, used by `patient_source_search`.
# The agent's system prompt names them; the tool restricts results to them
# (Perplexity post-filters the full list; the Brave fallback truncates to 6
# `site:` clauses, so put the highest-value domains first).
# --------------------------------------------------------------------------- #

# Tier 1 — general, condition-agnostic, government/public-service patient info.
_GENERAL_PATIENT = [
    "medlineplus.gov",        # US National Library of Medicine, plain-language by design
    "nhs.uk",                 # UK National Health Service
    "nih.gov",
    "cdc.gov",
    "who.int",
    "nice.org.uk",            # UK guideline body (has patient versions)
    "healthdirect.gov.au",
    "familydoctor.org",
    "health.gov",
]

# Tier 4 — major academic-center patient pages.
_ACADEMIC_PATIENT_PAGES = [
    "mayoclinic.org",
    "clevelandclinic.org",
    "hopkinsmedicine.org",
    "mskcc.org",
    "mdanderson.org",
    "dana-farber.org",
]

# Tier 2 — condition-specific patient organizations, grouped so a specialist can
# take the whole set. Retrieval is query-driven, so carrying the full catalogue
# costs nothing but lets any condition find its own charity.
_CONDITION_ORGS = [
    # Cancer
    "cancer.net", "cancer.gov", "cancer.org", "macmillan.org.uk",
    "cancerresearchuk.org", "esmo.org", "lls.org", "cancercare.org",
    # Heart / circulation
    "heart.org", "bhf.org.uk", "acc.org",
    # Diabetes / endocrine
    "diabetes.org", "diabetes.org.uk", "thyroid.org",
    # Kidney
    "kidney.org", "kidneyfund.org", "kidneycareuk.org",
    # Lung / breathing
    "lung.org", "asthmaandlung.org.uk", "goldcopd.org",
    # Brain / nerves
    "stroke.org", "stroke.org.uk", "parkinson.org", "alz.org",
    "nationalmssociety.org", "mssociety.org.uk", "epilepsy.com", "als.org",
    "migrainetrust.org",
    # Digestive / liver
    "crohnscolitisfoundation.org", "celiac.org", "liverfoundation.org",
    "guts.org.uk",
    # Joints / immune
    "arthritis.org", "versusarthritis.org", "lupus.org", "rheumatology.org",
    # Blood
    "hematology.org", "sicklecelldisease.org",
    # Other common long-term conditions
    "sleepfoundation.org", "aafa.org", "hivinfo.nih.gov",
]

_BASE_TRUSTED = _GENERAL_PATIENT + _CONDITION_ORGS + _ACADEMIC_PATIENT_PAGES


# Patient-story podcast allowlist: iTunes collectionId → show display name.
# IDs were looked up via the iTunes Search API; they are stable for the life of
# the podcast. Match on numeric ID, not name (names drift).
_PATIENT_STORY_PODCASTS: dict[str, int] = {
    "Cancer.Net Podcast (ASCO)":                           1334855695,
    "The Stupid Cancer Show":                              1622221056,
    "Cancer Straight Talk (MSKCC)":                        1531529982,
    "Unraveled (Dana-Farber)":                             1575993703,
    "CancerCare Connect Education Workshops":               451511608,
    "Digital Diaries: Cancer Patient Stories (Macmillan)": 1616856984,
}

# Written-story site allowlist. healthtalk.org is the anchor: it is a UK
# research-grade narrative archive covering 100+ conditions, not just cancer,
# which is what makes a general patient-stories agent viable at all.
_PATIENT_STORY_DOMAINS: list[str] = [
    "healthtalk.org",                # research-grade narratives, all conditions
    "patientstory.com",
    "rarediseases.org",              # patient stories across rare disease
    # Condition charities that publish first-person accounts
    "heart.org",
    "diabetes.org.uk",
    "kidney.org",
    "lung.org",
    "stroke.org",
    "parkinson.org",
    "alz.org",
    "nationalmssociety.org",
    "crohnscolitisfoundation.org",
    "arthritis.org",
    "lupus.org",
    "sicklecelldisease.org",
    # Cancer-specific voices (kept from the original build)
    "cancer.net",
    "cancer.org",
    "macmillan.org.uk",
    "cancersupportcommunity.org",
    "stupidcancer.org",
    "imermanangels.org",
    "lbbc.org",
    "youngsurvival.org",
]


SPECIALIST_CONFIGS: dict[str, dict] = {
    "researcher": {
        "display_name": "Medical Research",
        "color": "#3F6C8F",     # steel blue
        "system_prompt": prompts.RESEARCHER,
        "allowed_tools": PATIENT_BASE_TOOLS,
        # No MeSH narrowing: this agent answers questions about any condition, so
        # a fixed bias would fight the query instead of focusing it.
        "pubmed_bias": None,
        "trusted_sources": _BASE_TRUSTED,
        "citation_required": True,
        "conditional": False,
        # The fallback when the router returns nothing usable.
        "default_agent": True,
    },
    "physio": {
        "display_name": "Physiotherapist",
        "color": "#4A7C6F",     # sage
        "system_prompt": prompts.PHYSIO,
        "allowed_tools": PATIENT_BASE_TOOLS,
        "pubmed_bias": {
            "mesh_terms": [
                "Physical Therapy Modalities",
                "Rehabilitation",
                "Exercise Therapy",
                "Postoperative Care",
                "Accidental Falls",
                "Lymphedema",
                "Peripheral Nervous System Diseases",
            ]
        },
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                "apta.org",
                "csp.org.uk",
                "oncologypt.org",
                "stroke.org", "stroke.org.uk",
                "arthritis.org", "versusarthritis.org",
                "lung.org", "heart.org",
                "parkinson.org",
                "lymphnet.org", "lymphaticnetwork.org", "lymphoedema.org",
            ]
            + _ACADEMIC_PATIENT_PAGES
        ),
        "citation_required": True,
        "conditional": False,
    },
    "exercise": {
        "display_name": "Exercise & Activity",
        "color": "#5C9E52",     # fresh green
        "system_prompt": prompts.EXERCISE,
        "allowed_tools": PATIENT_BASE_TOOLS,
        "pubmed_bias": {
            "mesh_terms": [
                "Exercise",
                "Physical Fitness",
                "Resistance Training",
                "Sedentary Behavior",
                "Exercise Tolerance",
            ]
        },
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                "acsm.org",
                "heart.org",
                "diabetes.org", "diabetes.org.uk",
                "lung.org",
                "kidney.org",
                "arthritis.org",
                "cancer.org",
            ]
            + _ACADEMIC_PATIENT_PAGES
        ),
        "citation_required": True,
        "conditional": False,
    },
    "dietician": {
        "display_name": "Dietitian",
        "color": "#8E9F4A",     # warm olive
        "system_prompt": prompts.DIETICIAN,
        "allowed_tools": PATIENT_BASE_TOOLS,
        "pubmed_bias": {
            "mesh_terms": [
                "Diet Therapy",
                "Nutrition Therapy",
                "Diet, Sodium-Restricted",
                "Malnutrition",
                "Dietary Proteins",
                "Blood Glucose",
            ]
        },
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                "eatright.org",
                "bda.uk.com",
                "espen.org",
                "nutrition.org.uk",
                "kidney.org", "kidneyfund.org",
                "heart.org",
                "diabetes.org", "diabetes.org.uk",
                "crohnscolitisfoundation.org", "celiac.org",
                "liverfoundation.org",
                "aicr.org", "oncologynutrition.org",
            ]
            + _ACADEMIC_PATIENT_PAGES
        ),
        "citation_required": True,
        "conditional": False,
    },
    "slp": {
        "display_name": "Speech & Swallowing",
        "color": "#5A8FA8",     # muted teal
        "system_prompt": prompts.SLP,
        "allowed_tools": PATIENT_BASE_TOOLS,
        "pubmed_bias": {
            "mesh_terms": [
                "Deglutition Disorders",
                "Speech Therapy",
                "Aphasia",
                "Voice Disorders",
                "Dysarthria",
                "Laryngectomy",
            ]
        },
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                "asha.org",
                "rcslt.org",
                "dysphagiaresearch.org",
                "stroke.org", "stroke.org.uk",
                "parkinson.org",
                "alz.org",
                "als.org",
                "webwhispers.org",
                "headandneck.org",
            ]
            + _ACADEMIC_PATIENT_PAGES
        ),
        "citation_required": True,
        # The SLP self-skips when swallowing/voice/speech isn't in play; the
        # router is the first filter and this flag drives the UI badge.
        "conditional": True,
    },
    "mental": {
        "display_name": "Emotional Wellbeing",
        "color": "#7A6BAA",     # muted purple
        "system_prompt": prompts.MENTAL_HEALTH,
        "allowed_tools": PATIENT_BASE_TOOLS,
        "pubmed_bias": {
            "mesh_terms": [
                "Adaptation, Psychological",
                "Anxiety",
                "Depression",
                "Sleep Initiation and Maintenance Disorders",
                "Cognitive Behavioral Therapy",
                "Caregiver Burden",
            ]
        },
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                "nimh.nih.gov",
                "mind.org.uk",
                "nami.org",
                "apa.org",
                "mentalhealth.org.uk",
                "apos-society.org", "ipos-society.org",
                "cancersupportcommunity.org",
                "988lifeline.org", "samaritans.org",
                "findahelpline.com", "crisistextline.org",
            ]
            + _ACADEMIC_PATIENT_PAGES
        ),
        "citation_required": True,
        "conditional": False,
    },
    "trials": {
        "display_name": "Clinical Trials",
        "color": "#2C7A6B",     # deep teal
        "system_prompt": prompts.TRIALS,
        "allowed_tools": {
            "clinical_trials_search",
            "patient_source_search",
            "pubmed_search",
        },
        "pubmed_bias": None,
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                "clinicaltrials.gov",
                "bepartofresearch.nihr.ac.uk",
                "cancer.gov",
                "nih.gov",
            ]
            + _CONDITION_ORGS[:12]
        ),
        # Registry records are the evidence here, and they come from the trials
        # tool rather than the literature — the soft gate keeps a well-sourced
        # trial list from being forced into an abstain.
        "citation_required": True,
        "soft_citation_gate": True,
        "needs_location": True,
        "conditional": False,
    },
    "navigator": {
        "display_name": "Patient Navigator",
        "color": "#C97B3F",     # warm amber
        "system_prompt": prompts.SOCIAL_WORKER,
        "allowed_tools": PATIENT_BASE_TOOLS | {"social_resource_search"},
        "pubmed_bias": None,
        "trusted_sources": (
            _GENERAL_PATIENT
            + [
                # US
                "benefits.gov", "ssa.gov", "medicare.gov", "medicaid.gov",
                "dol.gov", "eeoc.gov",
                "needymeds.org", "panfoundation.org", "healthwellfoundation.org",
                "copays.org", "patientadvocate.org", "findhelp.org", "211.org",
                "ruralhealthinfo.org",
                "cancercare.org", "triagecancer.org", "lls.org", "kidneyfund.org",
                # UK
                "gov.uk", "citizensadvice.org.uk", "turn2us.org.uk",
                "carersuk.org", "macmillan.org.uk", "mariecurie.org.uk",
                # Canada
                "canada.ca", "cancer.ca", "wellspring.ca",
                # Australia
                "servicesaustralia.gov.au", "carergateway.gov.au", "cancer.org.au",
                # Professional
                "aosw.org", "socialworkers.org",
            ]
        ),
        # The navigator's value is in directing people to real-world programs;
        # peer-reviewed evidence rarely exists for "is there a ride-to-dialysis
        # program in Manchester." The citation gate is softened so that web
        # sources from `patient_source_search` and `social_resource_search`
        # satisfy the [N] requirement.
        "citation_required": True,
        "soft_citation_gate": True,
        "needs_location": True,
        "conditional": False,
    },
    "stories": {
        "display_name": "Stories from Others",
        "color": "#B05E6E",     # warm rose
        "system_prompt": prompts.STORIES,
        "allowed_tools": {"patient_stories_search", "patient_source_search"},
        "pubmed_bias": None,
        "trusted_sources": _PATIENT_STORY_DOMAINS,
        "podcast_allowlist": _PATIENT_STORY_PODCASTS,
        # Story citations are URLs to lived experience, not PubMed papers — the
        # citation gate is soft (the carve-out is also in the SYNTHESIZER prompt).
        "citation_required": True,
        "soft_citation_gate": True,
        "conditional": False,
    },
    "translator": {
        "display_name": "Translator",
        "color": "#6B5F52",     # warm taupe
        "system_prompt": prompts.TRANSLATOR,
        "allowed_tools": set(),
        "pubmed_bias": None,
        "trusted_sources": [],
        "citation_required": False,
        # Marker: this agent is NOT run through the parallel gather. It runs
        # once, after the synthesizer, via board._translate().
        "role": "post_synthesis",
    },
}


# Heading used for each specialist's section in the synthesized summary. Driven
# off the same dict so adding an agent can't desync the synthesizer's outline.
SECTION_HEADINGS: dict[str, str] = {
    "researcher": "What the evidence says",
    "physio":     "Movement, rehab, and physical function",
    "exercise":   "Getting active safely",
    "dietician":  "Eating and nutrition",
    "slp":        "Swallowing, voice, and communication",
    "mental":     "Emotional wellbeing",
    "trials":     "Clinical trials you could ask about",
    "navigator":  "Practical help — money, work, transport, support",
    "stories":    "Stories from people who've been through this",
}

# The order sections appear in, regardless of the order the router listed them.
# Evidence first (it frames everything), practical help and stories last.
SECTION_ORDER: list[str] = [
    "researcher",
    "physio",
    "exercise",
    "dietician",
    "slp",
    "mental",
    "trials",
    "navigator",
    "stories",
]


# The roster for a one-shot "full consult" (the /api/board endpoint and the eval
# harness), where nothing has triaged the case. Deliberately not every agent:
# `exercise` overlaps `physio` for someone who hasn't said which they need, and
# `trials` is only appropriate when trials were actually asked about. The chat
# path ignores this and uses the router's selection instead.
FULL_CONSULT_IDS: list[str] = [
    "researcher",
    "physio",
    "dietician",
    "slp",        # keyword-gated by board._slp_relevant on this path
    "mental",
    "navigator",
    "stories",
]


def researcher_ids() -> list[str]:
    """Every specialist eligible for the parallel research round."""
    return [
        sid
        for sid, cfg in SPECIALIST_CONFIGS.items()
        if cfg.get("role") != "post_synthesis"
    ]


def default_specialist_id() -> str:
    """The agent used when the router fails or returns nothing usable."""
    for sid, cfg in SPECIALIST_CONFIGS.items():
        if cfg.get("default_agent"):
            return sid
    return "researcher"


def order_specialists(ids: list[str]) -> list[str]:
    """Sort a router-chosen set into SECTION_ORDER, dropping unknown ids."""
    known = set(researcher_ids())
    seen: set[str] = set()
    picked = [i for i in ids if i in known and not (i in seen or seen.add(i))]
    return sorted(picked, key=lambda s: SECTION_ORDER.index(s) if s in SECTION_ORDER else 99)


SPECIALIST_IDS = list(SPECIALIST_CONFIGS.keys())


def public_specialist_info(ids: list[str] | None = None) -> list[dict]:
    """Roster sent to the frontend. Pass the router-selected ids to describe just
    this turn's team; omit for the full catalogue."""
    wanted = ids if ids is not None else SPECIALIST_IDS
    out = []
    for sid in wanted:
        cfg = SPECIALIST_CONFIGS.get(sid)
        if not cfg:
            continue
        out.append(
            {
                "id": sid,
                "display_name": cfg["display_name"],
                "color": cfg["color"],
                "conditional": cfg.get("conditional", False),
                "role": cfg.get("role"),
                "section_heading": SECTION_HEADINGS.get(sid, cfg["display_name"]),
            }
        )
    return out
