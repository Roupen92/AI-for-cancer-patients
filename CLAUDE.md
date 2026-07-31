# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A patient-facing health chatbot for **any condition** (not just cancer — the directory name is
historical). It searches the same sources clinicians use — guidelines, PubMed, ClinicalTrials.gov,
patient organizations — and translates what it finds into plain language with a citation behind
every claim.

## Commands

```bash
./run.sh                                        # venv bootstrap + uvicorn on :8000 with --reload
.venv/bin/python -m pytest tests/ -q            # full suite: no network, no LLM, ~0.5s
.venv/bin/python -m pytest tests/test_smoke.py::test_name -q      # single test
.venv/bin/python -m pytest tests/ -q -k safety                    # by keyword
```

There is a working `.venv` (Python 3.12) — use `.venv/bin/python`, not bare `python3`.

Eval harness (slow, hits live LLM + search APIs):

```bash
.venv/bin/python -m tests.eval.loop_eval capture --name haplo   # real board run -> fixture
.venv/bin/python -m tests.eval.loop_eval iterate --name haplo   # re-synth from fixture + judge
.venv/bin/python -m tests.eval.loop_eval batch                  # Reddit-derived cases + judge
```

`fixtures/` and `runs/` are gitignored; `capture` regenerates them. Existing fixtures predate the
router rework, so their frozen synthesizer input uses the older format.

Deployment is Railway, GitHub-connected to `main` — **merging to `main` and pushing IS the deploy**.

```bash
railway logs
railway variables --service AI-for-cancer-patients
curl <url>/api/health?probe=1                   # what actually works, not just what's configured
```

## Architecture

### The routing decision is the core of the system

`app/router.py` makes one cheap LLM JSON call per patient message and returns a mode plus a
per-agent focus brief. Everything downstream follows from it:

```
patient message
  ├─ app/safety.py         deterministic regex screen — runs BEFORE any LLM call
  └─ app/router.py         mode + which specialists + focus for each
       ├─ clarify → one question back, no research at all
       ├─ reply   → 1 specialist              → plain-language pass
       └─ team    → 2-4 in parallel → synthesis → institution gloss → plain-language pass
                                                     └─ translation (if not English)
```

`app/chat.py` orchestrates a turn; `app/board.py` owns the specialist round and every
post-processing pass. `board.run_consult()` takes the roster as a parameter — the router supplies
it. `board.run_board()` is the un-routed entry point (the `/api/board` endpoint and the eval
harness) and uses the standing `config.FULL_CONSULT_IDS` roster instead.

Everything the router returns is validated and clamped in `_coerce_specialists`. A router failure
degrades to the generalist rather than breaking the turn — `route()` never raises.

### Specialists are config, not code

`app/config.py: SPECIALIST_CONFIGS` is the catalogue (9 researchers + a post-synthesis translator).
Adding an agent means adding an entry there, a prompt in `app/prompts.py`, and entries in
`SECTION_HEADINGS` + `SECTION_ORDER`. A test enforces that last part, because a missing heading
silently drops the agent's section from every summary.

`board._sections_block()` generates a `SECTIONS TO WRITE` contract from the agents that actually
produced drafts, so the summary's shape follows the roster rather than a hardcoded outline. An
agent that skipped has no line there and therefore no heading — that is what stops the synthesizer
inventing placeholder sections.

### Evidence ledger is per-conversation

One `EvidenceLedger` lives on the `Conversation`, not the turn, so `[3]` means the same source in
message ten as in message two. Re-retrieving a URL reuses its label. Only entries some specialist
actually **cited** reach the UI; retrieved-but-uncited stay in the ledger for LLM context.

### Turn events are an append-only log, not a queue

`sessions.Turn.events` plus an index per reader. A late-connecting or reconnecting SSE client
replays from index 0 and sees the whole turn. Do not convert this back to a consumable queue: the
emit path drops some event types under load, which desyncs any skip-N scheme.

## Invariants that are easy to break

**Safety.** `app/safety.py` runs before any model call and is ORed with the router's screen; ties go
to "warn". `emergency_markdown()` is assembled in Python and prepended **after** every LLM pass, so
no model can reword or drop it. `clean_patient_phrase()` guards the router's `why`/`action`, which
are shown to the patient verbatim — an LLM drifts into third-person clinical notes there.

**The plain-language pass is verified mechanically.** `board._plain_language()` compares citation
labels and clinical numbers before/after and keeps the original if either was lost. Its failure mode
is "helpfully" turning `1.2-1.5 g protein per kg per day` into `enough protein`, which destroys the
entire value of the answer. Do not relax this check.

**Search order is Perplexity first, Brave as fallback** (`app/tools/_perplexity.py`, then the Brave
path in each tool). Brave truncates to 6 `site:` clauses, so put the highest-value domains first in
any allowlist. Keyless sources (PubMed, Europe PMC, Semantic Scholar, ClinicalTrials.gov) always work.

**Silent search failure is the worst failure mode.** With no working search backend every specialist
retrieves nothing, the citation gate does its job, and every agent honestly abstains — the patient
gets "I couldn't find anything" for every question and nothing says a key expired. That is why
`app/health.py` and `/api/health?probe=1` exist. `search.redundant: false` means one lapsed key from
that state.

**`pubmed._apply_bias` reads `bias["mesh_terms"]`.** Config once said `"mesh"`, which silently
disabled specialty biasing everywhere. It now accepts either, and a test asserts config and tool agree.

**OpenRouter checks affordability against `max_tokens` before routing.** Unset means the model's full
output budget and a 402 on a small balance — `llm.chat()` caps it for OpenRouter only.

**Env prefixes:** `CANCERPATIENT_*` and `MEDBOARD_*` are both accepted (an `.env` copied from the AI
Tumor Board works unchanged). Don't break the fallback.

**Soft citation gates** on `navigator`, `trials`, `stories` — their value is naming real programs,
studies and lived experience, not citing PubMed. The clinical agents keep the hard gate.

**`trials` must never state eligibility.** `prompts.TRIALS` forbids "you qualify" / benefit
prediction; the tool surfaces burdens (placebo arms, site distance) rather than hiding them.

### Front-end

`static/shared.js` holds agent visuals, markdown + citation rendering and the tooltip, shared by the
chat (`app.js`) and the one-shot consult (`consult.js`) so the two cannot drift.

- `[hidden] { display: none !important; }` **must stay last in `styles.css`**. Any class rule that
  sets `display` outranks the user-agent `[hidden]` rule and silently reveals hidden markup.
- `renderMarkdown` stamps `dir="auto"` on **top-level blocks only**. The `dir=auto` algorithm skips
  descendants carrying their own `dir`, so stamping nested nodes blinds their parent — `dir` on
  `<li>` makes the `<ul>` fall back to `ltr`. RTL layout uses logical properties
  (`margin-inline-start`); a `[dir="rtl"]` selector never matches because the value is literally `auto`.
- Reference URLs come from third-party search results and are rendered as clickable anchors outside
  DOMPurify's reach — they go through `safeUrl()` (absolute http(s) only).

## Gotchas when testing

- `load_dotenv()` resolves from the **calling file's** directory, not the CWD. A throwaway script in
  a temp dir reports "no API key" while the app is fine. Put test scripts in the repo or pass an
  explicit path.
- Backgrounding uvicorn inside a `(… &)` subshell kills it when the parent shell call times out.
- LLM behaviour varies run to run: a specialist can abstain once and answer well on retry (hence the
  targeted retry in `chat._run_reply`), and borderline red flags fire inconsistently by design.
