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

# Genomics. THE FIRST SIX ENTRIES ARE LOAD-BEARING: the Brave fallback truncates
# to 6 `site:` clauses, so those six have to be able to answer a gene-or-variant
# question on their own. Deliberately NOT led by _GENERAL_PATIENT — cdc.gov,
# who.int and nice.org.uk have almost nothing patient-facing on variant
# interpretation, and putting them first would spend half the budget on domains
# that return nothing for this agent's questions.
_GENOMIC_PATIENT = [
    "medlineplus.gov",            # MedlinePlus Genetics — plain-language gene and condition pages
    "genome.gov",                 # NHGRI, incl. the Talking Glossary of Genomic Terms
    "cancer.gov",                 # NCI patient pages on biomarker and tumour genomic testing
    "cancer.net",                 # ASCO patient site — understanding biomarker test results
    "nsgc.org",                   # National Society of Genetic Counselors — find a counsellor
    "nhs.uk",                     # NHS Genomic Medicine Service, patient-facing
    # Below here only the Perplexity path (post-filter, full list) ever sees them.
    "rarediseases.info.nih.gov",  # GARD — inherited conditions
    "clinicalgenome.org",         # ClinGen
    "ncbi.nlm.nih.gov",           # ClinVar / MedGen — clinician register, cite sparingly
    "acmg.net",
    "genomicseducation.hee.nhs.uk",
    "nih.gov",
    "healthdirect.gov.au",
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


# --------------------------------------------------------------------------- #
# Room metadata ("your care team" dashboard)
#
# Each researcher specialist is a ROOM the patient can walk into and chat with on
# its own. The `room` block below is the card the patient sees before they commit
# to a room, so the copy has to be honest about what that agent can actually do —
# it is written from the agent's own system prompt in app/prompts.py, and nothing
# in it may promise a capability the prompt forbids.
#
#   tagline  <= 60 chars, second person, no hype
#   blurb    1-2 sentences: what this room is for (and, where it matters, what it
#            deliberately is NOT — `trials` may never suggest we can tell someone
#            whether they qualify; `genomics` explains terms, it does not
#            interpret a patient's own result)
#   covers   3-5 short topic chips
#   examples 3 first-person starter questions a patient would really type
#
# A missing `room` ships as a broken dashboard card with no error anywhere, which
# is why tests/test_smoke.py asserts every researcher has one — the same failure
# mode as a missing SECTION_HEADINGS entry.
# --------------------------------------------------------------------------- #

SPECIALIST_CONFIGS: dict[str, dict] = {
    "researcher": {
        "display_name": "Medical Research",
        "color": "#3F6C8F",     # steel blue
        "system_prompt": prompts.RESEARCHER,
        "room": {
            "tagline": "What the research actually says",
            "blurb": (
                "Ask about a condition, a treatment, a procedure, or a word nobody has "
                "explained yet. You get what the published guidelines and studies say, in "
                "plain English, with a source behind every point."
            ),
            "covers": [
                "What a condition is",
                "Treatment options",
                "What a test measures",
                "How strong the evidence is",
                "Questions worth asking",
            ],
            "examples": [
                "What is stage 3 kidney disease, in plain English?",
                "What do the studies actually show about statins for someone my age?",
                "My letter says 'ejection fraction 35%' — what does that measure?",
            ],
        },
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
    "genomics": {
        "display_name": "Genomics & Biomarkers",
        "color": "#4F5D96",     # deep indigo — the one unoccupied hue band
        "system_prompt": prompts.GENOMICS,
        # Deliberately "explains the words", never "reads your result". The prompt
        # forbids inferring germline vs somatic, predicting benefit, or giving a
        # threshold verdict, so the card must not imply any of them.
        "room": {
            "tagline": "Making sense of the words on your report",
            "blurb": (
                "Bring the terms and numbers from a genetic, genomic, or biomarker report "
                "and have them explained in plain English. This room explains what they "
                "mean in general — it does not read your own result or say what to do "
                "about it, which is your care team's and a genetic counsellor's job."
            ),
            "covers": [
                "Gene and variant names",
                "What 'uncertain significance' means",
                "Biomarker numbers",
                "Germline vs tumour tests",
                "Seeing a genetic counsellor",
            ],
            "examples": [
                "My report says 'BRCA2 variant of uncertain significance' — what is that?",
                "What is TMB, and what does the number actually measure?",
                "What's the difference between a germline test and a tumour test?",
            ],
        },
        # `genomics_lookup` is the patient-language layer; the base tools cover the
        # literature and patient-facing explanation pages. Deliberately NO
        # clinical_trials_search: that would double-hit the registry (the memo is
        # per-agent-run) and invite this agent into eligibility language. Deferring
        # to the trials agent is the mechanism, per COMMON_PREFIX's "stay in your
        # lane" rule.
        "allowed_tools": PATIENT_BASE_TOOLS | {"genomics_lookup"},
        # Broad on purpose: _apply_bias ORs the terms, so more terms widen rather
        # than narrow, and the search retries unbiased when a bias starves the
        # query. Spans oncology AND non-oncology genomics (pharmacogenomics,
        # predisposition) because this app serves every condition.
        "pubmed_bias": {
            "mesh_terms": [
                "Biomarkers, Tumor",
                "Genetic Testing",
                "Genetic Predisposition to Disease",
                "Molecular Targeted Therapy",
                "High-Throughput Nucleotide Sequencing",
                "Microsatellite Instability",
                "Pharmacogenomic Testing",
                "Genetic Counseling",
                "Mutation",
            ]
        },
        "trusted_sources": _GENOMIC_PATIENT + _CONDITION_ORGS[:8] + _ACADEMIC_PATIENT_PAGES,
        # HARD citation gate — no `soft_citation_gate` key. The soft gate exists for
        # agents whose value is naming real programs and lived experience
        # (navigator, stories, trials). Genomics is the inverse: its entire value is
        # that a classification traces to a source, and an LLM's latent knowledge
        # about BRCA and EGFR is fluent, abundant, and exactly what must not reach a
        # patient uncited.
        "citation_required": True,
        "conditional": False,
        # Gene reference pages are long; the default 1800-char cap truncates a
        # MedlinePlus Genetics page mid-explanation.
        "result_char_cap": 3000,
    },
    "physio": {
        "display_name": "Physiotherapist",
        "color": "#4A7C6F",     # sage
        "system_prompt": prompts.PHYSIO,
        "room": {
            "tagline": "Moving better, safely",
            "blurb": (
                "For pain, weakness, balance and getting back on your feet after surgery, a "
                "stroke, or a long stretch of illness. Explains what rehab usually involves "
                "and how to get referred; the actual programme comes from a physiotherapist "
                "who can examine you."
            ),
            "covers": [
                "Recovering after surgery",
                "Balance and falls",
                "Joint and back pain",
                "Limb swelling",
                "Cardiac and pulmonary rehab",
            ],
            "examples": [
                "I've been in hospital three weeks and my legs feel useless. Where do I start?",
                "What actually happens in cardiac rehab?",
                "My knee hurts going down stairs — what usually helps?",
            ],
        },
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
        "room": {
            "tagline": "Getting active again, at your pace",
            "blurb": (
                "For building fitness and strength when you're medically steady, and for how "
                "activity affects your condition, your medicines and your blood sugar. "
                "Covers where to start, how to build up, and the signs that mean stop."
            ),
            "covers": [
                "Starting from almost nothing",
                "How much and how often",
                "Exercise and blood sugar",
                "Pacing on low-energy days",
                "When to stop and get help",
            ],
            "examples": [
                "I'm out of breath walking to the shop. How do I build up from here?",
                "Is it safe to lift weights with high blood pressure?",
                "How does exercise change my blood sugar during the day?",
            ],
        },
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
        "room": {
            "tagline": "Food that works with your condition",
            "blurb": (
                "For eating with your condition and eating through treatment side effects — "
                "salt, carbs, protein, appetite, taste changes, weight. Your personal "
                "targets need a dietitian who has your blood results; this room explains "
                "what the guidance says and what to ask for."
            ),
            "covers": [
                "Salt, carbs and protein",
                "Appetite and weight loss",
                "Taste changes and sore mouth",
                "Supplements and interactions",
                "Practical meal ideas",
            ],
            "examples": [
                "How much salt is too much with heart failure?",
                "Nothing tastes right since chemo started — what can I actually eat?",
                "Should I be watching potassium with kidney disease?",
            ],
        },
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
        "room": {
            "tagline": "Swallowing, voice, and finding words",
            "blurb": (
                "For coughing or choking on food and drink, food that sticks, a hoarse or "
                "lost voice, and trouble speaking or finding words. Explains the warning "
                "signs, what helps day to day, and how to get properly assessed."
            ),
            "covers": [
                "Coughing when you eat or drink",
                "Food getting stuck",
                "A hoarse or quiet voice",
                "Word-finding after a stroke",
                "Getting a swallow assessment",
            ],
            "examples": [
                "I keep coughing when I drink water. Should I be worried?",
                "My voice has gone quiet since my Parkinson's diagnosis — what helps?",
                "How do I get a swallowing assessment arranged?",
            ],
        },
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
        "room": {
            "tagline": "The hard feelings that come with this",
            "blurb": (
                "For worry, low mood, sleep, scan anxiety and fear about what comes next — "
                "your own, or the person you care for. Covers coping approaches that have "
                "been studied and how to find a professional when you want one."
            ),
            "covers": [
                "Worry and low mood",
                "Sleep problems",
                "Fear of it coming back",
                "Talking to family",
                "Finding a therapist",
            ],
            "examples": [
                "I can't sleep the week before every scan. What helps?",
                "Is it normal to feel this flat now that treatment has finished?",
                "How do I tell my children what's going on?",
            ],
        },
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
        # prompts.TRIALS forbids "you qualify" / "you're a good fit" / benefit
        # prediction. The card carries the same rule: it may promise a LIST and an
        # explanation, never a verdict on whether this patient can join.
        "room": {
            "tagline": "What trials exist, and what joining means",
            "blurb": (
                "Searches the public trial registry for studies in your condition and your "
                "part of the world, and explains what each one is testing, what the phase "
                "means, and what taking part involves. Whether anyone can join is decided "
                "by the trial team after they check the records — never here."
            ),
            "covers": [
                "Finding listed studies",
                "What the phases mean",
                "Where the sites are",
                "What taking part involves",
                "Questions for your specialist",
            ],
            "examples": [
                "Are there any trials listed in the UK for my type of lung cancer?",
                "What does a phase 2 trial actually involve week to week?",
                "What should I ask my specialist about trials?",
            ],
        },
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
        # "you would need to confirm eligibility", never "you qualify" — the same
        # line the prompt draws.
        "room": {
            "tagline": "Money, work, transport, and paperwork",
            "blurb": (
                "For the practical side of being ill — help with costs, getting to "
                "appointments, sick pay and work rights, benefits, home help and support "
                "for carers. Each programme decides for itself who it can help; this room "
                "finds them and explains how to apply."
            ),
            "covers": [
                "Help with costs",
                "Getting to appointments",
                "Work and sick pay",
                "Benefits and forms",
                "Support for carers",
            ],
            "examples": [
                "How do I get help paying for my medication?",
                "What are my rights at work while I'm having treatment?",
                "Is there any help with transport to dialysis?",
            ],
        },
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
        # `stories` is a researcher, so it is offered by /api/team and needs a room
        # like the rest. The prompt is explicit that this agent surfaces and labels
        # lived experience and gives no advice — the card says the same.
        "room": {
            "tagline": "Hear from people who've been here",
            "blurb": (
                "Finds written accounts and podcast episodes from people living with the "
                "same condition, from a curated set of patient-voice sources. These are "
                "other people's experiences, not advice, and not a prediction about yours."
            ),
            "covers": [
                "First-hand accounts",
                "Podcast episodes",
                "Newly diagnosed",
                "Living with it long term",
                "Carers' voices",
            ],
            "examples": [
                "I'd like to hear from someone who had the same operation.",
                "Are there stories from people living with MS for years?",
                "What was starting dialysis like for other people?",
            ],
        },
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
    "genomics":   "What your gene and biomarker test results mean",
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
    # Genomics sits second, immediately after the evidence: a molecular result
    # frames everything below it, and it must precede `trials`, because the marker
    # is what the trial list is selected on.
    "genomics",
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


def room_for(sid: str) -> dict:
    """The dashboard card for one specialist, always with every key present.

    Returns empty strings/lists rather than raising, so one missing `room` degrades
    to a bare card instead of a 500 on /api/team. The test suite is what actually
    stops a room going missing.
    """
    room = (SPECIALIST_CONFIGS.get(sid) or {}).get("room") or {}
    return {
        "tagline": str(room.get("tagline") or ""),
        "blurb": str(room.get("blurb") or ""),
        "covers": list(room.get("covers") or []),
        "examples": list(room.get("examples") or []),
    }


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
