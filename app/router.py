"""Triage: decide who on the team answers this message, and how much team to spend.

One cheap LLM call per patient turn. It replaces the old fixed roster (six agents
on every question, ~2 minutes) with a roster sized to the actual need — one agent
for "what does eGFR mean", four for "I was just diagnosed and I'm overwhelmed".

Everything the model returns is validated and clamped here, so a malformed or
adversarial routing decision degrades to "ask the generalist" instead of
breaking the turn. `route()` never raises.
"""
import logging
from dataclasses import dataclass, field

from app import llm, prompts, safety
from app.config import (
    MAX_TEAM_SIZE,
    SPECIALIST_CONFIGS,
    default_specialist_id,
    order_specialists,
    researcher_ids,
)

log = logging.getLogger(__name__)

VALID_MODES = ("clarify", "reply", "team")

# How much conversation the router sees. Enough for follow-ups ("what about
# swimming instead?") to be routed to the right agent, small enough to stay cheap.
_HISTORY_TURNS = 6
_HISTORY_CHARS_PER_MSG = 1200


@dataclass
class Route:
    mode: str = "reply"
    condition: str = ""
    topic: str = ""
    clarifying_question: str = ""
    specialists: list[str] = field(default_factory=list)
    focus: dict[str, str] = field(default_factory=dict)
    red_flag: safety.RedFlag = field(default_factory=safety.RedFlag)
    red_flag_action: str = ""
    reasoning: str = ""
    # True when the LLM call failed and we fell back to the default agent. Surfaced
    # so the UI can say "answering with the researcher" rather than pretending the
    # routing was deliberate.
    degraded: bool = False

    def focus_for(self, sid: str) -> str:
        return self.focus.get(sid, "")


def _history_block(messages: list[dict]) -> str:
    """Render recent conversation for the router. `messages` is oldest-first with
    {"role": "user"|"assistant", "content": str}."""
    recent = [m for m in (messages or []) if m.get("role") in ("user", "assistant")][-_HISTORY_TURNS:]
    if not recent:
        return "(this is the patient's first message)"
    lines = []
    for m in recent:
        who = "PATIENT" if m["role"] == "user" else "TEAM"
        body = (m.get("content") or "").strip()
        if len(body) > _HISTORY_CHARS_PER_MSG:
            body = body[:_HISTORY_CHARS_PER_MSG] + " …[truncated]"
        lines.append(f"{who}: {body}")
    return "\n\n".join(lines)


def _profile_block(profile: dict | None) -> str:
    p = profile or {}
    fields = [
        ("Condition(s) the patient has told us about", p.get("condition")),
        ("Location", p.get("location")),
        ("Preferred language for answers", p.get("language")),
        ("Diet / movement preferences and limits", p.get("preferences")),
        ("Age", p.get("age")),
    ]
    filled = [f"  {label}: {str(v).strip()}" for label, v in fields if str(v or "").strip()]
    if not filled:
        return "PATIENT PROFILE: (empty — the patient has not filled anything in)"
    return "PATIENT PROFILE:\n" + "\n".join(filled)


def _coerce_specialists(raw, mode: str) -> tuple[list[str], dict[str, str]]:
    """Validate the model's specialist list. Returns (ordered_ids, focus_by_id)."""
    valid = set(researcher_ids())
    ids: list[str] = []
    focus: dict[str, str] = {}

    for item in raw or []:
        sid, item_focus = "", ""
        if isinstance(item, dict):
            sid = str(item.get("id") or "").strip().lower()
            item_focus = str(item.get("focus") or "").strip()
        elif isinstance(item, str):
            sid = item.strip().lower()
        if sid in valid and sid not in ids:
            ids.append(sid)
            if item_focus:
                focus[sid] = item_focus

    if mode == "clarify":
        return [], {}

    # Truncate in the ROUTER's order of preference, then sort for presentation.
    # Ordering first would silently swap the model's first choice for whichever
    # id happens to come earliest in SECTION_ORDER.
    ids = ids[:1] if mode == "reply" else ids[:MAX_TEAM_SIZE]
    ids = order_specialists(ids)

    return ids, {k: v for k, v in focus.items() if k in ids}


def generic_focus(message: str) -> str:
    """The focus brief handed to a selected agent the model didn't brief.

    Shared so the pinned-room path in app/chat.py hands its agent the same string
    the router would have, instead of a near-copy that drifts.
    """
    return f"From your specialty, address the patient's question: {(message or '').strip()[:400]}"


def _fallback(message: str, condition: str = "", degraded: bool = True) -> Route:
    sid = default_specialist_id()
    flag = safety.screen(message)
    return Route(
        mode="reply",
        condition=condition,
        topic=(message or "").strip()[:80],
        specialists=[sid],
        focus={sid: f"Answer the patient's question directly: {(message or '').strip()[:400]}"},
        red_flag=flag,
        reasoning="Router unavailable; defaulted to the generalist researcher.",
        degraded=degraded,
    )


def route(
    message: str,
    history: list[dict] | None = None,
    profile: dict | None = None,
    force_single: bool = False,
) -> Route:
    """Triage one patient message. Never raises — always returns a usable Route.

    `force_single=True` is the "your care team" dashboard's mode: one agent per
    turn, always. It downgrades `team` to `reply` BEFORE `_coerce_specialists`
    runs, so the truncation to one id happens in the ROUTER's order of preference.
    Ordering first would swap the model's first choice for whichever id happens to
    come earliest in SECTION_ORDER. `clarify` is untouched and can still win.
    """
    msg = (message or "").strip()
    if not msg:
        return _fallback(msg, degraded=False)

    # Deterministic screen runs regardless of what the LLM decides.
    regex_flag = safety.screen(msg)

    user_content = (
        _profile_block(profile)
        + "\n\nCONVERSATION SO FAR:\n"
        + _history_block(history or [])
        + "\n\nTHE PATIENT'S NEWEST MESSAGE (route this one):\n"
        + msg
    )

    try:
        parsed = llm.chat_json(
            [
                {"role": "system", "content": prompts.ROUTER},
                {"role": "user", "content": user_content},
            ]
        )
    except llm.QuotaExceeded as e:
        log.warning("Router hit LLM quota: %s", e)
        return _fallback(msg, (profile or {}).get("condition", ""))
    except Exception:
        log.exception("Router failed; falling back to the default agent.")
        return _fallback(msg, (profile or {}).get("condition", ""))

    if not isinstance(parsed, dict):
        return _fallback(msg, (profile or {}).get("condition", ""))

    mode = str(parsed.get("mode") or "").strip().lower()
    if mode not in VALID_MODES:
        mode = "reply"
    # Before _coerce_specialists, deliberately — see the docstring.
    if force_single and mode == "team":
        mode = "reply"

    specialists, focus = _coerce_specialists(parsed.get("specialists"), mode)
    clarifying = str(parsed.get("clarifying_question") or "").strip()

    # Reconcile mode with what actually survived validation. A "team" that
    # validated down to one agent is a reply; a "clarify" with no question to ask
    # is a reply; a "reply" with no valid agent gets the default.
    if mode == "clarify" and not clarifying:
        mode = "reply"
    if mode != "clarify" and not specialists:
        sid = default_specialist_id()
        specialists = [sid]
        focus.setdefault(sid, f"Answer the patient's question directly: {msg[:400]}")
    if mode == "team" and len(specialists) < 2:
        mode = "reply"
        specialists = specialists[:1]

    # Every selected specialist gets a focus, even if the model omitted one.
    for sid in specialists:
        if not focus.get(sid):
            focus[sid] = generic_focus(msg)

    llm_flag = parsed.get("red_flag") or {}
    llm_present = bool(llm_flag.get("present")) if isinstance(llm_flag, dict) else False
    llm_why = str((llm_flag or {}).get("why") or "").strip() if isinstance(llm_flag, dict) else ""
    llm_action = str((llm_flag or {}).get("action") or "").strip() if isinstance(llm_flag, dict) else ""

    # OR the two screens. The regex screen wins on `kind` when it fired, because
    # it is the one that reliably distinguishes a crisis (needs helplines) from a
    # physical emergency (needs an ambulance).
    if regex_flag.present:
        flag = regex_flag
        if llm_why and not flag.why:
            flag.why = llm_why
    elif llm_present:
        flag = safety.RedFlag(
            present=True,
            kind="emergency",
            why=llm_why or "something you described",
            matched=["router"],
        )
    else:
        flag = safety.RedFlag()

    condition = str(parsed.get("condition") or "").strip()
    if not condition:
        condition = str((profile or {}).get("condition") or "").strip()

    return Route(
        mode=mode,
        condition=condition,
        topic=str(parsed.get("topic") or "").strip(),
        clarifying_question=clarifying,
        specialists=specialists,
        focus=focus,
        red_flag=flag,
        red_flag_action=llm_action,
        reasoning=str(parsed.get("reasoning") or "").strip(),
        degraded=False,
    )


def public_route(r: Route) -> dict:
    """Serializable view of a routing decision, for the SSE stream and the UI."""
    return {
        "mode": r.mode,
        "condition": r.condition,
        "topic": r.topic,
        "clarifying_question": r.clarifying_question,
        "specialists": [
            {
                "id": sid,
                "display_name": SPECIALIST_CONFIGS[sid]["display_name"],
                "color": SPECIALIST_CONFIGS[sid]["color"],
                "focus": r.focus_for(sid),
            }
            for sid in r.specialists
            if sid in SPECIALIST_CONFIGS
        ],
        "red_flag": {
            "present": r.red_flag.present,
            "kind": r.red_flag.kind,
            "why": r.red_flag.why,
        },
        "reasoning": r.reasoning,
        "degraded": r.degraded,
    }
