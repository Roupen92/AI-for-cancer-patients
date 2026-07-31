# Patient Guide — plain-language answers from real medical sources

A chatbot for patients and caregivers, for **any** condition — cancer, heart failure,
diabetes, kidney disease, stroke, Parkinson's, COPD, IBD, long COVID, chronic pain, or a
diagnosis nobody has explained yet.

The premise: your doctors have clinical guidelines, PubMed, and the clinical trial
registry. Those are all public — but they're written for clinicians. This puts an AI
layer in between that searches the same sources and translates what it finds into the
patient's own language, at a reading level anyone can follow, with a link behind every
claim.

**This is not medical advice.** Always talk to your care team before making any decisions.

## How a turn works

Ask a question in plain words. A **router** reads it and spins up only the helpers that
are actually relevant — usually one, sometimes a small team:

```
patient message
   ├─ safety screen (regex, no LLM)  → emergency / crisis block, prepended verbatim
   └─ router (1 LLM call)            → mode + which specialists + a focus brief for each
        ├─ clarify → one short question back, no research
        ├─ reply   → 1 specialist  → plain-language pass                      (~60-100s)
        └─ team    → 2-4 in parallel → synthesis → institution gloss
                                      → plain-language pass                   (~3-6 min)
                                                        └─ translation (if not English)
```

A question about salt in heart failure spins one dietitian. "I was just diagnosed and I'm
overwhelmed" spins four. The old behaviour — every agent on every question — is still
available at `/consult` for people who want to write out their whole situation once and
get a full printable summary.

## The helpers

| id | Helper | Covers |
|---|---|---|
| `researcher` | **Medical Research** | The default. Conditions, tests, treatments, what the evidence says *and how strong it is* |
| `physio` | **Physiotherapist** | Rehab, pain, balance, falls, post-op and post-stroke recovery, lymphedema, cardiac/pulmonary rehab |
| `exercise` | **Exercise & Activity** | Getting fitter safely with a long-term condition (conditioning, not rehab) |
| `dietician` | **Dietitian** | Salt, fluid, carbs, potassium, protein, appetite, eating through side effects, food-drug interactions |
| `slp` | **Speech & Swallowing** | *Conditional*: swallowing, voice, speech, language — stroke, Parkinson's, dementia, MND, head/neck and esophageal cancer |
| `mental` | **Emotional Wellbeing** | Distress, anxiety, low mood, sleep, fear of progression, caregiver strain, crisis routing |
| `trials` | **Clinical Trials** | Searches ClinicalTrials.gov for open studies — never states eligibility |
| `navigator` | **Patient Navigator** | Money, insurance, drug costs, transport, work rights, benefits, home help — country-aware |
| `stories` | **Stories from Others** | First-person accounts from curated patient-voice archives |
| `translator` | **Translator** | Post-synthesis pass into the patient's language, medical terms preserved in English |

## Safety design

- **Deterministic red-flag screen** (`app/safety.py`) runs on every message *before any
  LLM call* — regex, no network, no failure mode. It catches chest pain, stroke signs,
  breathing trouble, bleeding, neutropenic fever, anaphylaxis, and suicidal/passive
  ideation. The router screens too, and the two are ORed; ties go to "warn". The emergency
  block is assembled in Python and prepended **after** every LLM pass, so no model can
  reword, soften, or drop it.
- **Hard scope-of-practice rules** in `COMMON_PREFIX`: no diagnosing, no prescribing, no
  dose recommendations, no "you qualify".
- **Evidence-only rule**: every clinical claim must cite a retrieved source `[N]`. Drafts
  with zero citations are forced to abstain rather than answer from model memory.
- **Specificity gate** (three tiers): a specific protocol is used verbatim; general
  guidance must admit it lacks numbers *and* name who to ask; only genuine retrieval
  failure abstains. "Do aerobic exercise" is a rejected answer.
- **The plain-language pass is verified mechanically.** Its failure mode is helpfully
  flattening "1.2–1.5 g protein per kg per day" into "enough protein", so the output is
  checked for citation and number loss and the original is kept if either dropped.
- **Trials never imply eligibility.** The prompt forbids "you qualify" / "good candidate"
  / benefit prediction, and the tool surfaces burdens (placebo arms, site distance) rather
  than hiding them.
- **Country matching**: the Navigator extracts the country before recommending programs,
  so a UK patient never gets told about FMLA.
- **Translator preserves English medical terms in parentheses** so a patient can match
  what they read to their hospital chart, plus a local-script gloss for non-Latin scripts.

## Architecture

FastAPI + SSE + asyncio, over any OpenAI-compatible provider (OpenRouter / Gemini /
OpenAI). Originally forked from the [AI Tumor Board](../AI%20tumor%20Board/) clinician
tool and rebuilt for patients.

- **Dynamic roster.** `app/router.py` returns the specialist set per message; `board.run_consult`
  takes it as a parameter. The synthesizer receives a generated `SECTIONS TO WRITE` block,
  so the summary's shape follows the roster instead of a hardcoded outline.
- **One evidence ledger per conversation.** `[3]` means the same source in the tenth
  message as in the second. Re-retrieving a URL reuses its label.
- **Replayable turn streams.** A turn's events go to an append-only log and readers track
  their own index, so a late connection or a dropped SSE connection replays the whole turn
  instead of losing the start of it.
- **Single round** of parallel specialists — no judge, no multi-round consensus.
- **Soft citation gates** for `navigator`, `trials`, and `stories`: their value is naming
  real programs, studies, and lived experience, not citing PubMed.

## Run it

```bash
cp .env.example .env
# Edit .env: set one LLM key + one search key
./run.sh
```

Then open http://localhost:8000 for the chat, or /consult for the full-team form.

### Keys

One LLM provider (auto-detected from whichever key is present — OpenRouter, then Gemini,
then OpenAI):

- `OPENROUTER_API_KEY` — https://openrouter.ai/keys (default model `z-ai/glm-5.2`)
- `GEMINI_API_KEY` — https://aistudio.google.com/apikey
- `OPENAI_API_KEY`

One search backend (Perplexity is tried first, Brave is the fallback):

- `PERPLEXITY_API_KEY`
- `Brave_API` — https://brave.com/search/api/ (2k queries/mo free)

PubMed, Europe PMC, Semantic Scholar and ClinicalTrials.gov need no key.

### Optional

- `NCBI_EMAIL` + `NCBI_API_KEY` — raises the PubMed rate limit
- `CANCERPATIENT_MODEL` / `CANCERPATIENT_PROVIDER` — override model or provider
  (`MEDBOARD_*` is accepted too, so an .env copied from the Tumor Board works unchanged)
- `CANCERPATIENT_REASONING_EFFORT` — `none|low|medium|high` (default `high`; `low` is much
  faster and is what the tuned setup uses)
- `CANCERPATIENT_MAX_TOKENS` — output cap, needed on OpenRouter (default 16384)
- `CANCERPATIENT_MAX_ACTIVE_TURNS`, `CANCERPATIENT_MAX_ACTIVE_SESSIONS` — concurrency caps
- `CANCERPATIENT_RATE_PER_HOUR` (default 20), `CANCERPATIENT_RATE_PER_DAY` (default 60) —
  per-IP limits on the endpoints that spend credits. Set both to 0 to disable.
  Deliberately generous: a clinic, library or care home can put many genuine
  patients behind one address, so the defaults throttle scripts, not waiting rooms.
- `CANCERPATIENT_ALLOWED_ORIGINS` — comma-separated CORS allowlist

## API

```
POST   /api/chat                                  {message, conversation_id?, profile?} → {conversation_id, turn_id}
GET    /api/chat/{cid}/turns/{tid}/stream         SSE: routed, specialist_event, phase, turn_complete
GET    /api/chat/{cid}                            full transcript + references
DELETE /api/chat/{cid}/turns/{tid}                cancel a turn
GET    /api/chat/{cid}/lay_summary/{label}        plain-English rewrite of one citation
GET    /api/team                                  the specialist catalogue
GET    /api/health                                which backends are configured (free)
GET    /api/health?probe=1                        which backends actually work (costs a call)
POST   /api/board                                 one-shot full consult (original API)
```

### Check your backends before you trust an answer

The app's worst failure mode is silent. If the search key lapses, every
specialist retrieves nothing, the citation gate does its job, and every agent
honestly abstains — so a patient gets "I couldn't find anything" for every
question and nothing says the credential is dead. It looks like a bad product
rather than an expired token.

```bash
curl localhost:8000/api/health?probe=1
```

`search.redundant: false` means you are one lapsed key away from that state.
PubMed, Europe PMC, Semantic Scholar and ClinicalTrials.gov keep working
without any credential, but the patient-facing plain-language sources go dark.

## Deploy

`railpack.json` and `Procfile` are configured for Railway.

## File map

```
app/
  server.py        - FastAPI app, chat + consult SSE endpoints
  chat.py          - chat turn orchestration (clarify / reply / team)
  router.py        - triage: which specialists, what focus, red flags
  safety.py        - deterministic red-flag screen (no LLM)
  board.py         - parallel specialists → synthesis → gloss → plain language → translate
  specialist.py    - per-agent tool loop, self-check, citation gate
  config.py        - specialist catalogue, source allowlists, section headings
  prompts.py       - COMMON_PREFIX + 9 specialist prompts + router/synth/gloss/plain-language
  evidence.py      - in-memory ledger, [N] labels, dedup by (kind, id)
  llm.py           - provider-agnostic client wrapper with retry
  sessions.py      - conversations, turns, one-shot sessions, TTL reaper
  language.py      - LLM-backed location & language helpers
  tools/
    pubmed.py, europe_pmc.py, semantic_scholar.py, brave_search.py, _perplexity.py
    patient_source_search.py     - curated allowlist search across trusted patient sites
    social_resource_search.py    - country-keyed benefit/assistance directories
    patient_stories_search.py    - patient-voice narratives + podcasts, any condition
    clinical_trials.py           - ClinicalTrials.gov v2, location-aware, patient-safe

static/
  index.html,  app.js       - the chat
  consult.html, consult.js  - the one-shot full consult
  shared.js                 - agent visuals, markdown + citation rendering, tooltips
  styles.css, about.html, privacy.html

tests/
  test_smoke.py    - config coherence, safety screen, router validation, board passes
  eval/            - LLM-judge harness for synthesis quality (see tests/eval/loop_eval.py)
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q          # fast, no network, no LLM
.venv/bin/python -m tests.eval.loop_eval batch   # LLM-judge eval (slow, hits live APIs)
```

## License

Licensed under the [Apache License 2.0](LICENSE). Free to use, study, modify, and
build on — including commercially — provided you keep the license and attribution.

Source code: https://github.com/Roupen92/AI-for-cancer-patients
