"""Chat turn orchestration.

One patient message in, one patient-facing answer out. The shape of the work is
decided per message by app/router.py:

    clarify  →  one short question back, no research at all
    reply    →  ONE specialist researches, then the plain-language pass          (fast)
    team     →  2-4 specialists in parallel → synthesis → gloss → plain language (thorough)

A red-flag screen runs before any of that, and its emergency block is prepended
deterministically so no LLM pass can soften or drop it.

The conversation's `EvidenceLedger` is shared across turns, so `[3]` refers to
the same source in the tenth message as it did in the second.
"""
import asyncio
import logging
import re
import time
from typing import Callable

from app import board, genomics, language, llm, prompts, router, safety
from app.config import SPECIALIST_CONFIGS, default_specialist_id
from app.sessions import Conversation, Turn

log = logging.getLogger(__name__)

# How much of the conversation the specialists see as context.
_CONTEXT_TURNS = 4
_CONTEXT_CHARS = 700


# --------------------------------------------------------------------------- #
# Markdown tidying
# --------------------------------------------------------------------------- #

# Internal handoff marker: the specialists end every draft with it so the
# synthesizer has a one-line take-home. On the reply path there is no
# synthesizer, so it must not reach the patient.
_RECOMMENDATION_BLOCK = re.compile(
    r"\n*#{0,4}\s*\**RECOMMENDATION\s+SUMMARY\s*:?\**.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_LEADING_ABSTAIN = re.compile(r"^\s*(ABSTAIN|SKIP)\s*:\s*", re.IGNORECASE)
# The all-caps cue the prompts require. Fine as an instruction to the model,
# shouty as a heading for the patient.
# Horizontal whitespace only (`[ \t]`, not `\s`) so the surrounding blank lines
# survive — eating them would weld the heading onto the paragraph above it.
# Both patterns are case-SENSITIVE: they must match only the model's literal
# ALL-CAPS cue, never the nicely-cased heading the first substitution inserts.
_ASK_BLOCK = re.compile(
    r"^[ \t]*#{0,4}[ \t]*\**WHAT\s+TO\s+ASK\s+YOUR\s+CARE\s+TEAM[ \t]*:?\**[ \t]*$",
    re.MULTILINE,
)
_ASK_INLINE = re.compile(r"\**WHAT\s+TO\s+ASK\s+YOUR\s+CARE\s+TEAM[ \t]*:?\**")
# The red-flag callout gets the same treatment — but it keeps its colon and stays
# bold rather than becoming a heading, because it usually introduces a sentence
# on the same line rather than a section.
# The trailing qualifier varies ("RIGHT AWAY", "IMMEDIATELY", "NOW", nothing at
# all), and an unmatched variant survives into the answer as shouting — worse
# still in a translated answer, where the translator faithfully reproduces the
# capitals in the target language.
_URGENCY = r"(?:\s+(?:RIGHT\s+AWAY|IMMEDIATELY|NOW|URGENTLY|STRAIGHT\s+AWAY))?"
_CALL_INLINE = re.compile(
    rf"\**WHEN\s+TO\s+CALL\s+YOUR\s+CARE\s+TEAM{_URGENCY}[ \t]*:?\**"
)
_CALL_IF_INLINE = re.compile(
    rf"\**CALL\s+YOUR\s+CARE\s+TEAM{_URGENCY}\s+IF[ \t]*:?\**"
)


def _strip_internal_markers(md: str) -> str:
    out = _RECOMMENDATION_BLOCK.sub("", md or "").rstrip()
    out = _LEADING_ABSTAIN.sub("", out)
    return out.strip()


def _prettify_markers(md: str) -> str:
    """Turn the prompts' all-caps cue lines into real headings.

    All three patterns are case-sensitive so they only ever match the model's
    literal ALL-CAPS cue, never the nicely-cased text a previous substitution
    just inserted.
    """
    out = _ASK_BLOCK.sub("### What to ask your care team", md or "")
    out = _ASK_INLINE.sub("**What to ask your care team**", out)
    out = _CALL_IF_INLINE.sub("**Call your care team right away if:**", out)
    out = _CALL_INLINE.sub("**When to call your care team:**", out)
    return out


_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _references_in(ledger, md: str) -> list[dict]:
    """The ledger entries actually cited in this piece of markdown, in label order."""
    labels = {int(n) for n in _CITE_RE.findall(md or "")}
    out = []
    for label in sorted(labels):
        entry = ledger.get_by_label(str(label))
        if entry is not None:
            out.append(entry.public())
    return out


# --------------------------------------------------------------------------- #
# Conversation context
# --------------------------------------------------------------------------- #

def _conversation_context(conv: Conversation) -> str:
    """Compact recent history for the specialists and the synthesizer."""
    recent = [m for m in conv.messages if m.get("role") in ("user", "assistant")][-(_CONTEXT_TURNS * 2):]
    if not recent:
        return ""
    lines = []
    for m in recent:
        who = "Patient asked" if m["role"] == "user" else "The team answered"
        body = re.sub(r"\s+", " ", (m.get("content") or "")).strip()
        if len(body) > _CONTEXT_CHARS:
            body = body[:_CONTEXT_CHARS] + " …"
        lines.append(f"- {who}: {body}")
    return "\n".join(lines)


def _profile_case_block(conv: Conversation, route_result: router.Route, message: str) -> str:
    """The 'case' text handed to the specialists for this turn."""
    p = conv.profile or {}
    parts = [f"THE PATIENT'S QUESTION (this turn):\n{message.strip()}"]

    facts = []
    condition = str(p.get("condition") or route_result.condition or "").strip()
    if condition:
        facts.append(f"Condition the patient has told us about: {condition}")
    if str(p.get("age") or "").strip():
        facts.append(f"Age: {str(p['age']).strip()}")
    if str(p.get("location") or "").strip():
        facts.append(f"Location (free text): {str(p['location']).strip()}")
    if facts:
        parts.append("WHAT THE PATIENT HAS TOLD US ABOUT THEMSELVES:\n" + "\n".join(f"- {f}" for f in facts))
    return "\n\n".join(parts)


async def _ensure_location(conv: Conversation) -> dict:
    """Parse the profile's free-text location once per conversation."""
    raw = str((conv.profile or {}).get("location") or "").strip()
    if not raw:
        conv.location_parsed = {"country": "", "region": "", "city": "", "confidence": "low"}
        conv.location_source = ""
        return conv.location_parsed
    if conv.location_parsed is not None and conv.location_source == raw:
        return conv.location_parsed
    parsed = await asyncio.to_thread(language.extract_country_region, raw)
    conv.location_parsed = parsed
    conv.location_source = raw
    return parsed


# --------------------------------------------------------------------------- #
# The reply path — one specialist, then plain language
# --------------------------------------------------------------------------- #

_STATUS_APOLOGY = {
    "no_evidence": (
        "I looked through the trusted sources I have access to and couldn't find "
        "anything solid enough to answer this safely, so I'd rather tell you that "
        "than guess.\n\n"
        "This is a good one to put to your care team directly — and if you tell me a "
        "bit more about your situation, I can try a different angle."
    ),
    "error": (
        "Something went wrong on my side while I was researching this — it wasn't "
        "anything you did. Please try asking again in a moment."
    ),
    "skipped": (
        "That question sits outside what I can help with from public sources. Your "
        "care team is the right place for it."
    ),
}


async def _run_reply(
    conv: Conversation,
    route_result: router.Route,
    case_text: str,
    context: str,
    emit: Callable[[str, dict], None],
    timing: dict,
) -> tuple[str, str]:
    """Run the single chosen specialist. Returns (english_markdown, status)."""
    sid = route_result.specialists[0]
    directives = prompts.CHAT_BREVITY + (
        f"\n\nCONVERSATION SO FAR (do not repeat this back):\n{context}" if context else ""
    )

    async def _ask(agent_id: str, focus: dict[str, str]):
        history = await board.run_specialists(
            [agent_id], case_text, conv.ledger, emit, timing,
            focus=focus, extra_directives=directives,
        )
        return history.get(agent_id)

    res = await _ask(sid, route_result.focus)

    # On the reply path a single agent IS the answer, so an agent that skips or
    # abstains leaves the patient with nothing. Exactly one retry, chosen by why
    # the first attempt failed:
    #
    #   * Wrong specialist picked (it skipped, or its sources don't cover this)
    #     -> hand it to the generalist, which has the widest source list and
    #        never self-skips.
    #   * Right specialist, but it abstained even though it DID retrieve sources
    #     -> that's the self-check being over-strict, not a retrieval failure.
    #        Observed live: the same question abstains once and answers well on a
    #        second pass. Re-ask the same agent rather than switching.
    if res is not None and res.status in ("skipped", "no_evidence"):
        fallback = default_specialist_id()
        retrieved_anyway = conv.ledger.count_for(sid) > 0

        if sid != fallback:
            emit("fallback", {"from": sid, "to": fallback, "reason": res.status})
            retried = await _ask(
                fallback,
                {fallback: route_result.focus_for(sid) or f"Answer directly: {case_text[:400]}"},
            )
            res = retried or res
        elif res.status == "no_evidence" and retrieved_anyway:
            emit("fallback", {"from": sid, "to": sid, "reason": "abstained_despite_sources"})
            retried = await _ask(sid, route_result.focus)
            # Only take the retry if it actually did better — never trade a real
            # answer for a second abstention.
            if retried is not None and retried.status == "done":
                res = retried

    if res is None:
        return _STATUS_APOLOGY["error"], "error"
    if res.status != "done":
        return _STATUS_APOLOGY.get(res.status, _STATUS_APOLOGY["error"]), res.status

    draft = _strip_internal_markers(res.draft_markdown or res.recommendation_summary)
    if not draft:
        return _STATUS_APOLOGY["error"], "error"

    emit("phase", {"phase": "simplifying"})
    _p0 = time.perf_counter()
    simple = await asyncio.to_thread(board._plain_language, draft)
    timing["plain"] += time.perf_counter() - _p0

    return simple, "done"


# --------------------------------------------------------------------------- #
# Turn entry point
# --------------------------------------------------------------------------- #

async def run_turn(
    conv: Conversation,
    turn: Turn,
    message: str,
    emit: Callable[[str, dict], None],
) -> dict:
    """Handle one patient message end-to-end. Streams events via emit()."""
    t0 = time.perf_counter()
    timing = board._new_timing()

    target_language = language.normalize_language((conv.profile or {}).get("language") or "English")
    preferences = str((conv.profile or {}).get("preferences") or "").strip()

    emit("turn_started", {"turn_id": turn.tid, "target_language": target_language})

    # 1. Deterministic red-flag screen, before any model call — so the emergency
    #    block reaches the patient in milliseconds even if the LLM is slow or down.
    early_flag = safety.screen(message)
    if early_flag.present:
        emit("red_flag", {"kind": early_flag.kind, "why": early_flag.why})

    # Genomic screen, same shape as the red-flag screen: deterministic, before any
    # model call, and its block is prepended in Python at step 5 so no LLM pass can
    # reword or drop the germline-vs-somatic question. See app/genomics.py for why
    # asking beats inferring.
    report_signal = genomics.classify(message)
    if report_signal.present:
        emit(
            "genomic_report",
            {
                "origin": report_signal.origin,
                "markers_without_assay": report_signal.markers_without_assay,
                # Which KINDS of identifier were spotted, never the values.
                "identifiers": report_signal.identifiers,
            },
        )

    # 2. Triage.
    emit("phase", {"phase": "triaging"})
    _r0 = time.perf_counter()
    route_result = await asyncio.to_thread(
        router.route, message, conv.messages, conv.profile
    )
    timing["router"] += time.perf_counter() - _r0
    emit("routed", router.public_route(route_result))

    if route_result.red_flag.present and not early_flag.present:
        emit("red_flag", {"kind": route_result.red_flag.kind, "why": route_result.red_flag.why})

    emergency_block = safety.emergency_markdown(
        route_result.red_flag, route_result.red_flag_action
    )

    await _ensure_location(conv)
    context = _conversation_context(conv)
    case_text = _profile_case_block(conv, route_result, message)

    status = "done"
    english_md = ""
    consult: dict | None = None

    # 3. Do the work.
    if route_result.mode == "clarify":
        english_md = route_result.clarifying_question.strip()
        status = "clarify"

    elif route_result.mode == "reply":
        english_md, status = await _run_reply(
            conv, route_result, case_text, context, emit, timing
        )

    else:  # team
        consult = await board.run_consult(
            case_text,
            str((conv.profile or {}).get("location") or ""),
            target_language,
            emit,
            specialist_ids=route_result.specialists,
            focus=route_result.focus,
            preferences=preferences,
            ledger=conv.ledger,
            conversation_context=context,
            location_parsed=conv.location_parsed,
            emit_roster=False,
        )
        english_md = consult.get("english_markdown") or ""
        status = "done" if english_md else "error"
        # run_consult already merged its own timings; fold them into ours so the
        # turn reports one coherent set of numbers.
        for key in ("synth", "gloss", "plain", "translate"):
            timing[key] += consult["timing"].get(f"{key}_s", 0.0)

    english_md = _prettify_markers(english_md)

    # 4. Translate. The team path already did it inside run_consult.
    if consult is not None:
        translated_md = _prettify_markers(consult.get("translated_markdown") or english_md)
    elif board._is_english(target_language) or status == "clarify":
        # A one-line clarifying question still needs translating for a non-English
        # patient, but nothing else about it needs the full pipeline.
        if status == "clarify" and not board._is_english(target_language):
            emit("phase", {"phase": "translating", "target_language": target_language})
            translated_md = await asyncio.to_thread(board._translate, english_md, target_language)
        else:
            translated_md = english_md
    else:
        emit("phase", {"phase": "translating", "target_language": target_language})
        _t0 = time.perf_counter()
        translated_md = await asyncio.to_thread(board._translate, english_md, target_language)
        timing["translate"] += time.perf_counter() - _t0
        # Run the tidier over the translated text too: the cue patterns are
        # English and case-sensitive, so they no-op on translated prose but do
        # catch a cue the translator chose to leave in English.
        translated_md = _prettify_markers(translated_md)

    # 5. Prepend the deterministic blocks LAST, after every LLM pass has run, so
    #    nothing can reword, shorten, or bury them. The emergency block goes
    #    outermost: if someone is describing a stroke while asking about their
    #    BRCA result, the stroke instruction is the first thing they must read.
    #
    #    The genomic block is skipped on the clarify path, where the whole answer
    #    is one short question and a wall of preamble above it would bury it.
    if report_signal.present and status != "clarify":
        genomic_block = genomics.preamble(report_signal)
        if genomic_block:
            english_md = genomic_block + "\n" + english_md
            translated_md = genomic_block + "\n" + translated_md

    if emergency_block:
        english_md = emergency_block + "\n" + english_md
        translated_md = emergency_block + "\n" + translated_md

    references = _references_in(conv.ledger, english_md)

    conv.add_message("user", message, turn_id=turn.tid)
    conv.add_message(
        "assistant",
        translated_md or english_md,
        turn_id=turn.tid,
        references=[r["label"] for r in references],
    )

    timing_summary = board._build_timing_summary(timing, time.perf_counter() - t0)

    result = {
        "turn_id": turn.tid,
        "conversation_id": conv.cid,
        "mode": route_result.mode,
        "status": status,
        "english_markdown": english_md,
        "markdown": translated_md or english_md,
        "target_language": target_language,
        "references": references,
        "all_references": conv.ledger.public_list(),
        "route": router.public_route(route_result),
        "timing": timing_summary,
        "location_inferred": conv.location_parsed or {},
    }
    board.log_timing(
        f"turn {conv.cid}/{turn.tid}",
        timing_summary,
        mode=route_result.mode,
        status=status,
        refs=len(references),
        lang=target_language,
    )
    emit("timing_summary", timing_summary)
    emit("turn_complete", result)
    return result


async def lay_summary_for(conv: Conversation, label: str) -> str:
    """On-demand plain-English rewrite of one citation, cached per conversation."""
    cached = conv.lay_summaries.get(label)
    if cached:
        return cached

    entry = conv.ledger.get_by_label(str(label))
    if entry is None:
        return ""

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
    text = ""
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(llm.chat, messages, tools=None), timeout=25.0
        )
        text = (resp.choices[0].message.content or "").strip()
    except llm.QuotaExceeded:
        text = ""
    except (asyncio.TimeoutError, Exception):
        log.exception("lay_summary failed for cid=%s label=%s", conv.cid, label)
        text = ""

    if not text:
        snippet = (entry.summary or "").strip()
        if snippet:
            text = snippet[:240].rsplit(" ", 1)[0] + ("…" if len(snippet) > 240 else "")

    if text:
        conv.lay_summaries[label] = text
        conv.ledger.set_lay_summary(label, text)
    return text


def public_conversation(conv: Conversation) -> dict:
    """Serializable transcript for a client that reloaded mid-conversation."""
    return {
        "conversation_id": conv.cid,
        "profile": conv.profile,
        "messages": [
            {
                "role": m["role"],
                "content": m["content"],
                "turn_id": m.get("turn_id", ""),
                "references": m.get("references", []),
            }
            for m in conv.messages
        ],
        "references": conv.ledger.public_list(),
        "active_turns": conv.active_turns(),
    }


def specialist_catalogue() -> list[dict]:
    """Every agent the router can call, for the UI's 'who's on the team' panel."""
    from app.config import SECTION_HEADINGS, researcher_ids

    out = []
    for sid in researcher_ids():
        cfg = SPECIALIST_CONFIGS[sid]
        out.append(
            {
                "id": sid,
                "display_name": cfg["display_name"],
                "color": cfg["color"],
                "section_heading": SECTION_HEADINGS.get(sid, cfg["display_name"]),
            }
        )
    return out
