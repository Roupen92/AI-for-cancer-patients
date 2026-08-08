"""The "your care team" dashboard: room metadata, one agent per turn, referrals.

The patient picks a room (Dietitian, Clinical Trials, …) and talks to THAT agent.
These tests pin the three things that break silently:

  * a specialist with no `room` block ships as a blank dashboard card,
  * `force_single` truncating AFTER the SECTION_ORDER sort would quietly swap the
    router's first choice for whichever id sorts earliest,
  * a pinned room that falls back to the generalist answers in the dietitian's
    name, which is the whole metaphor broken.

Nothing here hits the network or an LLM: the router, the specialist round and the
plain-language pass are all stubbed.
"""
import asyncio

import pytest


# --------------------------------------------------------------------------- #
# Room metadata
# --------------------------------------------------------------------------- #

def test_every_researcher_has_a_room_block():
    # Same failure mode as a missing SECTION_HEADINGS entry: nothing raises, the
    # patient just gets a card with no tagline, no blurb and no starter questions.
    from app.config import SPECIALIST_CONFIGS, researcher_ids

    for sid in researcher_ids():
        room = SPECIALIST_CONFIGS[sid].get("room")
        assert room, f"{sid} has no room block"
        for key in ("tagline", "blurb", "covers", "examples"):
            assert room.get(key), f"{sid}'s room is missing {key}"
        assert len(room["examples"]) >= 3, f"{sid} needs 3 starter questions"
        assert 3 <= len(room["covers"]) <= 5, f"{sid}'s covers should be 3-5 chips"


def test_room_taglines_fit_the_card():
    from app.config import SPECIALIST_CONFIGS, researcher_ids

    for sid in researcher_ids():
        tagline = SPECIALIST_CONFIGS[sid]["room"]["tagline"]
        assert len(tagline) <= 60, f"{sid}'s tagline is {len(tagline)} chars"


def test_room_copy_never_promises_a_verdict_on_eligibility_or_diagnosis():
    # prompts.TRIALS forbids "you qualify"; the card is read BEFORE the answer and
    # would set the expectation the answer then has to refuse.
    from app.config import SPECIALIST_CONFIGS, researcher_ids

    # Phrases, not words: "newly diagnosed" is a legitimate chip on the stories
    # card, while "diagnose you" is a promise nothing here is allowed to keep.
    banned = ("you qualify", "you're eligible", "you are eligible", "whether you qualify",
              "ai-powered", "ai powered", "diagnose you", "diagnose your",
              "tell you what you have", "what's wrong with you")
    for sid in researcher_ids():
        room = SPECIALIST_CONFIGS[sid]["room"]
        blob = " ".join(
            [room["tagline"], room["blurb"], *room["covers"], *room["examples"]]
        ).lower()
        for phrase in banned:
            assert phrase not in blob, f"{sid}'s room copy says {phrase!r}"


def test_translator_has_no_room_and_is_not_a_place_you_can_chat():
    from app.config import SPECIALIST_CONFIGS, researcher_ids

    assert "room" not in SPECIALIST_CONFIGS["translator"]
    assert "translator" not in researcher_ids()


def test_room_for_never_raises_on_a_specialist_without_one():
    from app.config import room_for

    empty = room_for("translator")
    assert empty == {"tagline": "", "blurb": "", "covers": [], "examples": []}
    assert room_for("nope_not_an_agent")["covers"] == []


def test_specialist_catalogue_carries_the_room_fields():
    from app.chat import specialist_catalogue
    from app.config import researcher_ids

    cat = specialist_catalogue()
    assert {c["id"] for c in cat} == set(researcher_ids())
    for entry in cat:
        for key in ("display_name", "color", "section_heading",
                    "tagline", "blurb", "covers", "examples"):
            assert entry.get(key), f"{entry['id']} is missing {key}"


# --------------------------------------------------------------------------- #
# force_single
# --------------------------------------------------------------------------- #

def _router_returning(monkeypatch, payload: dict):
    """Point router.llm.chat_json at a fixed parsed payload."""
    from app import router

    monkeypatch.setattr(router.llm, "chat_json", lambda messages, **kw: payload)
    return router


def test_force_single_never_returns_a_team(monkeypatch):
    router = _router_returning(monkeypatch, {
        "mode": "team",
        "specialists": [{"id": "dietician"}, {"id": "physio"}, {"id": "mental"}],
    })
    r = router.route("I was just diagnosed and I'm overwhelmed", force_single=True)
    assert r.mode == "reply"
    assert len(r.specialists) == 1


def test_force_single_keeps_the_routers_first_choice_not_the_first_in_section_order(
    monkeypatch
):
    # `stories` sorts LAST in SECTION_ORDER and `researcher` sorts FIRST. Coercing
    # team -> reply after the sort would hand back "researcher" and silently
    # discard the model's actual pick — which is why route() downgrades the mode
    # before _coerce_specialists runs.
    router = _router_returning(monkeypatch, {
        "mode": "team",
        "specialists": [{"id": "stories", "focus": "lived experience"},
                        {"id": "researcher", "focus": "the evidence"}],
    })
    r = router.route("what was it like for other people?", force_single=True)
    assert r.specialists == ["stories"]
    assert r.focus_for("stories") == "lived experience"

    # Without the flag the same payload is still a team, ordered for presentation.
    same = router.route("what was it like for other people?")
    assert same.mode == "team"
    assert same.specialists == ["researcher", "stories"]


def test_force_single_still_lets_clarify_win(monkeypatch):
    router = _router_returning(monkeypatch, {
        "mode": "clarify",
        "clarifying_question": "Which of your conditions is this about?",
        "specialists": [{"id": "researcher"}],
    })
    r = router.route("is it bad?", force_single=True)
    assert r.mode == "clarify"
    assert r.specialists == []


def test_generic_focus_is_the_one_the_router_uses(monkeypatch):
    # The pinned path in chat.py reaches for this when the router briefed a
    # different agent; a near-copy would drift out of sync with the prompt.
    from app import router

    router_ = _router_returning(monkeypatch, {
        "mode": "reply", "specialists": [{"id": "physio"}],   # no focus given
    })
    r = router_.route("my knee hurts")
    assert r.focus_for("physio") == router.generic_focus("my knee hurts")


# --------------------------------------------------------------------------- #
# Referral message and target
# --------------------------------------------------------------------------- #

def test_referral_message_names_both_rooms_and_needs_no_llm():
    from app.chat import _referral_markdown

    md = _referral_markdown("dietician", "trials")
    assert "Dietitian" in md
    assert "Clinical Trials" in md
    # Warm, short, and it points at the patient's real clinicians too.
    assert "care team" in md
    assert len(md) < 600


def test_referral_message_is_deterministic():
    # Assembled in Python for the same reason as safety.emergency_markdown().
    from app.chat import _referral_markdown

    assert _referral_markdown("slp", "physio") == _referral_markdown("slp", "physio")


def test_referral_message_still_works_with_nobody_to_refer_to():
    from app.chat import _referral_markdown

    md = _referral_markdown("researcher", "")
    assert "Medical Research" in md
    assert "care team" in md


def test_referral_target_prefers_the_routers_pick_then_the_generalist():
    from app.chat import _referral_target

    # The router wanted someone else — that's the target, for free.
    assert _referral_target("dietician", "trials") == "trials"
    # The router wanted this same room: fall back to the generalist.
    assert _referral_target("dietician", "dietician") == "researcher"
    assert _referral_target("dietician", "") == "researcher"
    assert _referral_target("dietician", "not_an_agent") == "researcher"
    # ...unless the generalist IS this room, in which case there is nobody left.
    assert _referral_target("researcher", "researcher") == ""


def test_referral_payload_shape():
    from app.chat import _referral_payload

    payload = _referral_payload("dietician", "trials")
    assert set(payload) == {"to", "to_display_name", "reason", "kind"}
    assert payload["to"] == "trials"
    assert payload["to_display_name"] == "Clinical Trials"
    assert payload["reason"]
    assert payload["kind"] == "handoff", "a bare call is the couldn't-answer case"

    # The two kinds read differently to the patient, because they mean different
    # things: one replaces the answer, the other sits under it.
    suggestion = _referral_payload("dietician", "trials", kind="suggestion")
    assert suggestion["kind"] == "suggestion"
    assert suggestion["reason"] != payload["reason"]


# --------------------------------------------------------------------------- #
# run_turn in a pinned room (router + specialists + plain-language all stubbed)
# --------------------------------------------------------------------------- #

def _stub_turn(monkeypatch, *, route, results):
    """Wire chat.run_turn up with no LLM anywhere.

    `route` is the Route the router "returned"; `results` maps a specialist id to
    the SpecialistResult it produces. `asked` records the order agents were run in.
    """
    from app import board, chat, router as router_mod
    from app.specialist import SpecialistResult

    asked: list[str] = []
    seen: dict = {}

    def fake_route(message, history=None, profile=None, force_single=False):
        seen["force_single"] = force_single
        return route

    async def fake_run_specialists(ids, case_text, ledger, emit, timing, **kw):
        out = {}
        for sid in ids:
            asked.append(sid)
            res = results.get(sid)
            if callable(res):
                res = res(len([a for a in asked if a == sid]))
            out[sid] = res or SpecialistResult(sid, "error")
        return out

    monkeypatch.setattr(router_mod, "route", fake_route)
    monkeypatch.setattr(chat.router, "route", fake_route)
    monkeypatch.setattr(board, "run_specialists", fake_run_specialists)
    monkeypatch.setattr(board, "_plain_language", lambda md, **kw: md)
    return asked, seen


def _run(conv, message, **kwargs):
    from app import chat, sessions

    turn = sessions.new_turn(conv, message)
    events: list[tuple[str, dict]] = []
    result = asyncio.run(
        chat.run_turn(conv, turn, message, lambda t, p: events.append((t, p)), **kwargs)
    )
    return result, events


def _conv(specialist=""):
    from app import sessions
    return sessions.new_conversation({}, specialist=specialist)


def _route(mode="reply", specialists=("dietician",), focus=None, question=""):
    from app.router import Route
    return Route(
        mode=mode,
        specialists=list(specialists),
        focus=dict(focus or {}),
        clarifying_question=question,
    )


def test_a_pinned_room_answers_even_when_the_router_wanted_someone_else(monkeypatch):
    from app.specialist import SpecialistResult

    asked, seen = _stub_turn(
        monkeypatch,
        route=_route(specialists=["trials"], focus={"trials": "find studies"}),
        results={"dietician": SpecialistResult("dietician", "done",
                                               draft_markdown="Salt under 1500 mg [1].")},
    )
    result, _ = _run(_conv("dietician"), "how much salt?", pinned_specialist="dietician")

    assert asked == ["dietician"], "the room's agent answers, nobody else runs"
    assert result["specialist"] == "dietician"
    assert result["mode"] == "reply"
    assert "1500 mg" in result["markdown"]
    # The answer stands as the dietitian's — but the mismatch is signposted rather
    # than swallowed, so the patient learns the trials room exists.
    assert result["referral"] == {
        "to": "trials",
        "to_display_name": "Clinical Trials",
        "reason": "Clinical Trials can go further on this than Dietitian can.",
        "kind": "suggestion",
    }
    # The router still ran, and still ran in single-agent mode.
    assert seen["force_single"] is True


def test_no_referral_when_the_room_is_the_right_one(monkeypatch):
    """The suggestion must be rare, or it becomes noise the patient learns to ignore.

    Ask the dietitian about salt and triage picks the dietitian, so nothing fires.
    """
    from app.specialist import SpecialistResult

    _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"]),
        results={"dietician": SpecialistResult("dietician", "done",
                                               draft_markdown="Salt under 1500 mg [1].")},
    )
    result, events = _run(_conv("dietician"), "how much salt?", pinned_specialist="dietician")

    assert result["referral"] is None
    assert not [e for e in events if e[0] == "referral"]


def test_the_front_door_never_suggests_another_room(monkeypatch):
    """Nothing to signpost: the front door already opened whichever room triage chose."""
    from app.specialist import SpecialistResult

    _stub_turn(
        monkeypatch,
        route=_route(specialists=["trials"]),
        results={"trials": SpecialistResult("trials", "done",
                                            draft_markdown="Two studies are listed [1].")},
    )
    result, events = _run(_conv(), "are there trials?")  # no pin

    assert result["specialist"] == "trials"
    assert result["referral"] is None
    assert not [e for e in events if e[0] == "referral"]


def test_a_pinned_room_carries_the_routers_brief_when_it_has_one(monkeypatch):
    from app import board
    from app.specialist import SpecialistResult

    captured: dict = {}

    asked, _ = _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"], focus={"dietician": "salt in heart failure"}),
        results={"dietician": SpecialistResult("dietician", "done", draft_markdown="ok [1]")},
    )
    inner = board.run_specialists

    async def spy(ids, case_text, ledger, emit, timing, **kw):
        captured.update(kw.get("focus") or {})
        return await inner(ids, case_text, ledger, emit, timing, **kw)

    monkeypatch.setattr(board, "run_specialists", spy)
    _run(_conv("dietician"), "how much salt?", pinned_specialist="dietician")
    assert captured["dietician"] == "salt in heart failure"


def test_a_pinned_room_synthesizes_a_brief_when_the_router_briefed_someone_else(monkeypatch):
    from app import board, router
    from app.specialist import SpecialistResult

    captured: dict = {}
    _stub_turn(
        monkeypatch,
        route=_route(specialists=["trials"], focus={"trials": "find studies"}),
        results={"dietician": SpecialistResult("dietician", "done", draft_markdown="ok [1]")},
    )
    inner = board.run_specialists

    async def spy(ids, case_text, ledger, emit, timing, **kw):
        captured.update(kw.get("focus") or {})
        return await inner(ids, case_text, ledger, emit, timing, **kw)

    monkeypatch.setattr(board, "run_specialists", spy)
    _run(_conv("dietician"), "are there trials?", pinned_specialist="dietician")
    assert captured["dietician"] == router.generic_focus("are there trials?")


def test_a_skip_in_a_pinned_room_refers_instead_of_swapping_agents(monkeypatch):
    from app.specialist import SpecialistResult

    asked, _ = _stub_turn(
        monkeypatch,
        route=_route(specialists=["trials"]),
        results={"dietician": SpecialistResult("dietician", "skipped")},
    )
    result, events = _run(_conv("dietician"), "are there trials?",
                          pinned_specialist="dietician")

    # The generalist must NOT have quietly answered in the dietitian's name.
    assert asked == ["dietician"]
    assert result["referral"] == {
        "to": "trials",
        "to_display_name": "Clinical Trials",
        "reason": "This isn't something Dietitian can answer.",
        "kind": "handoff",
    }
    assert ("referral", result["referral"]) in events
    assert "Dietitian" in result["markdown"] and "Clinical Trials" in result["markdown"]
    assert result["specialist"] == "dietician"


def test_a_skip_falls_back_to_the_generalist_when_the_router_picked_this_room(monkeypatch):
    from app.specialist import SpecialistResult

    _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"]),
        results={"dietician": SpecialistResult("dietician", "skipped")},
    )
    result, _ = _run(_conv("dietician"), "what is my prognosis?",
                     pinned_specialist="dietician")
    assert result["referral"]["to"] == "researcher"


def test_a_skip_in_the_generalists_own_room_has_nobody_to_refer_to(monkeypatch):
    from app.specialist import SpecialistResult

    _stub_turn(
        monkeypatch,
        route=_route(specialists=["researcher"]),
        results={"researcher": SpecialistResult("researcher", "skipped")},
    )
    result, events = _run(_conv("researcher"), "unanswerable", pinned_specialist="researcher")
    assert result["referral"] is None
    assert not [e for e in events if e[0] == "referral"]
    assert "Medical Research" in result["markdown"]


def test_no_evidence_with_nothing_retrieved_apologizes_and_swaps_nobody(monkeypatch):
    from app import chat
    from app.specialist import SpecialistResult

    asked, _ = _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"]),
        results={"dietician": SpecialistResult("dietician", "no_evidence")},
    )
    result, _ = _run(_conv("dietician"), "obscure question", pinned_specialist="dietician")

    assert asked == ["dietician"], "nothing retrieved is not a reason to try someone else"
    assert result["referral"] is None
    assert chat._STATUS_APOLOGY["no_evidence"] in result["markdown"]


def test_no_evidence_despite_sources_retries_the_same_agent_once(monkeypatch):
    from app.specialist import SpecialistResult

    def dietician(attempt):
        if attempt == 1:
            return SpecialistResult("dietician", "no_evidence")
        return SpecialistResult("dietician", "done", draft_markdown="Under 1500 mg [1].")

    asked, _ = _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"]),
        results={"dietician": dietician},
    )
    conv = _conv("dietician")
    # Give the agent a retrieved source, which is what separates "over-strict
    # self-check" from "found nothing".
    conv.ledger.add(source_kind="patient_source", source_id="u1", title="T",
                    retrieved_by="dietician")

    result, _ = _run(conv, "how much salt?", pinned_specialist="dietician")
    assert asked == ["dietician", "dietician"], "same agent, exactly once more"
    assert result["referral"] is None
    assert "1500 mg" in result["markdown"]


def test_the_retry_is_discarded_when_it_does_no_better(monkeypatch):
    from app import chat
    from app.specialist import SpecialistResult

    _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"]),
        results={"dietician": SpecialistResult("dietician", "no_evidence")},
    )
    conv = _conv("dietician")
    conv.ledger.add(source_kind="patient_source", source_id="u1", title="T",
                    retrieved_by="dietician")
    result, _ = _run(conv, "how much salt?", pinned_specialist="dietician")
    assert chat._STATUS_APOLOGY["no_evidence"] in result["markdown"]


def test_clarify_still_wins_inside_a_room(monkeypatch):
    asked, _ = _stub_turn(
        monkeypatch,
        route=_route(mode="clarify", specialists=[],
                     question="Which condition is this about?"),
        results={},
    )
    result, _ = _run(_conv("physio"), "is it bad?", pinned_specialist="physio")

    assert asked == [], "clarify does no research at all"
    assert result["status"] == "clarify"
    assert result["specialist"] == "", "nobody answered — nobody researched"
    assert "Which condition" in result["markdown"]


def test_the_front_door_keeps_todays_silent_fallback(monkeypatch):
    from app.specialist import SpecialistResult

    asked, seen = _stub_turn(
        monkeypatch,
        route=_route(specialists=["dietician"]),
        results={
            "dietician": SpecialistResult("dietician", "skipped"),
            "researcher": SpecialistResult("researcher", "done", draft_markdown="Here [1]."),
        },
    )
    result, events = _run(_conv(), "something odd")   # no pinned_specialist

    assert asked == ["dietician", "researcher"], "unpinned still hands off silently"
    assert result["referral"] is None
    assert [e for e in events if e[0] == "fallback"]
    assert result["specialist"] == "researcher", "report who actually answered"
    assert seen["force_single"] is True, "the front door is single-agent too"


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app import ratelimit
    from app.server import app

    ratelimit.limiter._buckets.clear()
    with TestClient(app) as c:
        yield c


def test_team_endpoint_serves_the_room_cards(client):
    from app.config import researcher_ids

    data = client.get("/api/team").json()["specialists"]
    assert {s["id"] for s in data} == set(researcher_ids())
    assert "translator" not in {s["id"] for s in data}
    for entry in data:
        assert entry["tagline"] and entry["blurb"]
        assert len(entry["covers"]) >= 3
        assert len(entry["examples"]) >= 3


def test_an_unknown_specialist_is_a_400(client):
    r = client.post("/api/chat", json={"message": "hello there", "specialist": "psychic"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "psychic" in detail
    # The client should be able to fix itself from the message.
    assert "dietician" in detail


def test_the_translator_is_not_a_room_you_can_post_to(client):
    r = client.post("/api/chat", json={"message": "hello there", "specialist": "translator"})
    assert r.status_code == 400


def test_pinning_one_room_onto_another_rooms_conversation_is_a_409(client):
    from app import sessions

    conv = sessions.new_conversation({}, specialist="dietician")
    try:
        r = client.post("/api/chat", json={
            "message": "what about my knee?",
            "conversation_id": conv.cid,
            "specialist": "physio",
        })
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "dietician" in detail and "physio" in detail
        # Nothing may have started: cross-wiring would merge two rooms' ledgers.
        assert conv.turns == {}
    finally:
        sessions.CONVERSATIONS.pop(conv.cid, None)


def test_a_room_conversation_rejects_a_front_door_message(client):
    from app import sessions

    conv = sessions.new_conversation({}, specialist="dietician")
    try:
        r = client.post("/api/chat", json={
            "message": "hello there", "conversation_id": conv.cid,
        })
        assert r.status_code == 409
        assert conv.turns == {}
    finally:
        sessions.CONVERSATIONS.pop(conv.cid, None)


def test_conversation_state_reports_which_room_it_belongs_to(client):
    from app import sessions

    conv = sessions.new_conversation({}, specialist="physio")
    try:
        body = client.get(f"/api/chat/{conv.cid}").json()
        assert body["specialist"] == "physio"
    finally:
        sessions.CONVERSATIONS.pop(conv.cid, None)


def test_a_front_door_conversation_has_no_room():
    from app import sessions

    conv = sessions.new_conversation({})
    assert conv.specialist == ""
    sessions.CONVERSATIONS.pop(conv.cid, None)
