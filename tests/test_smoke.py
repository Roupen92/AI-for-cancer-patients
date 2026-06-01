"""Smoke tests: the project imports, config is coherent, SLP regex behaves."""
import pytest


def test_imports():
    from app import board, server, specialist, config, prompts, evidence, llm, sessions, language  # noqa: F401
    from app.tools import schemas_for, dispatch, all_tool_names  # noqa: F401


def test_specialist_configs_shape():
    from app.config import SPECIALIST_CONFIGS, researcher_ids, public_specialist_info
    # 6 researchers + translator
    assert set(SPECIALIST_CONFIGS.keys()) == {
        "physio", "dietician", "slp", "mental", "stories", "navigator", "translator"
    }
    assert "translator" not in researcher_ids()
    assert len(researcher_ids()) == 6

    for sid, cfg in SPECIALIST_CONFIGS.items():
        assert "display_name" in cfg
        assert "color" in cfg
        assert "system_prompt" in cfg
        assert "allowed_tools" in cfg

    roster = public_specialist_info()
    assert {r["id"] for r in roster} == set(SPECIALIST_CONFIGS.keys())


def test_navigator_has_soft_citation_gate():
    from app.config import SPECIALIST_CONFIGS
    assert SPECIALIST_CONFIGS["navigator"].get("soft_citation_gate") is True


def test_stories_agent_registered():
    from app.config import SPECIALIST_CONFIGS, researcher_ids
    cfg = SPECIALIST_CONFIGS.get("stories")
    assert cfg is not None
    assert cfg.get("soft_citation_gate") is True
    assert "patient_stories_search" in cfg["allowed_tools"]
    assert isinstance(cfg.get("podcast_allowlist"), dict)
    assert len(cfg["podcast_allowlist"]) >= 5
    assert "stories" in researcher_ids()


def test_patient_stories_search_tool_registered():
    from app.tools import all_tool_names, schemas_for
    names = all_tool_names()
    assert "patient_stories_search" in names
    schemas = schemas_for({"patient_stories_search"})
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "patient_stories_search"


def test_slp_is_conditional():
    from app.config import SPECIALIST_CONFIGS
    assert SPECIALIST_CONFIGS["slp"].get("conditional") is True


def test_translator_is_post_synthesis():
    from app.config import SPECIALIST_CONFIGS
    assert SPECIALIST_CONFIGS["translator"].get("role") == "post_synthesis"
    assert SPECIALIST_CONFIGS["translator"].get("allowed_tools") == set()


@pytest.mark.parametrize("case,expected", [
    ("Stage 2 breast cancer, lumpectomy planned", False),
    ("I have laryngeal cancer and trouble swallowing", True),
    ("Recently diagnosed with glioblastoma", True),
    ("Esophageal cancer, just starting chemo", True),
    ("Colon cancer, doing okay", False),
    ("Lung cancer, dysphagia after radiation", True),
])
def test_slp_relevance_regex(case, expected):
    from app.board import _slp_relevant
    assert _slp_relevant(case) == expected


def test_synthesizer_and_translator_prompts_exist():
    from app import prompts
    assert "SYNTHESIZER" in dir(prompts)
    assert "TRANSLATOR" in dir(prompts)
    assert "COMMON_PREFIX" in dir(prompts)
    assert "not medical advice" in prompts.COMMON_PREFIX.lower()


def test_patient_tools_registered():
    from app.tools import all_tool_names
    names = set(all_tool_names())
    assert "patient_source_search" in names
    assert "social_resource_search" in names
    assert "pubmed_search" in names
    assert "web_search" in names
    # Clinician-only tools should be absent
    assert "fda_approvals_search" not in names
    assert "civic_query" not in names
    assert "drug_interactions" not in names
    assert "clinical_trial_match_search" not in names


def test_is_english_handles_variants():
    from app.board import _is_english
    assert _is_english("English") is True
    assert _is_english("english") is True
    assert _is_english("en") is True
    assert _is_english("EN-US") is True
    assert _is_english("") is True
    assert _is_english("Spanish") is False
    assert _is_english("Mandarin Chinese") is False


def test_translator_run_norun_passthrough():
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
