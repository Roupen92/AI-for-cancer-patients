"""Smoke tests: the project imports, the specialist catalogue is coherent, the
router validates/clamps what the LLM hands back, and the safety screen fires.

Nothing here hits the network or an LLM.
"""
import pytest


def test_imports():
    from app import (  # noqa: F401
        board, chat, config, evidence, language, llm, prompts, router, safety,
        server, sessions, specialist,
    )
    from app.tools import schemas_for, dispatch, all_tool_names  # noqa: F401


# --------------------------------------------------------------------------- #
# Specialist catalogue
# --------------------------------------------------------------------------- #

EXPECTED_RESEARCHERS = {
    "researcher", "physio", "exercise", "dietician", "slp",
    "mental", "trials", "navigator", "stories",
}


def test_specialist_configs_shape():
    from app.config import SPECIALIST_CONFIGS, researcher_ids, public_specialist_info

    assert set(SPECIALIST_CONFIGS.keys()) == EXPECTED_RESEARCHERS | {"translator"}
    assert "translator" not in researcher_ids()
    assert set(researcher_ids()) == EXPECTED_RESEARCHERS

    for sid, cfg in SPECIALIST_CONFIGS.items():
        assert "display_name" in cfg
        assert "color" in cfg
        assert "system_prompt" in cfg
        assert "allowed_tools" in cfg

    roster = public_specialist_info()
    assert {r["id"] for r in roster} == set(SPECIALIST_CONFIGS.keys())


def test_every_researcher_has_a_section_heading():
    # The synthesizer's outline is generated from SECTION_HEADINGS/SECTION_ORDER;
    # a missing entry would silently drop that agent's section from the summary.
    from app.config import SECTION_HEADINGS, SECTION_ORDER, researcher_ids

    for sid in researcher_ids():
        assert sid in SECTION_HEADINGS, f"{sid} has no section heading"
        assert sid in SECTION_ORDER, f"{sid} is not in SECTION_ORDER"


def test_exactly_one_default_agent():
    from app.config import SPECIALIST_CONFIGS, default_specialist_id

    defaults = [sid for sid, cfg in SPECIALIST_CONFIGS.items() if cfg.get("default_agent")]
    assert defaults == ["researcher"]
    assert default_specialist_id() == "researcher"


def test_soft_citation_gates():
    from app.config import SPECIALIST_CONFIGS

    for sid in ("navigator", "stories", "trials"):
        assert SPECIALIST_CONFIGS[sid].get("soft_citation_gate") is True, sid
    # The clinical agents keep the hard gate: no citation, no answer.
    for sid in ("researcher", "physio", "exercise", "dietician", "slp", "mental"):
        assert not SPECIALIST_CONFIGS[sid].get("soft_citation_gate"), sid


def test_slp_is_conditional():
    from app.config import SPECIALIST_CONFIGS
    assert SPECIALIST_CONFIGS["slp"].get("conditional") is True


def test_translator_is_post_synthesis():
    from app.config import SPECIALIST_CONFIGS
    assert SPECIALIST_CONFIGS["translator"].get("role") == "post_synthesis"
    assert SPECIALIST_CONFIGS["translator"].get("allowed_tools") == set()


def test_pubmed_bias_uses_the_key_the_tool_reads():
    # config once used "mesh" while pubmed._apply_bias read "mesh_terms", which
    # silently disabled specialty biasing everywhere. Assert they agree.
    from app.config import SPECIALIST_CONFIGS
    from app.tools.pubmed import _apply_bias

    for sid, cfg in SPECIALIST_CONFIGS.items():
        bias = cfg.get("pubmed_bias")
        if not bias:
            continue
        assert "mesh_terms" in bias, f"{sid} uses a stale pubmed_bias key"
        biased = _apply_bias("fatigue", bias)
        assert "[MeSH]" in biased, f"{sid}'s bias did not apply"


def test_full_consult_roster_is_valid_and_excludes_overlap():
    from app.config import FULL_CONSULT_IDS, SPECIALIST_CONFIGS, researcher_ids

    for sid in FULL_CONSULT_IDS:
        assert sid in researcher_ids(), sid
        assert sid in SPECIALIST_CONFIGS
    # exercise overlaps physio, and trials is only right when asked for.
    assert "exercise" not in FULL_CONSULT_IDS
    assert "trials" not in FULL_CONSULT_IDS


def test_order_specialists_sorts_and_drops_unknowns():
    from app.config import order_specialists

    assert order_specialists(["stories", "researcher", "dietician"]) == [
        "researcher", "dietician", "stories"
    ]
    assert order_specialists(["nope", "physio", "physio"]) == ["physio"]
    assert order_specialists([]) == []


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def test_patient_tools_registered():
    from app.tools import all_tool_names
    names = set(all_tool_names())
    assert {"patient_source_search", "social_resource_search", "pubmed_search",
            "web_search", "patient_stories_search", "clinical_trials_search"} <= names
    # Prescriber-facing tools stay out: they invite dosing/interaction advice.
    assert "fda_approvals_search" not in names
    assert "civic_query" not in names
    assert "drug_interactions" not in names


def test_every_allowed_tool_actually_exists():
    from app.config import SPECIALIST_CONFIGS
    from app.tools import all_tool_names

    known = set(all_tool_names())
    for sid, cfg in SPECIALIST_CONFIGS.items():
        for tool in cfg["allowed_tools"]:
            assert tool in known, f"{sid} is allowed unregistered tool {tool}"


def test_trials_agent_has_the_registry_tool():
    from app.config import SPECIALIST_CONFIGS
    from app.tools import schemas_for

    assert "clinical_trials_search" in SPECIALIST_CONFIGS["trials"]["allowed_tools"]
    schemas = schemas_for({"clinical_trials_search"})
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "clinical_trials_search"
    assert "condition" in schemas[0]["function"]["parameters"]["required"]


def test_tool_context_memoizes_repeat_queries():
    # The trials agent was observed searching the registry 11 times with two
    # alternating queries because no result had a site near the patient. The memo
    # is what makes a repeat cheap to detect regardless of prompt compliance.
    from app.evidence import EvidenceLedger
    from app.tools import ToolContext

    ctx = ToolContext(specialist_id="trials", pubmed_bias=None, ledger=EvidenceLedger())
    assert ctx.already_asked("clinical_trials_search", "heart failure|UK|") is False
    assert ctx.already_asked("clinical_trials_search", "heart failure|UK|") is True
    # Normalized on case and whitespace.
    assert ctx.already_asked("clinical_trials_search", "Heart  Failure|UK|") is True
    # A different query, and the same query on a different tool, are both fresh.
    assert ctx.already_asked("clinical_trials_search", "stroke|UK|") is False
    assert ctx.already_asked("patient_source_search", "heart failure|UK|") is False


def test_two_tool_contexts_do_not_share_a_memo():
    from app.evidence import EvidenceLedger
    from app.tools import ToolContext

    ledger = EvidenceLedger()
    a = ToolContext(specialist_id="trials", pubmed_bias=None, ledger=ledger)
    b = ToolContext(specialist_id="trials", pubmed_bias=None, ledger=ledger)
    a.already_asked("clinical_trials_search", "heart failure||")
    assert b.already_asked("clinical_trials_search", "heart failure||") is False


def test_stories_tool_takes_a_condition_not_a_cancer_type():
    from app.tools import all_tool_names, schemas_for
    from app.tools.patient_stories_search import SCHEMA

    assert "patient_stories_search" in all_tool_names()
    assert schemas_for({"patient_stories_search"})[0]["function"]["name"] == "patient_stories_search"
    assert "condition" in SCHEMA["parameters"]["required"]
    assert "cancer_type" not in SCHEMA["parameters"]["required"]


def test_stories_config_still_has_a_podcast_allowlist():
    from app.config import SPECIALIST_CONFIGS
    cfg = SPECIALIST_CONFIGS["stories"]
    assert isinstance(cfg.get("podcast_allowlist"), dict)
    assert len(cfg["podcast_allowlist"]) >= 5
    # healthtalk covers 100+ conditions and is what makes this agent general.
    assert "healthtalk.org" in cfg["trusted_sources"]


def test_end_of_life_filter_defaults_to_on():
    from app.tools.patient_stories_search import _should_filter_end_of_life

    # Unknown stage, and any non-cancer condition, must filter.
    assert _should_filter_end_of_life("") is True
    assert _should_filter_end_of_life("II") is True
    # A patient who said stage IV can see stage-IV stories.
    assert _should_filter_end_of_life("IV") is False
    assert _should_filter_end_of_life("stage 4") is False


def test_resource_directories_lead_with_condition_agnostic_domains():
    from app.tools.social_resource_search import _DIRECTORIES, _normalize_country

    assert _normalize_country("USA") == "united states"
    assert _normalize_country("England") == "united kingdom"
    us = _DIRECTORIES["united states"]
    # The Brave fallback truncates to 6 site: clauses, so the first six must be
    # useful for someone who doesn't have cancer.
    assert not any("cancer" in d for d in us[:6]), us[:6]


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def test_core_prompts_exist():
    from app import prompts

    for name in (
        "COMMON_PREFIX", "CHAT_BREVITY", "RESEARCHER", "PHYSIO", "EXERCISE",
        "DIETICIAN", "SLP", "MENTAL_HEALTH", "SOCIAL_WORKER", "TRIALS", "STORIES",
        "TRANSLATOR", "SYNTHESIZER", "INSTITUTION_GLOSSARY", "PLAIN_LANGUAGE",
        "SELF_CHECK", "LAY_SUMMARY", "LOCATION_EXTRACTOR", "ROUTER",
        "CRISIS_LINES_BLOCK",
    ):
        assert getattr(prompts, name, "").strip(), f"prompts.{name} is missing or empty"


def test_prompts_are_condition_agnostic():
    from app import prompts

    assert "not medical advice" in prompts.COMMON_PREFIX.lower()
    # The team is "your care team", not "your oncology team" — this app now serves
    # every condition, and telling a diabetic to ask their oncologist is a bug.
    assert "oncology team" not in prompts.COMMON_PREFIX.lower()
    assert "WHAT TO ASK YOUR CARE TEAM" in prompts.COMMON_PREFIX
    for name in ("RESEARCHER", "PHYSIO", "EXERCISE", "DIETICIAN", "SLP",
                 "MENTAL_HEALTH", "SOCIAL_WORKER", "TRIALS", "STORIES"):
        body = getattr(prompts, name)
        assert "oncology team" not in body.lower(), f"{name} still says 'oncology team'"


def test_router_prompt_constrains_the_patient_facing_red_flag_fields():
    # `why` and `action` are shown to the patient verbatim. Live runs produced
    # both a clinician-voice essay in `why` and a UK emergency number for a
    # patient in Texas, so the prompt has to pin down voice, length and locale.
    from app import prompts

    assert "ARE SHOWN TO THE PATIENT WORD FOR WORD" in prompts.ROUTER
    assert "MATCH THE EMERGENCY NUMBER TO THE PATIENT'S COUNTRY" in prompts.ROUTER
    assert "your local emergency number" in prompts.ROUTER


def test_router_prompt_lists_every_agent_id():
    from app import prompts
    from app.config import researcher_ids

    for sid in researcher_ids():
        assert sid in prompts.ROUTER, f"router prompt never mentions {sid}"


def test_trials_prompt_forbids_eligibility_claims():
    from app import prompts
    low = prompts.TRIALS.lower()
    assert "eligib" in low
    assert "never say or imply the patient qualifies" in low


# --------------------------------------------------------------------------- #
# Safety screen
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    "I've had chest pain for the last hour and it's spreading to my arm",
    "my dad's face is drooping and he can't speak properly",
    "I can't breathe properly and it came on suddenly",
    "I'm coughing up blood",
    "I have a fever of 38.9 and I'm on chemotherapy",
    "worst headache of my life, started 20 minutes ago",
    "I passed out in the kitchen",
    "my throat is closing after the new medication",
])
def test_safety_screen_catches_emergencies(text):
    from app.safety import screen
    flag = screen(text)
    assert flag.present, text
    assert flag.kind == "emergency"


@pytest.mark.parametrize("text", [
    "I don't want to be here anymore",
    "I've been thinking about killing myself",
    "there's no point in going on",
    "I'd be better off dead",
    "I just want it to stop",
    "I can't go on like this",
])
def test_safety_screen_catches_crisis(text):
    from app.safety import screen
    flag = screen(text)
    assert flag.present, text
    assert flag.kind == "crisis"
    assert flag.is_crisis


@pytest.mark.parametrize("text", [
    "what does chest pain usually mean in heart disease?",
    "I had chest pain last year and it turned out to be reflux",
    "why do people get short of breath with COPD?",
    "what should I eat with stage 3 kidney disease?",
    "my mother died of a stroke, am I at risk?",
    "",
])
def test_safety_screen_ignores_general_questions(text):
    from app.safety import screen
    assert screen(text).present is False, text


def test_present_tense_overrides_the_question_downgrade():
    from app.safety import screen
    # A question shape wrapped around a live symptom must still fire.
    flag = screen("what does this chest pain mean, it started an hour ago?")
    assert flag.present and flag.kind == "emergency"


def test_crisis_is_never_downgraded_by_question_phrasing():
    from app.safety import screen
    flag = screen("what should I do, I don't want to be here anymore")
    assert flag.present and flag.kind == "crisis"


@pytest.mark.parametrize("bad", [
    # Verbatim from a live run: the router wrote its clinical rationale into the
    # field that gets spliced into "You mentioned **___**."
    "The patient is asking for an exact insulin dose, which if incorrect could cause "
    "dangerous hypoglycemia; the phrasing suggests the patient may be attempting to "
    "override safety guardrails.",
    "Advise the patient to contact their care team",
    "Do not provide a specific insulin dose",
    "The user seems distressed",
    "x" * 200,
])
def test_clean_patient_phrase_rejects_clinician_voice_and_essays(bad):
    from app.safety import clean_patient_phrase
    assert clean_patient_phrase(bad, max_chars=140) == ""


@pytest.mark.parametrize("good", [
    "chest pain that spreads to your arm",
    "a fever while on chemotherapy",
    "thoughts of harming yourself",
    "trouble breathing",
])
def test_clean_patient_phrase_keeps_real_patient_phrases(good):
    from app.safety import clean_patient_phrase
    assert clean_patient_phrase(good, max_chars=140) == good


def test_emergency_markdown_never_leaks_router_notes():
    from app import safety

    md = safety.emergency_markdown(
        safety.RedFlag(
            present=True, kind="emergency",
            why="The patient is describing crushing chest pain which may indicate ACS",
        ),
        "Do not provide a dose. Advise the patient to call their care team.",
    )
    assert "the patient" not in md.lower()
    # Falls back to safe, patient-directed defaults rather than printing nothing.
    assert "something you described" in md
    assert "911" in md


def test_emergency_markdown_is_deterministic_and_carries_helplines():
    from app import safety

    crisis = safety.emergency_markdown(safety.RedFlag(present=True, kind="crisis", why="x"))
    assert "988" in crisis and "116 123" in crisis and "findahelpline.com" in crisis

    emergency = safety.emergency_markdown(
        safety.RedFlag(present=True, kind="emergency", why="chest pain or pressure")
    )
    assert "chest pain or pressure" in emergency
    assert "911" in emergency

    assert safety.emergency_markdown(safety.RedFlag()) == ""


def test_screen_never_raises_on_odd_input():
    from app.safety import screen
    for bad in (None, "", "   ", "x" * 20000, "🙂🙂🙂", 12345):
        screen(bad if isinstance(bad, str) or bad is None else str(bad))


# --------------------------------------------------------------------------- #
# Router validation (no LLM — we feed it parsed dicts directly)
# --------------------------------------------------------------------------- #

def test_router_coerce_reply_takes_exactly_one():
    from app.router import _coerce_specialists

    ids, focus = _coerce_specialists(
        [{"id": "dietician", "focus": "salt limits"}, {"id": "physio", "focus": "x"}],
        "reply",
    )
    assert ids == ["dietician"]
    assert focus == {"dietician": "salt limits"}


def test_router_coerce_team_is_capped_by_preference_then_ordered():
    from app.config import MAX_TEAM_SIZE
    from app.router import _coerce_specialists

    ids, _ = _coerce_specialists(
        [{"id": "stories"}, {"id": "navigator"}, {"id": "researcher"},
         {"id": "dietician"}, {"id": "mental"}, {"id": "physio"}],
        "team",
    )
    assert len(ids) == MAX_TEAM_SIZE
    # The cap keeps the router's first four (its order is its preference), and
    # only then sorts them into SECTION_ORDER for presentation. mental/physio were
    # listed fifth and sixth, so they are the ones dropped.
    assert ids == ["researcher", "dietician", "navigator", "stories"]


def test_router_coerce_drops_unknown_ids_and_clarify_empties():
    from app.router import _coerce_specialists

    ids, _ = _coerce_specialists([{"id": "psychic"}, {"id": "researcher"}], "reply")
    assert ids == ["researcher"]
    assert _coerce_specialists([{"id": "researcher"}], "clarify") == ([], {})


def test_router_fallback_is_usable():
    from app.router import _fallback

    r = _fallback("what is eGFR?")
    assert r.mode == "reply"
    assert r.specialists == ["researcher"]
    assert r.focus_for("researcher")
    assert r.degraded is True


def test_router_fallback_still_carries_the_red_flag():
    # A router outage must not silently disable the emergency screen.
    from app.router import _fallback

    r = _fallback("I'm having chest pain right now")
    assert r.red_flag.present and r.red_flag.kind == "emergency"


def test_public_route_is_json_shaped():
    from app import router

    r = router.Route(
        mode="team",
        specialists=["researcher", "dietician"],
        focus={"researcher": "a", "dietician": "b"},
    )
    pub = router.public_route(r)
    assert pub["mode"] == "team"
    assert [s["id"] for s in pub["specialists"]] == ["researcher", "dietician"]
    assert all(s["display_name"] and s["color"] for s in pub["specialists"])
    assert pub["red_flag"] == {"present": False, "kind": "", "why": ""}


# --------------------------------------------------------------------------- #
# Board passes
# --------------------------------------------------------------------------- #

def test_is_english_handles_variants():
    from app.board import _is_english
    assert _is_english("English") is True
    assert _is_english("english") is True
    assert _is_english("en") is True
    assert _is_english("EN-US") is True
    assert _is_english("") is True
    assert _is_english("Spanish") is False
    assert _is_english("Mandarin Chinese") is False


def test_translator_norun_passthrough():
    # Translator should return English markdown unchanged when target is English,
    # without calling the LLM.
    from app.board import _translate
    md = "## Hello\n\nThis is a test [1]."
    assert _translate(md, "English") == md
    assert _translate(md, "en") == md
    assert _translate(md, "") == md


def test_normalize_language_default():
    from app.language import normalize_language
    assert normalize_language("") == "English"
    assert normalize_language("  Spanish  ") == "Spanish"
    assert normalize_language(None) == "English"


def test_plain_language_rejects_a_pass_that_drops_citations(monkeypatch):
    from app import board

    src = "Walk 30 minutes, 3 times a week [4]. Aim for 1.5 g of protein per kg [7]."

    def fake_chat(messages, **kwargs):
        class M:
            content = "Walk regularly [4]. Get enough protein."
        class C:
            message = M()
        class R:
            choices = [C()]
        return R()

    monkeypatch.setattr(board.llm, "chat", fake_chat)
    assert board._plain_language(src) == src   # [7] was lost → keep the original


def test_plain_language_rejects_a_pass_that_flattens_the_numbers(monkeypatch):
    from app import board

    src = (
        "Aim for 1.2 to 1.5 grams of protein per kilogram per day [4]. "
        "Do 30 minutes of cycling at 60 to 70 percent of your maximum heart rate, "
        "3 times a week for 12 weeks [5]. Keep salt under 1500 mg a day [6]."
    )

    def fake_chat(messages, **kwargs):
        class M:
            content = (
                "Try to eat enough protein [4]. Do some aerobic exercise most weeks [5]. "
                "Go easy on salt [6]. Ask your care team what targets fit you, and keep "
                "a note of what you manage each week so you can show them."
            )
        class C:
            message = M()
        class R:
            choices = [C()]
        return R()

    monkeypatch.setattr(board.llm, "chat", fake_chat)
    assert board._plain_language(src) == src   # numbers gone → keep the original


def test_plain_language_accepts_a_faithful_simplification(monkeypatch):
    from app import board

    src = "It is recommended that patients maintain a sodium intake below 1500 mg daily [6]."
    better = (
        "Try to keep salt (sodium) under 1500 mg a day [6]. That is about two-thirds of a "
        "teaspoon of table salt for the whole day, counting what is already in packaged food."
    )

    def fake_chat(messages, **kwargs):
        class M:
            content = better
        class C:
            message = M()
        class R:
            choices = [C()]
        return R()

    monkeypatch.setattr(board.llm, "chat", fake_chat)
    assert board._plain_language(src) == better


def test_plain_language_survives_an_llm_failure(monkeypatch):
    from app import board

    def boom(messages, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(board.llm, "chat", boom)
    src = "Keep salt under 1500 mg a day [6]."
    assert board._plain_language(src) == src


def test_sections_block_only_lists_agents_that_produced_drafts():
    from app.board import _sections_block
    from app.specialist import SpecialistResult

    history = {
        "researcher": SpecialistResult("researcher", "done", draft_markdown="x"),
        "slp": SpecialistResult("slp", "skipped"),
        "dietician": SpecialistResult("dietician", "done", draft_markdown="y"),
    }
    block = _sections_block(["researcher", "slp", "dietician"], history)
    assert "What the evidence says" in block
    assert "Eating and nutrition" in block
    assert "Swallowing" not in block          # skipped → no heading, no placeholder
    # Order follows SECTION_ORDER.
    assert block.index("What the evidence says") < block.index("Eating and nutrition")


def test_sections_block_handles_an_empty_round():
    from app.board import _sections_block
    assert "none" in _sections_block([], {}).lower()


@pytest.mark.parametrize("case,expected", [
    ("Stage 2 breast cancer, lumpectomy planned", False),
    ("I have laryngeal cancer and trouble swallowing", True),
    ("Recently diagnosed with glioblastoma", True),
    ("Esophageal cancer, just starting chemo", True),
    ("Colon cancer, doing okay", False),
    ("Lung cancer, dysphagia after radiation", True),
    # Generalized beyond cancer:
    ("I had a stroke and now I have trouble finding words", True),
    ("Parkinson's disease, my voice has gone quiet", True),
    ("I keep choking on water", True),
    ("Type 2 diabetes, want to know about diet", False),
    ("Heart failure, ankles swelling", False),
])
def test_slp_relevance_regex(case, expected):
    from app.board import _slp_relevant
    assert _slp_relevant(case) == expected


# --------------------------------------------------------------------------- #
# Chat helpers
# --------------------------------------------------------------------------- #

def test_strip_internal_markers_removes_the_handoff_line():
    from app.chat import _strip_internal_markers

    md = (
        "## What helps\n\nDrink fluids [2].\n\n"
        "RECOMMENDATION SUMMARY: Stay hydrated and ask about salt targets."
    )
    out = _strip_internal_markers(md)
    assert "RECOMMENDATION SUMMARY" not in out
    assert "Drink fluids [2]." in out


def test_strip_internal_markers_handles_a_bolded_marker():
    from app.chat import _strip_internal_markers
    md = "Body text [1].\n\n**RECOMMENDATION SUMMARY:** take-home here."
    assert "RECOMMENDATION SUMMARY" not in _strip_internal_markers(md)


def test_strip_internal_markers_drops_a_leading_abstain_prefix():
    from app.chat import _strip_internal_markers
    assert _strip_internal_markers("ABSTAIN: could not find sources.").startswith("could not")


def test_prettify_markers_makes_the_ask_block_a_heading():
    from app.chat import _prettify_markers

    out = _prettify_markers("Body.\n\nWHAT TO ASK YOUR CARE TEAM:\n\n- Question one?")
    assert "### What to ask your care team" in out
    assert "WHAT TO ASK YOUR CARE TEAM" not in out


@pytest.mark.parametrize("cue", [
    "WHEN TO CALL YOUR CARE TEAM:",
    "WHEN TO CALL YOUR CARE TEAM RIGHT AWAY:",
    "WHEN TO CALL YOUR CARE TEAM IMMEDIATELY:",
    "WHEN TO CALL YOUR CARE TEAM NOW:",
    "**WHEN TO CALL YOUR CARE TEAM URGENTLY:**",
])
def test_prettify_markers_handles_every_red_flag_cue_variant(cue):
    # An unmatched variant reaches the patient as shouting — and in a translated
    # answer the translator faithfully reproduces the capitals in their language.
    from app.chat import _prettify_markers

    out = _prettify_markers(f"Body [1].\n\n{cue} if you swell up.")
    assert "WHEN TO CALL" not in out, out
    assert "When to call your care team" in out


def test_prettify_markers_handles_the_call_if_cue():
    from app.chat import _prettify_markers

    for cue in ("CALL YOUR CARE TEAM RIGHT AWAY IF:", "CALL YOUR CARE TEAM IMMEDIATELY IF:"):
        out = _prettify_markers(f"{cue}\n\n- fever")
        assert "Call your care team" in out
        assert cue not in out


def test_translator_prompt_requires_translating_headings():
    # Observed live: the translator left "### What to ask your care team" in
    # English inside an otherwise-Spanish answer, because "preserve the
    # structure" read as "preserve the wording".
    from app import prompts

    assert "HEADINGS ARE PROSE" in prompts.TRANSLATOR
    assert "does NOT mean keep their wording in English" in prompts.TRANSLATOR


def test_prettify_markers_is_idempotent():
    # The substitutions are case-sensitive so a second pass can't re-wrap the
    # nicely-cased heading the first pass produced.
    from app.chat import _prettify_markers

    once = _prettify_markers("Body.\n\nWHAT TO ASK YOUR CARE TEAM:\n\n- Q?")
    assert _prettify_markers(once) == once


def test_references_in_only_returns_cited_entries():
    from app.chat import _references_in
    from app.evidence import EvidenceLedger

    ledger = EvidenceLedger()
    a = ledger.add(source_kind="pubmed", source_id="1", title="A", retrieved_by="researcher")
    b = ledger.add(source_kind="pubmed", source_id="2", title="B", retrieved_by="researcher")
    ledger.mark_cited(a.label, "researcher")
    ledger.mark_cited(b.label, "researcher")

    refs = _references_in(ledger, f"Only the first one is cited [{a.label}].")
    assert [r["label"] for r in refs] == [a.label]


def test_conversation_ledger_labels_are_stable_across_turns():
    from app import sessions

    conv = sessions.new_conversation({"condition": "heart failure"})
    first = conv.ledger.add(source_kind="patient_source", source_id="u1", title="T1",
                            retrieved_by="dietician")
    second = conv.ledger.add(source_kind="patient_source", source_id="u2", title="T2",
                             retrieved_by="physio")
    # Re-retrieving the same URL on a later turn must reuse the same label.
    again = conv.ledger.add(source_kind="patient_source", source_id="u1", title="T1",
                            retrieved_by="researcher")
    assert first.label == "1" and second.label == "2"
    assert again.label == first.label
    sessions.CONVERSATIONS.pop(conv.cid, None)


def test_conversation_history_is_bounded():
    from app import sessions

    conv = sessions.new_conversation()
    for i in range(sessions.MAX_MESSAGES_PER_CONVERSATION + 12):
        conv.add_message("user", f"m{i}")
    assert len(conv.messages) == sessions.MAX_MESSAGES_PER_CONVERSATION
    # The oldest were dropped, the newest kept.
    assert conv.messages[-1]["content"].endswith(str(sessions.MAX_MESSAGES_PER_CONVERSATION + 11))
    sessions.CONVERSATIONS.pop(conv.cid, None)


def test_specialist_catalogue_matches_config():
    from app.chat import specialist_catalogue
    from app.config import researcher_ids

    cat = specialist_catalogue()
    assert {c["id"] for c in cat} == set(researcher_ids())
    assert all(c["display_name"] and c["color"] and c["section_heading"] for c in cat)


# --------------------------------------------------------------------------- #
# HTTP surface (no LLM: only routes that don't start a turn)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.server import app

    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/consult", "/about", "/privacy"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "Patient Guide" in r.text


def test_chat_page_loads_the_chat_client_and_consult_loads_its_own(client):
    # The two views must not share a script: consult.js drives the form flow and
    # would throw on the chat page (and vice versa).
    assert "/static/app.js" in client.get("/").text
    assert "/static/consult.js" in client.get("/consult").text
    for path in ("/", "/consult"):
        assert "/static/shared.js" in client.get(path).text


def test_team_endpoint_lists_every_agent(client):
    from app.config import researcher_ids

    data = client.get("/api/team").json()
    assert {s["id"] for s in data["specialists"]} == set(researcher_ids())


def test_unknown_conversation_is_a_clear_404(client):
    r = client.get("/api/chat/cv_doesnotexist")
    assert r.status_code == 404

    r = client.post("/api/chat", json={"message": "hello there", "conversation_id": "cv_gone"})
    assert r.status_code == 404
    # The client retries as a fresh conversation on this, so the message has to
    # say the conversation expired rather than something generic.
    assert "expired" in r.json()["detail"].lower()


def test_chat_rejects_an_empty_message(client):
    assert client.post("/api/chat", json={"message": "x"}).status_code == 422
    assert client.post("/api/chat", json={}).status_code == 422


def test_board_state_404s_for_unknown_session(client):
    assert client.get("/api/board/tb_nope").status_code == 404


@pytest.mark.parametrize("raw,expected", [
    ('{"mode":"reply"}', {"mode": "reply"}),
    ('```json\n{"mode":"reply"}\n```', {"mode": "reply"}),
    ('Here you go:\n{"mode":"reply"}\nHope that helps!', {"mode": "reply"}),
    ("", None),
    ("not json at all", None),
])
def test_json_payload_parsing_tolerates_model_mangling(raw, expected):
    from app.llm import _parse_json_payload
    assert _parse_json_payload(raw) == expected


def test_chat_json_retries_a_malformed_reply(monkeypatch):
    # Observed live: GLM returns unparseable JSON roughly 1 call in 30, and the
    # router calls chat_json on EVERY turn — without a retry those turns lose
    # their routing and silently fall back to the generalist.
    from app import llm

    calls = {"n": 0}

    def flaky(messages, **kwargs):
        calls["n"] += 1
        body = "" if calls["n"] == 1 else '{"mode":"team"}'

        class M:
            content = body
        class C:
            message = M()
        class R:
            choices = [C()]
        return R()

    monkeypatch.setattr(llm, "chat", flaky)
    assert llm.chat_json([{"role": "user", "content": "x"}]) == {"mode": "team"}
    assert calls["n"] == 2, "should have retried once"


def test_chat_json_gives_up_after_its_attempts(monkeypatch):
    from app import llm

    calls = {"n": 0}

    def always_bad(messages, **kwargs):
        calls["n"] += 1

        class M:
            content = "still not json"
        class C:
            message = M()
        class R:
            choices = [C()]
        return R()

    monkeypatch.setattr(llm, "chat", always_bad)
    with pytest.raises(ValueError, match="unparseable JSON"):
        llm.chat_json([{"role": "user", "content": "x"}], parse_attempts=2)
    assert calls["n"] == 2


def test_rate_limiter_allows_a_normal_patient_then_stops_a_script():
    from app.ratelimit import RateLimiter

    rl = RateLimiter(per_hour=5, per_day=100)
    for i in range(5):
        allowed, _, _ = rl.check("1.2.3.4")
        assert allowed, f"blocked a legitimate request at #{i + 1}"

    allowed, retry_after, reason = rl.check("1.2.3.4")
    assert allowed is False
    assert reason == "hour"
    assert 0 < retry_after <= 3600


def test_rate_limiter_is_per_client():
    from app.ratelimit import RateLimiter

    rl = RateLimiter(per_hour=2, per_day=100)
    rl.check("a"); rl.check("a")
    assert rl.check("a")[0] is False
    # A different address behind the same proxy must be unaffected.
    assert rl.check("b")[0] is True


def test_rate_limiter_daily_cap_reports_the_day_reason():
    from app.ratelimit import RateLimiter

    rl = RateLimiter(per_hour=0, per_day=3)
    for _ in range(3):
        assert rl.check("x")[0] is True
    allowed, retry_after, reason = rl.check("x")
    assert allowed is False and reason == "day"
    assert retry_after > 3600


def test_rate_limiter_can_be_disabled():
    from app.ratelimit import RateLimiter

    rl = RateLimiter(per_hour=0, per_day=0)
    for _ in range(500):
        assert rl.check("x")[0] is True


def test_rate_limit_client_key_prefers_the_original_client(monkeypatch):
    from app.ratelimit import client_key

    class Req:
        def __init__(self, headers, host="127.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    # Railway puts the real client first and the proxy chain after it.
    assert client_key(Req({"x-forwarded-for": "203.0.113.9, 10.0.0.1, 10.0.0.2"})) == "203.0.113.9"
    assert client_key(Req({"x-real-ip": "203.0.113.7"})) == "203.0.113.7"
    assert client_key(Req({})) == "127.0.0.1"


def test_rate_limit_message_is_kind_and_points_at_the_care_team():
    from app.ratelimit import friendly_message

    hourly = friendly_message(600, "hour")
    daily = friendly_message(7200, "day")
    for msg in (hourly, daily):
        # Someone unwell should not be scolded for asking too many questions,
        # and must never be left waiting on us when something is urgent.
        assert "care team" in msg
        assert "error" not in msg.lower()
    assert "10 minute" in hourly
    assert "tomorrow" in daily


def test_no_log_call_interpolates_patient_text():
    """The privacy page tells patients "we do not log the content of what you
    shared". Every search tool used to log its query on a failure path, and those
    queries are built from the patient's own words — "metastatic pancreatic cancer
    diet Manchester" describes a person's health and where they live. This fails
    if anyone puts raw patient-derived text back into a log line.
    """
    import re
    from pathlib import Path

    offenders = []
    for path in sorted((Path(__file__).parent.parent / "app").rglob("*.py")):
        src = path.read_text()
        # Join wrapped log calls so multi-line ones are checked too.
        flat = re.sub(r"\n\s+", " ", src)
        for call in re.findall(r"log\.\w+\((?:[^()]|\([^()]*\))*\)", flat):
            if "scrub(" in call:
                continue
            # A raw slice of one of these names is patient-derived text.
            if re.search(r"\b(query|raw_query|q|last_raw|case|message|text|draft|content)\[:\d+\]", call):
                offenders.append(f"{path.name}: {call[:110]}")
    assert not offenders, "patient text in log calls:\n  " + "\n  ".join(offenders)


def test_content_logging_is_opt_in_and_off_by_default():
    from app import logsafe

    assert logsafe.LOG_CONTENT is False, "content logging must default to off"
    redacted = logsafe.scrub("metastatic pancreatic cancer diet Manchester")
    assert "cancer" not in redacted
    assert "Manchester" not in redacted
    # Length is kept so a truncation bug is still diagnosable.
    assert "44c" in redacted


def test_timing_log_is_switchable_and_carries_no_content(monkeypatch, caplog):
    import logging
    from app import board, logsafe

    summary = board._build_timing_summary(
        {"specialists": {"researcher": {"wall": 9.0, "llm": 8.0, "tool": 1.0, "llm_n": 2, "tool_n": 1}},
         "tools": {}, "synth": 0.0, "gloss": 0.0, "plain": 1.0, "translate": 0.0, "router": 2.0},
        12.0,
    )
    with caplog.at_level(logging.INFO):
        board.log_timing("turn cv_x/t_y", summary, mode="reply")
    line = caplog.text
    assert "TIMING" in line and "total=12.0s" in line
    # Ids and numbers only — no free text from the patient.
    assert "researcher" in line

    caplog.clear()
    monkeypatch.setattr(logsafe, "LOG_TIMING", False)
    with caplog.at_level(logging.INFO):
        board.log_timing("turn cv_x/t_y", summary, mode="reply")
    assert "TIMING" not in caplog.text


def test_pages_stamp_a_version_on_asset_urls(client):
    """Returning visitors were served fresh HTML with the PREVIOUS build's
    styles.css and app.js, so the chat rendered unstyled with no helper chips.
    Revalidation headers alone cannot fix a browser that never asks — the asset
    URL has to change, so it carries a hash of the asset contents.
    """
    import re
    from app.server import ASSET_VERSION

    for path in ("/", "/consult"):
        html = client.get(path).text
        assets = re.findall(r'(?:href|src)="/static/([^"?]+)(\?v=([a-f0-9]+))?"', html)
        for name, query, version in assets:
            if name.endswith((".css", ".js")):
                assert query, f"{path} -> /static/{name} has no cache-busting version"
                assert version == ASSET_VERSION


def test_asset_version_tracks_content(tmp_path, monkeypatch):
    from app import server

    first = server._compute_asset_version()
    assert first == server._compute_asset_version(), "version must be stable"

    css = server.STATIC_DIR / "styles.css"
    original = css.read_bytes()
    try:
        css.write_bytes(original + b"\n/* touched */\n")
        assert server._compute_asset_version() != first, "version must change with content"
    finally:
        css.write_bytes(original)
    assert server._compute_asset_version() == first


def test_pages_and_static_ask_the_browser_to_revalidate(client):
    # Without this the browser applies heuristic freshness and can serve a stale
    # bundle for hours with no way for a deploy to reach it.
    page = client.get("/")
    assert "no-cache" in page.headers.get("cache-control", "")

    asset = client.get("/static/styles.css")
    assert asset.status_code == 200
    assert "no-cache" in asset.headers.get("cache-control", "")


def test_health_reports_how_the_caller_was_identified(client):
    # If proxy header handling were wrong every visitor would share one bucket and
    # real patients would throttle each other. This is how that stays observable
    # in production without spending credits to find out.
    body = client.get("/api/health", headers={"x-forwarded-for": "203.0.113.44, 10.0.0.1"}).json()
    rl = body["rate_limit"]
    assert rl["client"] == "203.0.113.44"
    assert set(rl) == {"client", "used_this_hour", "used_today", "per_hour", "per_day"}
    # Reading health must not consume any of the caller's quota.
    again = client.get("/api/health", headers={"x-forwarded-for": "203.0.113.44"}).json()
    assert again["rate_limit"]["used_this_hour"] == rl["used_this_hour"]


def test_health_endpoint_is_free_unless_you_ask_it_to_probe(client):
    # Probing spends tokens and a search query, so an uptime pinger hitting
    # /api/health must not trigger it.
    body = client.get("/api/health").json()
    assert body["probed"] is False
    assert set(body["llm"]) == {"provider", "model", "key_configured"}
    assert set(body["search"]) == {"perplexity", "brave", "any_configured"}
    assert "pubmed" in body["keyless_sources"]


def test_health_snapshot_reads_every_accepted_key_alias(monkeypatch):
    from app import health

    for var in ("PERPLEXITY_API_KEY", "PPLX_API_KEY", "Brave_API",
                "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    snap = health.snapshot()
    assert snap["search"] == {"perplexity": False, "brave": False, "any_configured": False}

    # The alias spellings must count, or health would cry wolf on a working setup.
    monkeypatch.setenv("PPLX_API_KEY", "x")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "y")
    snap = health.snapshot()
    assert snap["search"]["perplexity"] and snap["search"]["brave"]
    assert snap["search"]["any_configured"] is True


def test_health_startup_logging_warns_when_search_is_gone(monkeypatch, caplog):
    import logging

    from app import health

    for var in ("PERPLEXITY_API_KEY", "PPLX_API_KEY", "Brave_API",
                "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with caplog.at_level(logging.INFO, logger="app.health"):
        health.log_startup_state()
    assert any("No search backend configured" in r.message for r in caplog.records)


def test_rtl_direction_is_stamped_on_top_level_blocks_only():
    """Answers get translated into right-to-left languages, so rendered markdown
    carries dir="auto". The subtlety: the dir=auto algorithm resolves direction
    from an element's first strong character but SKIPS descendants that have
    their own dir attribute. Stamping nested nodes therefore blinds their
    parent — dir on <li> makes the <ul> fall back to ltr, and dir on a
    blockquote's inner <p> does the same to the blockquote — putting bullets and
    quote bars on the wrong side of an Arabic answer.
    """
    from pathlib import Path

    shared = (Path(__file__).parent.parent / "static" / "shared.js").read_text()

    assert 'target.setAttribute("dir", "auto")' in shared
    assert "Array.from(target.children).forEach" in shared
    # No blanket descendant stamping.
    assert 'querySelectorAll("p, li' not in shared
    assert 'querySelectorAll("p, ul' not in shared

    import re

    styles = (Path(__file__).parent.parent / "static" / "styles.css").read_text()
    # Strip comments — the stylesheet documents why [dir="rtl"] is wrong, and the
    # explanation must not read as a violation.
    rules = re.sub(r"/\*.*?\*/", "", styles, flags=re.S)
    # Logical properties are what actually flip; physical left/right cannot, and
    # a [dir="rtl"] selector never matches because the attribute value is "auto".
    assert "margin-inline-start" in rules
    assert "border-inline-start" in rules
    assert '[dir="rtl"]' not in rules


def test_reference_urls_go_through_the_scheme_guard():
    """Reference URLs come from third-party search results and are rendered as
    clickable links. The markdown body is sanitized by DOMPurify, but the
    reference list and citation tooltip build their anchors by hand — escaping
    stops an attribute breakout but not a `javascript:` scheme. Guard both.
    """
    import re
    from pathlib import Path

    shared = (Path(__file__).parent.parent / "static" / "shared.js").read_text()

    assert "function safeUrl(" in shared
    # Absolute http(s) only: parsed with no base so relative junk is rejected too.
    assert 'parsed.protocol === "http:" || parsed.protocol === "https:"' in shared

    # No anchor may be built from a raw url expression — it has to go via safeUrl.
    raw_hrefs = re.findall(r'href="\$\{escape(?:Attr|Html)\((\w+(?:\.\w+)*)\)\}"', shared)
    for expr in raw_hrefs:
        assert expr in ("href", "ttHref"), f"anchor built from unguarded {expr!r}"
    assert raw_hrefs, "expected to find the reference/tooltip anchors"
