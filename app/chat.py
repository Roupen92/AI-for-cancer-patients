"""Chat turn orchestration.

One patient message in, one patient-facing answer out. The chat is a "your care
team" dashboard: the patient picks a room (Dietitian, Physiotherapist, Clinical
Trials…) and talks to THAT agent. One agent per turn, always.

    clarify  →  one short question back, no research at all
    reply    →  ONE specialist researches, then the plain-language pass

`run_turn(pinned_specialist=…)` is which room they are standing in; `None` is the
"not sure who to ask" front door, where the router picks the single best agent.
The router runs either way — it is cheap, and four things depend on it even when
the agent is already decided: the LLM red-flag screen that gets ORed with the
regex screen, the per-agent focus brief, `clarify` mode, and the referral target.

The `team` branch below is retained but unreachable from /api/chat. The un-routed
`board.run_consult` / `board.run_board` path (/api/board, /consult, the eval
harness) is untouched.

A red-flag screen runs before any of that, and its emergency block is prepended
deterministically so no LLM pass can soften or drop it.

The conversation's `EvidenceLedger` is shared across turns, so `[3]` refers to
the same source in the tenth message as it did in the second — and because each
room is its own `Conversation`, those labels are stable *per room*.
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


# --------------------------------------------------------------------------- #
# Referral (pinned rooms only)
# --------------------------------------------------------------------------- #
#
# In a room, silently swapping in the generalist when the chosen specialist skips
# breaks the whole metaphor: the patient thinks they are talking to the dietitian
# and someone else answers in their name. So a skip becomes an explicit referral
# to another room instead.
#
# The message is assembled in Python from the two display names, never generated
# by an LLM — same reasoning as safety.emergency_markdown(): a model asked to be
# warm here will drift into answering the question it was told it cannot answer.

def _display_name(sid: str) -> str:
    cfg = SPECIALIST_CONFIGS.get(sid) or {}
    return cfg.get("display_name") or sid


def _referral_target(from_id: str, router_top: str) -> str:
    """Who to send them to. "" means we have nobody better than the current room.

    The router's own top pick is free — it was computed for this message before
    the pin overwrote it. If the router wanted the pinned agent anyway (or wanted
    nothing usable), fall back to the generalist; if that IS the pinned agent,
    there is no referral to make.
    """
    if router_top and router_top != from_id and router_top in SPECIALIST_CONFIGS:
        return router_top
    fallback = default_specialist_id()
    return fallback if fallback != from_id else ""


def _referral_markdown(from_id: str, to_id: str) -> str:
    """Short, warm, in character, and deterministic. Names both rooms."""
    from_name = _display_name(from_id)
    if not to_id:
        return (
            f"That one is outside what I can help with as your {from_name} — I'd only be "
            "guessing, and I'd rather say so.\n\n"
            "It's a good one to put to your own care team. If part of it is about my "
            "area, ask me that part and I'll go and look it up properly."
        )
    to_name = _display_name(to_id)
    # Worded to stand alone AND to sit above the UI's "Ask the <room> →" button,
    # which carries the question across for them. Telling someone to go and find
    # the room by hand reads as a dead end next to a button that just does it.
    return (
        f"That one is outside what I can help with as your {from_name}.\n\n"
        f"**{to_name}** is the right place for it — open that room and your question "
        "comes with you. I'll still be here whenever you need me.\n\n"
        "It's also worth raising with your own care team."
    )


def _referral_payload(from_id: str, to_id: str, kind: str = "handoff") -> dict:
    """The `referral` SSE payload and the turn result's `referral` key.

    Two kinds, and the difference matters to the patient:

      handoff     the room could NOT answer. The referral IS the reply.
      suggestion  the room answered, but the triage pass wanted a different room
                  for this question. The answer stands; the referral sits under it
                  as "there's someone better placed for this."

    `suggestion` is what makes this feature real. A hard `skipped` almost never
    fires outside the conditional agents — asked about trials, the dietitian
    cheerfully answers rather than skipping — so a handoff-only design would leave
    a patient in the wrong room with no signpost, which is the exact failure the
    dashboard was supposed to remove.

    `reason` is a finished sentence, not a status code: it is written for a patient
    to read next to the button that switches rooms.
    """
    from_name, to_name = _display_name(from_id), _display_name(to_id)
    reason = (
        f"This isn't something {from_name} can answer."
        if kind == "handoff"
        else f"{to_name} can go further on this than {from_name} can."
    )
    return {
        "to": to_id,
        "to_display_name": to_name,
        "reason": reason,
        "kind": kind,
    }


async def _run_reply(
    conv: Conversation,
    route_result: router.Route,
    case_text: str,
    context: str,
    emit: Callable[[str, dict], None],
    timing: dict,
    *,
    pinned: str = "",
    router_top: str = "",
) -> tuple[str, str, dict | None, str]:
    """Run the single chosen specialist.

    Returns (english_markdown, status, referral_or_None, answering_specialist_id).

    `pinned` is set when the patient is inside a specialist's room, which changes
    the failure handling: a room may not silently hand the turn to a different
    agent, so an out-of-lane question becomes a referral instead of a swap.
    """
    sid = route_result.specialists[0]
    answered_by = sid
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
    # abstains leaves the patient with nothing.
    if res is not None and res.status in ("skipped", "no_evidence"):
        # Did this agent actually retrieve anything? That is what separates "wrong
        # room" from "right room, over-strict self-check".
        retrieved_anyway = conv.ledger.count_for(sid) > 0

        if pinned:
            # ---- Pinned room: refer, never swap. ----
            if res.status == "skipped":
                target = _referral_target(sid, router_top)
                referral = _referral_payload(sid, target) if target else None
                if referral:
                    emit("referral", referral)
                return _referral_markdown(sid, target), "skipped", referral, sid

            if retrieved_anyway:
                # It found sources and abstained anyway — the self-check being
                # over-strict, not a retrieval failure. Observed live: the same
                # question abstains once and answers well on a second pass.
                emit("fallback", {"from": sid, "to": sid, "reason": "abstained_despite_sources"})
                retried = await _ask(sid, route_result.focus)
                # Only take the retry if it actually did better — never trade a
                # real answer for a second abstention.
                if retried is not None and retried.status == "done":
                    res = retried
            # Nothing retrieved at all: say so honestly. No agent swap.

        else:
            # ---- Front door: unchanged. Exactly one retry, chosen by why the
            # first attempt failed:
            #
            #   * Wrong specialist picked (it skipped, or its sources don't cover
            #     this) -> hand it to the generalist, which has the widest source
            #     list and never self-skips.
            #   * Right specialist, but it abstained even though it DID retrieve
            #     sources -> re-ask the same agent rather than switching.
            fallback = default_specialist_id()

            if sid != fallback:
                emit("fallback", {"from": sid, "to": fallback, "reason": res.status})
                retried = await _ask(
                    fallback,
                    {fallback: route_result.focus_for(sid) or f"Answer directly: {case_text[:400]}"},
                )
                if retried is not None:
                    res = retried
                    answered_by = fallback
            elif res.status == "no_evidence" and retrieved_anyway:
                emit("fallback", {"from": sid, "to": sid, "reason": "abstained_despite_sources"})
                retried = await _ask(sid, route_result.focus)
                if retried is not None and retried.status == "done":
                    res = retried

    if res is None:
        return _STATUS_APOLOGY["error"], "error", None, answered_by
    if res.status != "done":
        return (
            _STATUS_APOLOGY.get(res.status, _STATUS_APOLOGY["error"]),
            res.status,
            None,
            answered_by,
        )

    draft = _strip_internal_markers(res.draft_markdown or res.recommendation_summary)
    if not draft:
        return _STATUS_APOLOGY["error"], "error", None, answered_by

    emit("phase", {"phase": "simplifying"})
    _p0 = time.perf_counter()
    simple = await asyncio.to_thread(board._plain_language, draft)
    timing["plain"] += time.perf_counter() - _p0

    # The room answered — but if triage wanted a different room for this question,
    # say so underneath. Only inside a room (the front door already went wherever
    # the router pointed), and only on a genuine mismatch: ask the dietitian about
    # salt and `router_top` is the dietitian, so nothing fires.
    if pinned and router_top and router_top != sid and router_top in SPECIALIST_CONFIGS:
        suggestion = _referral_payload(sid, router_top, kind="suggestion")
        emit("referral", suggestion)
        return simple, "done", suggestion, answered_by

    return simple, "done", None, answered_by


# --------------------------------------------------------------------------- #
# Turn entry point
# --------------------------------------------------------------------------- #

async def run_turn(
    conv: Conversation,
    turn: Turn,
    message: str,
    emit: Callable[[str, dict], None],
    pinned_specialist: str | None = None,
) -> dict:
    """Handle one patient message end-to-end. Streams events via emit().

    `pinned_specialist` is the room the patient is in. `None` is the front door,
    where the router picks the one best agent. Either way exactly one specialist
    answers — see the module docstring.
    """
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

    # 2. Triage. The router runs on EVERY turn, pinned or not: it is one cheap
    #    JSON call and four things depend on it even when the agent is already
    #    decided — the LLM red-flag screen that gets ORed with the regex screen
    #    above, the per-agent focus brief, `clarify` mode, and the referral target.
    #    `force_single` keeps it to one agent per turn in the router's own order of
    #    preference (see router.route).
    emit("phase", {"phase": "triaging"})
    _r0 = time.perf_counter()
    route_result = await asyncio.to_thread(
        router.route, message, conv.messages, conv.profile, True
    )
    timing["router"] += time.perf_counter() - _r0

    # The router's own top pick, remembered BEFORE the pin overwrites it. It costs
    # nothing extra and it is the referral target when this room turns out to be
    # the wrong one.
    router_top = route_result.specialists[0] if route_result.specialists else ""

    pinned = (pinned_specialist or "").strip()
    if pinned and route_result.mode != "clarify":
        # A clarifying question still wins: it comes from this room, which is fine.
        # Otherwise the room decides who answers, not the router.
        focus_text = route_result.focus_for(pinned) or router.generic_focus(message)
        route_result.mode = "reply"
        route_result.specialists = [pinned]
        route_result.focus = {pinned: focus_text}

    # Emitted after the pin is applied, so the UI names the agent that will really
    # answer rather than the router's pick.
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
    referral: dict | None = None
    answering_specialist = ""

    # 3. Do the work.
    if route_result.mode == "clarify":
        english_md = route_result.clarifying_question.strip()
        status = "clarify"

    elif route_result.mode == "reply":
        english_md, status, referral, answering_specialist = await _run_reply(
            conv, route_result, case_text, context, emit, timing,
            pinned=pinned, router_top=router_top,
        )

    else:  # team — retained but unreachable from /api/chat (force_single above).
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
        # Who actually answered. "" on the clarify path (nobody researched) and on
        # the team path (no single agent owns the summary).
        "specialist": answering_specialist,
        "referral": referral,
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
        # Which room this conversation belongs to; "" for the front door.
        "specialist": conv.specialist,
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
    """Every room on the care-team dashboard, in SECTION_ORDER-agnostic config order.

    Powers GET /api/team: the sidebar entry AND the room card the patient reads
    before they commit to a room, so it carries the `room` copy from config.
    The translator is excluded because it is not a researcher (`role:
    post_synthesis`), and a patient can no more chat with it than the synthesizer.
    """
    from app.config import SECTION_HEADINGS, researcher_ids, room_for

    out = []
    for sid in researcher_ids():
        cfg = SPECIALIST_CONFIGS[sid]
        out.append(
            {
                "id": sid,
                "display_name": cfg["display_name"],
                "color": cfg["color"],
                "section_heading": SECTION_HEADINGS.get(sid, cfg["display_name"]),
                **room_for(sid),
            }
        )
    return out
