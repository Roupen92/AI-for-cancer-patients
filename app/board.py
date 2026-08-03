"""Care-team orchestrator: one round of parallel specialists, synthesizer,
institution-gloss pass, plain-language pass, then translation. No judge, no
multi-round adversarial loop.

The roster is supplied by the caller (app/router.py picks it per patient
message). `run_board` keeps the original signature — full roster, free-text case
— for the eval harness and the one-shot consult endpoint; `run_consult` is the
parameterized entry point the chat path uses.
"""
import asyncio
import logging
import re
import time
from typing import Callable

from app import llm, language, logsafe, prompts
from app.config import (
    FULL_CONSULT_IDS,
    PARALLEL_SPECIALISTS,
    SECTION_HEADINGS,
    SPECIALIST_CONFIGS,
    order_specialists,
    public_specialist_info,
    researcher_ids,
)
from app.evidence import EvidenceLedger
from app.specialist import SpecialistResult, run_specialist

log = logging.getLogger(__name__)


# Regex pre-filter for SLP relevance, used only on the un-routed path (the
# one-shot consult, where nothing has triaged the case yet). On the chat path the
# router decides, and the LLM SKIP marker inside specialist.py is the backstop
# either way.
# Prefix-style: trailing word boundary intentionally omitted so "laryng" matches
# "laryngeal", "dysphag" matches "dysphagia", "glioblastom" matches "glioblastoma".
_SLP_KEYWORDS = re.compile(
    r"\b("
    # Head, neck, and airway
    r"head\s+and\s+neck|"
    r"orophar|hypophar|nasophar|larynx|laryng|"
    r"esophag|oesophag|"
    r"glossectom|tongue\s+cancer|oral\s+(cavity|cancer)|"
    r"thyroid\s+cancer|"
    r"vocal\s+cord|voice\s+box|tracheostom|intubat|"
    # Brain and nerves
    r"brain\s+tumor|brain\s+tumour|glioma|glioblastom|meningiom|"
    r"strok|tia\b|aphasi|dysarthri|"
    r"parkinson|multiple\s+sclerosis|\bals\b|motor\s+neuron|huntington|dementia|alzheim|"
    r"head\s+injury|traumatic\s+brain|"
    # Symptoms
    r"swallow|dysphag|choking|aspirat|"
    r"hoarse|voice\s+(change|loss|problem)|"
    r"speech\s+(problem|issue|change|loss)|slurred|"
    r"word[- ]finding|trouble\s+(speaking|talking|finding\s+words)"
    r")",
    re.IGNORECASE,
)


def _slp_relevant(case: str) -> bool:
    return bool(_SLP_KEYWORDS.search(case or ""))


def _summary_or_skip(res: SpecialistResult) -> str:
    if res.status == "skipped":
        return "(skipped — not applicable to this case)"
    if res.status == "no_evidence":
        return "(could not find trustworthy sources — please ask your care team)"
    if res.status == "error":
        return f"(error: {res.error})"
    return res.recommendation_summary


def _sections_block(active_ids: list[str], history: dict[str, SpecialistResult]) -> str:
    """The `SECTIONS TO WRITE` contract handed to the synthesizer.

    Built from the specialists that actually produced usable drafts, in
    SECTION_ORDER. This is what makes the summary shape follow the roster instead
    of a hardcoded six-section cancer outline: an agent that wasn't picked, or
    that skipped, simply has no line here and therefore no heading in the output.
    """
    lines = []
    for sid in order_specialists(active_ids):
        res = history.get(sid)
        if not res or res.status == "skipped":
            continue
        heading = SECTION_HEADINGS.get(sid, SPECIALIST_CONFIGS[sid]["display_name"])
        lines.append(f"  - `## {heading}`  ← from the {SPECIALIST_CONFIGS[sid]['display_name']} draft ({sid})")
    if not lines:
        return (
            "SECTIONS TO WRITE: (none — no specialist produced a draft; say so honestly "
            "in one short paragraph and tell the patient to ask their care team)"
        )
    return (
        "SECTIONS TO WRITE (exactly these, in this order, using these headings verbatim; "
        "do not add sections, do not rename headings):\n" + "\n".join(lines)
    )


def _synthesize_final(
    history: dict[str, SpecialistResult],
    case_raw: str,
    location_raw: str,
    location_parsed: dict,
    preferences: str,
    ledger: EvidenceLedger,
    active_ids: list[str] | None = None,
    *,
    conversation_context: str = "",
) -> str:
    """Single LLM call → English markdown summary, section-per-specialist.

    The user_content is structured as labeled blocks (PATIENT FACTS, SECTIONS TO
    WRITE, SPECIALIST DRAFTS, CITED EVIDENCE) so the synthesizer can ctrl-F for
    tokens before writing — closes the loophole where it would paraphrase patient
    prose and invent plausible-but-wrong facts (e.g., 'Boston' instead of Toronto).
    """
    ids = order_specialists(active_ids if active_ids is not None else researcher_ids())

    drafts = []
    for sid in ids:
        res = history.get(sid)
        if not res:
            continue
        if res.status == "skipped":
            # Skipped agents are OMITTED ENTIRELY from the input so the synthesizer
            # cannot hallucinate a placeholder section. The prompt's "if not in
            # input, omit heading" rule depends on this.
            continue
        name = SPECIALIST_CONFIGS[sid]["display_name"]
        labels = ", ".join(res.evidence_labels) if res.evidence_labels else "(none)"
        body = res.draft_markdown.strip() or res.recommendation_summary
        drafts.append(
            f"--- {name} ({sid}) ---\n"
            f"Status: {res.status}\n"
            f"Evidence labels used: {labels}\n\n"
            f"{body}"
        )

    evidence_blocks = []
    for entry in ledger.public_list():
        header_bits = [f"[{entry['label']}]", entry.get("title", "").strip() or "(no title)"]
        meta_bits = []
        if entry.get("journal"):
            meta_bits.append(entry["journal"])
        if entry.get("year"):
            meta_bits.append(str(entry["year"]))
        if entry.get("article_type"):
            meta_bits.append(entry["article_type"])
        header = " ".join(header_bits)
        if meta_bits:
            header += " · " + " · ".join(meta_bits)
        summary = (entry.get("summary") or "").strip()
        evidence_blocks.append(f"{header}\n{summary}" if summary else header)
    cited_evidence = "\n\n".join(evidence_blocks) if evidence_blocks else "(no cited evidence)"

    # Build a structured PATIENT FACTS block as DATA, not prose. This lets the
    # synthesizer ctrl-F real tokens (e.g., "Toronto") instead of paraphrasing.
    facts_lines = [
        "PATIENT FACTS (verbatim — do not embellish, do not add facts not present):",
        f"  Free-text case: {case_raw.strip() or '(none provided)'}",
        f"  Location (free text): {location_raw.strip() or '(none provided)'}",
    ]
    loc_country = (location_parsed or {}).get("country", "").strip()
    if loc_country:
        loc_region = (location_parsed or {}).get("region", "").strip()
        loc_city = (location_parsed or {}).get("city", "").strip()
        loc_conf = (location_parsed or {}).get("confidence", "low")
        parts = [p for p in (loc_city, loc_region, loc_country) if p]
        facts_lines.append(
            f"  Location (parsed): city={loc_city or '(unknown)'}, "
            f"region={loc_region or '(unknown)'}, country={loc_country}, "
            f"confidence={loc_conf}  →  rendered as: {', '.join(parts)}"
        )
    else:
        facts_lines.append("  Location (parsed): (could not extract — country unknown)")
    facts_lines.append(
        f"  Preferences (diet/movement/limits): {preferences.strip() or '(none provided)'}"
    )

    context_block = ""
    if conversation_context.strip():
        context_block = (
            "\n\nEARLIER IN THIS CONVERSATION (context only — these are facts the patient "
            "already gave you and answers they already received; do not repeat the answers, "
            "and do not treat anything here as a new clinical claim):\n"
            + conversation_context.strip()
        )

    user_content = (
        "\n".join(facts_lines)
        + context_block
        + "\n\n"
        + _sections_block(ids, history)
        + "\n\nSPECIALIST DRAFTS (only specialists listed here should appear as sections in your output):\n\n"
        + ("\n\n".join(drafts) if drafts else "(no specialists produced drafts)")
        + "\n\nCITED EVIDENCE (use these when reproducing claims; preserve [N] labels):\n\n"
        + cited_evidence
    )
    messages = [
        {"role": "system", "content": prompts.SYNTHESIZER},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = llm.chat(messages, tools=None)
        return resp.choices[0].message.content or "(synthesis returned empty content)"
    except llm.QuotaExceeded as e:
        log.warning("Synthesizer hit LLM quota: %s", e)
        return (
            "## We couldn't finish your summary\n\n"
            "The AI service ran out of credits while putting your summary together. "
            "Please try again in a few minutes.\n\n"
            "**This is general information from public sources. It is not medical "
            "advice. Always talk to your care team.**"
        )
    except Exception as e:
        log.exception("Synthesizer failed.")
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:197] + "…"
        return f"## Something went wrong assembling your summary\n\n`{msg}`"


# --------------------------------------------------------------------------- #
# Post-synthesis safety / readability passes
# --------------------------------------------------------------------------- #

_CITE_RE = re.compile(r"\[(\d{1,3})\]")
# Numbers that carry clinical meaning. Citation labels are stripped before this
# runs, so what's left is doses, durations, targets, and counts.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _citation_labels(md: str) -> set[str]:
    return set(_CITE_RE.findall(md or ""))


def _clinical_numbers(md: str) -> set[str]:
    stripped = _CITE_RE.sub(" ", md or "")
    return set(_NUMBER_RE.findall(stripped))


def _gloss_institutions(english_md: str) -> str:
    """Post-synthesis safety pass: guarantee every organization named in the
    summary is identified in plain English for the patient (e.g., "NICE" ->
    "NICE, the body that writes treatment guidance for the UK's health service").

    Inserts ONLY institution names/descriptions; everything else (claims, [N]
    labels, numbers, URLs, structure) is preserved. Falls back to the input on
    any error, and refuses an implausibly short result, so it can never gut or
    break the summary.
    """
    if not (english_md or "").strip():
        return english_md
    messages = [
        {"role": "system", "content": prompts.INSTITUTION_GLOSSARY},
        {"role": "user", "content": english_md},
    ]
    try:
        # Run THIS call at high reasoning effort regardless of the pipeline default:
        # it is a single cheap pass whose entire job is exhaustive coverage, and at
        # low effort it intermittently misses one institution (e.g. an agency named
        # in full mid-sentence). High effort makes the guarantee reliable.
        resp = llm.chat(messages, tools=None, reasoning_effort="high")
        out = (resp.choices[0].message.content or "").strip()
        # The pass only ADDS institution descriptions, so the output should never
        # be shorter than the input. A much-shorter result means a refusal or a
        # truncation — keep the original rather than ship a gutted summary.
        if len(out) < int(0.9 * len(english_md.strip())):
            log.warning(
                "Institution gloss pass returned a suspiciously short doc "
                "(%d vs %d chars); keeping original.", len(out), len(english_md.strip())
            )
            return english_md
        return out
    except llm.QuotaExceeded as e:
        log.warning("Institution gloss pass hit LLM quota: %s", e)
        return english_md
    except Exception:
        log.exception("Institution gloss pass failed; returning un-glossed summary.")
        return english_md


def _plain_language(md: str) -> str:
    """Final readability pass: shorten sentences, gloss jargon, anchor numbers.

    This is the pass most likely to do harm by being helpful — the failure mode is
    "simplifying" `1.2 to 1.5 g of protein per kg per day` into `enough protein`,
    which destroys the entire value of the answer. So the result is verified
    mechanically before it's accepted:
      * every `[N]` citation in the input must still be present, and
      * the clinical numbers must survive (a small tolerance covers legitimate
        rewrites like "3 times per week" → "three times a week").
    If either check fails, the original is returned unchanged.
    """
    src = (md or "").strip()
    if not src:
        return md

    messages = [
        {"role": "system", "content": prompts.PLAIN_LANGUAGE},
        {"role": "user", "content": src},
    ]
    try:
        resp = llm.chat(messages, tools=None)
        out = (resp.choices[0].message.content or "").strip()
    except llm.QuotaExceeded as e:
        log.warning("Plain-language pass hit LLM quota: %s", e)
        return md
    except Exception:
        log.exception("Plain-language pass failed; returning the un-simplified draft.")
        return md

    if not out:
        return md

    if len(out) < int(0.6 * len(src)):
        log.warning(
            "Plain-language pass returned a suspiciously short doc (%d vs %d chars); "
            "keeping original.", len(out), len(src)
        )
        return md

    lost_cites = _citation_labels(src) - _citation_labels(out)
    if lost_cites:
        log.warning(
            "Plain-language pass dropped citations %s; keeping original.",
            sorted(lost_cites),
        )
        return md

    src_numbers = _clinical_numbers(src)
    if src_numbers:
        lost_numbers = src_numbers - _clinical_numbers(out)
        # Spelled-out small numbers ("three times a week") are a legitimate
        # simplification, so tolerate a few losses — but not wholesale flattening.
        if len(lost_numbers) > max(2, int(0.2 * len(src_numbers))):
            log.warning(
                "Plain-language pass dropped clinical numbers %s (of %d); keeping original.",
                sorted(lost_numbers)[:10], len(src_numbers),
            )
            return md

    return out


def _is_english(target_language: str) -> bool:
    t = (target_language or "").strip().lower()
    return t in ("", "english", "en", "en-us", "en-gb", "en-ca", "en-au")


def _translate(english_md: str, target_language: str) -> str:
    """Post-synthesis single LLM call → translated markdown. No tools, no retrieval."""
    if _is_english(target_language):
        return english_md
    messages = [
        {"role": "system", "content": prompts.TRANSLATOR},
        {
            "role": "user",
            "content": f"Target language: {target_language}\n\n---\n\n{english_md}",
        },
    ]
    try:
        resp = llm.chat(messages, tools=None)
        return resp.choices[0].message.content or english_md
    except llm.QuotaExceeded as e:
        log.warning("Translator hit LLM quota: %s", e)
        return english_md + (
            "\n\n---\n\n*We couldn't translate this summary because the AI service "
            "ran out of credits. The summary above is in English.*"
        )
    except Exception:
        log.exception("Translator failed; returning English.")
        return english_md


async def _lay_summarize_references(ledger: EvidenceLedger, emit) -> None:
    """For each CITED entry in the ledger, generate a 1-2 sentence plain-English
    summary and attach it via ledger.set_lay_summary. Runs in parallel with a
    small concurrency cap so it doesn't blast the LLM provider.

    Not called on the normal path — the frontend fetches these on demand when the
    patient actually hovers a citation. Kept for offline/batch use.
    """
    entries = [e for e in ledger.all() if e.cited_by]
    if not entries:
        return

    emit("phase", {"phase": "lay_summarizing", "count": len(entries)})

    sem = asyncio.Semaphore(6)

    async def _one(entry) -> None:
        async with sem:
            user_content = (
                f"TITLE: {entry.title or '(no title)'}\n"
                f"DOMAIN: {entry.journal or '(unknown)'}\n"
                f"YEAR: {entry.year or '(unknown)'}\n"
                f"SNIPPET: {(entry.summary or '(no snippet)')[:1500]}"
            )
            messages = [
                {"role": "system", "content": prompts.LAY_SUMMARY},
                {"role": "user", "content": user_content},
            ]
            try:
                resp = await asyncio.to_thread(llm.chat, messages, tools=None)
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    ledger.set_lay_summary(entry.label, text)
            except llm.QuotaExceeded:
                # Fall back to a short truncation of the original snippet — better
                # than no popup content at all.
                fallback = (entry.summary or "")[:200].strip()
                if fallback:
                    ledger.set_lay_summary(entry.label, fallback)
            except Exception:
                log.exception("Lay-summary failed for entry [%s]", entry.label)

    await asyncio.gather(*(_one(e) for e in entries))


def _build_timing_summary(timing: dict, total_s: float) -> dict:
    specs, llm_total, tool_total, llm_calls, tool_calls = [], 0.0, 0.0, 0, 0
    for sid, rec in timing["specialists"].items():
        llm_total += rec["llm"]
        tool_total += rec["tool"]
        llm_calls += rec["llm_n"]
        tool_calls += rec["tool_n"]
        specs.append({
            "id": sid,
            "display_name": SPECIALIST_CONFIGS[sid]["display_name"],
            "wall_s": round(rec["wall"], 1),
            "llm_s": round(rec["llm"], 1),
            "tool_s": round(rec["tool"], 1),
            "llm_calls": rec["llm_n"],
            "tool_calls": rec["tool_n"],
        })
    specs.sort(key=lambda s: s["wall_s"], reverse=True)
    tools = sorted(
        ({"name": n, "seconds": round(v["seconds"], 1), "calls": v["calls"]}
         for n, v in timing["tools"].items()),
        key=lambda t: t["seconds"], reverse=True,
    )
    post = (
        timing["synth"]
        + timing.get("gloss", 0.0)
        + timing.get("plain", 0.0)
        + timing["translate"]
    )
    return {
        "total_s": round(total_s, 1),
        "llm_s": round(llm_total + post, 1),
        "tool_s": round(tool_total, 1),
        "synth_s": round(timing["synth"], 1),
        "gloss_s": round(timing.get("gloss", 0.0), 1),
        "plain_s": round(timing.get("plain", 0.0), 1),
        "translate_s": round(timing["translate"], 1),
        "router_s": round(timing.get("router", 0.0), 1),
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "specialists": specs,
        "tools": tools,
    }


def log_timing(label: str, summary: dict, **extra) -> None:
    """One line per turn showing where the wall clock actually went.

    The timing dict was only ever streamed to the browser, which is useless for
    diagnosing a slow production turn — nobody has the SSE stream open when a
    patient complains. This puts the same breakdown in the server log, so
    `railway logs` answers "where did those 90 seconds go" directly.

    Agents run in parallel, so their seconds sum to more than the wall clock;
    `slowest` is the one that actually set the pace.

    Contains NO patient text — only opaque ids, durations and counts. It is still
    switchable (CANCERPATIENT_LOG_TIMING=0) so "record nothing about turns at
    all" is a single variable rather than a code change.
    """
    if not logsafe.LOG_TIMING:
        return

    specs = summary.get("specialists") or []
    slowest = max(specs, key=lambda s: s.get("wall_s", 0), default=None)
    bits = " ".join(
        f"{k}={summary.get(k + '_s', 0)}"
        for k in ("router", "synth", "gloss", "plain", "translate", "tool")
    )
    log.info(
        "TIMING %s total=%ss | %s | llm_calls=%s tool_calls=%s | agents=[%s]%s%s",
        label,
        summary.get("total_s"),
        bits,
        summary.get("llm_calls"),
        summary.get("tool_calls"),
        ", ".join(f"{s['id']}:{s['wall_s']}s" for s in specs) or "none",
        f" | slowest={slowest['id']} {slowest['wall_s']}s" if slowest else "",
        "".join(f" {k}={v}" for k, v in extra.items()),
    )


def _new_timing() -> dict:
    return {
        "specialists": {},
        "tools": {},
        "synth": 0.0,
        "gloss": 0.0,
        "plain": 0.0,
        "translate": 0.0,
        "router": 0.0,
    }


async def run_specialists(
    active_ids: list[str],
    case_with_context: str,
    ledger: EvidenceLedger,
    emit: Callable[[str, dict], None],
    timing: dict,
    *,
    focus: dict[str, str] | None = None,
    extra_directives: str = "",
) -> dict[str, SpecialistResult]:
    """Run one parallel round of the given specialists. Returns id → result.

    Emits `specialist_event` for progress and `specialist_round_complete` per
    agent. Crashes are converted to error results so one bad agent can't take the
    turn down.
    """
    focus = focus or {}

    def _spec_rec(sid):
        return timing["specialists"].setdefault(
            sid, {"wall": 0.0, "llm": 0.0, "tool": 0.0, "llm_n": 0, "tool_n": 0}
        )

    sem = asyncio.Semaphore(PARALLEL_SPECIALISTS)

    async def run_one(spec_id: str) -> tuple[str, SpecialistResult]:
        async with sem:
            def _emit(t: str, p: dict, sid=spec_id) -> None:
                if t == "llm_timing":
                    rec = _spec_rec(sid)
                    rec["llm"] += p.get("seconds", 0.0)
                    rec["llm_n"] += 1
                elif t == "tool_result":
                    rec = _spec_rec(sid)
                    secs = p.get("seconds", 0.0)
                    rec["tool"] += secs
                    rec["tool_n"] += 1
                    tr = timing["tools"].setdefault(p.get("tool", "?"), {"seconds": 0.0, "calls": 0})
                    tr["seconds"] += secs
                    tr["calls"] += 1
                emit("specialist_event", {"specialist": sid, "type": t, "payload": p})

            # Per-agent brief from the router, plus any pipeline-level directive
            # (e.g. chat-mode brevity). Handed in as the context prefix so it sits
            # above the case text the agent reads.
            prefix_parts = []
            if focus.get(spec_id):
                prefix_parts.append(
                    "YOUR ASSIGNMENT FOR THIS PATIENT (from the team's triage):\n"
                    + focus[spec_id].strip()
                )
            if extra_directives.strip():
                prefix_parts.append(extra_directives.strip())
            context_prefix = "\n\n".join(prefix_parts)

            _t0 = time.perf_counter()
            res = await run_specialist(spec_id, case_with_context, context_prefix, ledger, _emit)
            _spec_rec(spec_id)["wall"] += time.perf_counter() - _t0
            return spec_id, res

    raw_results = await asyncio.gather(
        *(run_one(sid) for sid in active_ids), return_exceptions=True
    )

    results: list[tuple[str, SpecialistResult]] = []
    for sid, res in zip(active_ids, raw_results):
        if isinstance(res, BaseException):
            log.exception("Specialist %s crashed uncaught", sid, exc_info=res)
            results.append((
                sid,
                SpecialistResult(
                    specialist_id=sid,
                    status="error",
                    error=f"{type(res).__name__}: {str(res)[:160]}",
                ),
            ))
        else:
            results.append(res)

    history: dict[str, SpecialistResult] = {}
    for sid, res in results:
        history[sid] = res
        emit(
            "specialist_round_complete",
            {
                "specialist": sid,
                "status": res.status,
                "draft_markdown": res.draft_markdown,
                "recommendation_summary": res.recommendation_summary,
                "evidence_labels": res.evidence_labels,
                "evidence": [
                    e.public()
                    for e in (ledger.get_by_label(l) for l in res.evidence_labels)
                    if e is not None
                ],
                "error": res.error,
            },
        )
    return history


async def run_consult(
    case: str,
    location: str,
    target_language: str,
    emit: Callable[[str, dict], None],
    *,
    specialist_ids: list[str],
    focus: dict[str, str] | None = None,
    preferences: str = "",
    ledger: EvidenceLedger | None = None,
    conversation_context: str = "",
    extra_directives: str = "",
    location_parsed: dict | None = None,
    emit_roster: bool = True,
    skipped_ids: list[str] | None = None,
) -> dict:
    """Full consult over an explicit roster: parallel research → synthesis →
    institution gloss → plain-language → translation.

    `ledger` can be passed in so a multi-turn conversation keeps one stable set of
    `[N]` labels across turns instead of renumbering every message.
    """
    ledger = ledger if ledger is not None else EvidenceLedger()
    board_t0 = time.perf_counter()
    timing = _new_timing()

    target_language = language.normalize_language(target_language)
    active_ids = order_specialists(specialist_ids)

    if emit_roster:
        roster = public_specialist_info(active_ids + (["translator"] if not _is_english(target_language) else []))
        emit("board_started", {"specialists": roster, "target_language": target_language})

    # Location: reuse the parse if the caller already has one (conversations do),
    # otherwise pay for one extraction call.
    if location_parsed is not None:
        loc = location_parsed
    else:
        loc = await asyncio.to_thread(language.extract_country_region, location)

    location_block = ""
    if location and location.strip():
        location_block = f"\n\nPatient location (free text): {location.strip()}"
        if loc.get("country"):
            parts = [loc["country"]]
            if loc.get("region"):
                parts.insert(0, loc["region"])
            if loc.get("city"):
                parts.insert(0, loc["city"])
            location_block += (
                f"\nExtracted location: {', '.join(parts)} "
                f"(confidence: {loc.get('confidence', 'low')})."
            )
    case_with_loc = case.rstrip() + location_block

    # Patient-shared preferences (diet, exercise, limitations) — appended so every
    # specialist sees them and must honor them per the SPECIFICITY GATE in COMMON_PREFIX.
    if preferences and preferences.strip():
        case_with_loc += (
            "\n\nPATIENT'S STATED PREFERENCES (must be honored when picking specific "
            "foods, exercises, or recommendations):\n" + preferences.strip()
        )

    if conversation_context.strip():
        case_with_loc += (
            "\n\nEARLIER IN THIS CONVERSATION (context — do not repeat what the patient "
            "has already been told; build on it):\n" + conversation_context.strip()
        )

    if loc.get("country"):
        emit("location_extracted", loc)

    history = await run_specialists(
        active_ids, case_with_loc, ledger, emit, timing,
        focus=focus, extra_directives=extra_directives,
    )

    # Mark any explicitly-skipped candidates (e.g. SLP pre-filtered out on the
    # un-routed path) so the UI can render their cards as skipped.
    for sid in (skipped_ids or []):
        if sid in history or sid not in SPECIALIST_CONFIGS:
            continue
        history[sid] = SpecialistResult(
            specialist_id=sid,
            status="skipped",
            draft_markdown="",
            recommendation_summary="(not applicable to this case)",
        )
        emit(
            "specialist_round_complete",
            {
                "specialist": sid,
                "status": "skipped",
                "draft_markdown": "",
                "recommendation_summary": "(not applicable to this case)",
                "evidence_labels": [],
                "evidence": [],
                "error": "",
            },
        )

    # Synthesize the English summary.
    emit("phase", {"phase": "synthesizing"})
    _s0 = time.perf_counter()
    english_md = await asyncio.to_thread(
        _synthesize_final, history, case, location, loc, preferences, ledger, active_ids,
        conversation_context=conversation_context,
    )
    timing["synth"] += time.perf_counter() - _s0

    # Institution-naming safety pass: deterministically guarantee every org named
    # in the summary is identified in plain English. Falls back to the un-glossed
    # summary on any error (see _gloss_institutions).
    emit("phase", {"phase": "naming_sources"})
    _g0 = time.perf_counter()
    english_md = await asyncio.to_thread(_gloss_institutions, english_md)
    timing["gloss"] += time.perf_counter() - _g0

    # Plain-language pass — the "turn this into simple terms" agent. Verified
    # mechanically against citation and number loss before being accepted.
    emit("phase", {"phase": "simplifying"})
    _p0 = time.perf_counter()
    english_md = await asyncio.to_thread(_plain_language, english_md)
    timing["plain"] += time.perf_counter() - _p0

    emit("synthesis_complete", {"english_markdown": english_md})

    # Translate (no-op if target is English).
    emit("phase", {"phase": "translating", "target_language": target_language})
    _t0 = time.perf_counter()
    translated_md = await asyncio.to_thread(_translate, english_md, target_language)
    timing["translate"] += time.perf_counter() - _t0

    references = ledger.public_list()
    summary = _build_timing_summary(timing, time.perf_counter() - board_t0)

    final = {
        "english_markdown": english_md,
        "translated_markdown": translated_md,
        "target_language": target_language,
        "references": references,
        "timing": summary,
        "location_inferred": loc,
        "specialists": active_ids,
    }
    log_timing("consult", summary, agents=len(active_ids))
    emit("timing_summary", summary)
    emit("final", final)
    return final


async def run_board(
    case: str,
    location: str,
    target_language: str,
    emit: Callable[[str, dict], None],
    *,
    preferences: str = "",
    specialist_ids: list[str] | None = None,
) -> dict:
    """One-shot consult over the standing full-consult roster — the original entry point.

    Used by the /api/board endpoint and the eval harness. `specialist_ids` lets a
    caller narrow the roster; by default config.FULL_CONSULT_IDS runs, with the SLP
    gated by the keyword pre-filter.
    """
    if specialist_ids is None:
        candidates = [sid for sid in FULL_CONSULT_IDS if sid in SPECIALIST_CONFIGS]
        active_ids = [sid for sid in candidates if sid != "slp" or _slp_relevant(case)]
        skipped = [sid for sid in candidates if sid not in active_ids]
    else:
        active_ids = order_specialists(specialist_ids)
        skipped = []

    return await run_consult(
        case,
        location,
        target_language,
        emit,
        specialist_ids=active_ids,
        preferences=preferences,
        skipped_ids=skipped,
    )
