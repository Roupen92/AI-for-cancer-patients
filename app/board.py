"""Patient-care board orchestrator: single round of 6 parallel specialists,
synthesizer, then translator pass. No judge, no multi-round adversarial loop."""
import asyncio
import logging
import re
import time
from typing import Callable

from app import llm, language, prompts
from app.config import (
    PARALLEL_SPECIALISTS,
    SPECIALIST_CONFIGS,
    SPECIALIST_IDS,
    public_specialist_info,
    researcher_ids,
)
from app.evidence import EvidenceLedger
from app.specialist import SpecialistResult, run_specialist

log = logging.getLogger(__name__)


# Regex pre-filter for SLP relevance. If this hits, SLP runs; if it misses, we
# still let the LLM make the call via its SKIP marker (belt-and-suspenders).
# Prefix-style: trailing word boundary intentionally omitted so "laryng" matches
# "laryngeal", "dysphag" matches "dysphagia", "glioblastom" matches "glioblastoma".
_SLP_KEYWORDS = re.compile(
    r"\b("
    r"head\s+and\s+neck|"
    r"orophar|hypophar|nasophar|larynx|laryng|"
    r"esophag|oesophag|"
    r"glossectom|tongue\s+cancer|oral\s+(cavity|cancer)|"
    r"thyroid\s+cancer|"
    r"brain\s+tumor|brain\s+tumour|glioma|glioblastom|meningiom|"
    r"vocal\s+cord|voice\s+box|tracheostom|"
    r"swallow|dysphag|"
    r"speech\s+(problem|issue|change|loss)"
    r")",
    re.IGNORECASE,
)


def _slp_relevant(case: str) -> bool:
    return bool(_SLP_KEYWORDS.search(case or ""))


def _summary_or_skip(res: SpecialistResult) -> str:
    if res.status == "skipped":
        return "(skipped — not applicable to this case)"
    if res.status == "no_evidence":
        return "(could not find trustworthy sources — please ask your oncology team)"
    if res.status == "error":
        return f"(error: {res.error})"
    return res.recommendation_summary


def _synthesize_final(
    history: dict[str, SpecialistResult],
    case_raw: str,
    location_raw: str,
    location_parsed: dict,
    preferences: str,
    ledger: EvidenceLedger,
) -> str:
    """Single LLM call → English markdown summary, section-per-specialist.

    The user_content is structured as labeled blocks (PATIENT FACTS, SPECIALIST
    DRAFTS, CITED EVIDENCE) so the synthesizer can ctrl-F for tokens before
    writing — closes the loophole where it would paraphrase patient prose and
    invent plausible-but-wrong facts (e.g., 'Boston' instead of Toronto).
    """
    drafts = []
    for sid in researcher_ids():
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

    user_content = (
        "\n".join(facts_lines)
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
            "Please try again in a few minutes. The specialist notes above are still "
            "available below.\n\n"
            "**This is general information from public sources. It is not medical "
            "advice. Always talk to your oncology team.**"
        )
    except Exception as e:
        log.exception("Synthesizer failed.")
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:197] + "…"
        return f"## Something went wrong assembling your summary\n\n`{msg}`"


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

    Wall clock for ~20 entries at ~0.7s each, concurrency=6 ≈ 3-5 seconds.
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
    return {
        "total_s": round(total_s, 1),
        "llm_s": round(llm_total + timing["synth"] + timing["translate"], 1),
        "tool_s": round(tool_total, 1),
        "synth_s": round(timing["synth"], 1),
        "translate_s": round(timing["translate"], 1),
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "specialists": specs,
        "tools": tools,
    }


async def run_board(
    case: str,
    location: str,
    target_language: str,
    emit: Callable[[str, dict], None],
    *,
    preferences: str = "",
) -> dict:
    """Main entry. Streams events via emit(type, payload). Returns the final dict."""
    ledger = EvidenceLedger()
    history: dict[str, SpecialistResult] = {}

    board_t0 = time.perf_counter()
    timing = {"specialists": {}, "tools": {}, "synth": 0.0, "translate": 0.0}

    def _spec_rec(sid):
        return timing["specialists"].setdefault(
            sid, {"wall": 0.0, "llm": 0.0, "tool": 0.0, "llm_n": 0, "tool_n": 0}
        )

    target_language = language.normalize_language(target_language)

    # Determine which research agents run this turn. SLP pre-filter is a cheap
    # regex; the LLM SKIP gate inside specialist.py is the second line of defense.
    candidates = researcher_ids()
    active_ids = [sid for sid in candidates if sid != "slp" or _slp_relevant(case)]

    # Emit the roster IMMEDIATELY so the patient sees the helper cards on screen
    # right away — before the (potentially slow) location-extraction LLM call.
    roster = [s for s in public_specialist_info() if s["id"] in active_ids or s["id"] == "translator"]
    emit(
        "board_started",
        {
            "specialists": roster,
            "target_language": target_language,
        },
    )

    # One-shot location extraction. The result is bundled into the case so each
    # specialist sees it; the navigator's prompt knows to use it.
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
            location_block += f"\nExtracted location: {', '.join(parts)} (confidence: {loc.get('confidence', 'low')})."
    case_with_loc = case.rstrip() + location_block

    # Patient-shared preferences (diet, exercise, limitations) — appended so every
    # specialist sees them and must honor them per the SPECIFICITY GATE in COMMON_PREFIX.
    if preferences and preferences.strip():
        case_with_loc += (
            "\n\nPATIENT'S STATED PREFERENCES (must be honored when picking specific "
            "foods, exercises, or recommendations):\n" + preferences.strip()
        )

    if loc.get("country"):
        emit("location_extracted", loc)

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

            _t0 = time.perf_counter()
            res = await run_specialist(spec_id, case_with_loc, "", ledger, _emit)
            _spec_rec(spec_id)["wall"] += time.perf_counter() - _t0
            return spec_id, res

    # Single parallel research round.
    tasks = [run_one(sid) for sid in active_ids]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = []
    for sid, res in zip(active_ids, raw_results):
        if isinstance(res, BaseException):
            log.exception("Specialist %s crashed uncaught", sid, exc_info=res)
            clean = SpecialistResult(
                specialist_id=sid, status="error",
                error=f"{type(res).__name__}: {str(res)[:160]}",
            )
            results.append((sid, clean))
        else:
            results.append(res)

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

    # Mark any non-running candidates (e.g., SLP pre-filtered out) as skipped
    # in the history so the UI can render their cards as skipped.
    for sid in candidates:
        if sid not in [s for s, _ in results]:
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
        _synthesize_final, history, case, location, loc, preferences, ledger
    )
    timing["synth"] += time.perf_counter() - _s0
    emit("synthesis_complete", {"english_markdown": english_md})

    # NOTE: Plain-English (lay) summaries for citations are NOT generated
    # eagerly here. They're generated on-demand by GET /api/lay_summary/...
    # when the patient actually hovers a citation. That keeps the final
    # result fast (no extra 30s wait) and only pays the LLM cost for refs
    # the patient actually engages with.

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
    }
    emit("timing_summary", summary)
    emit("final", final)
    return final
