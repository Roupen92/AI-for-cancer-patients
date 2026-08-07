"""Tool registry for the patient-facing app.

Slimmed down from the Tumor Board: prescriber-facing tools (FDA approvals, CIViC
variants, RxNorm interaction checks) are NOT here — they would invite the model
into prescriptive territory. Added: curated patient-facing search, a
geography-aware resource directory, patient-story retrieval, and a patient-safe
ClinicalTrials.gov search (guardrailed by prompts.TRIALS, which forbids any
statement about eligibility).
"""
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from app.evidence import EvidenceLedger
from app.tools import (
    pubmed,
    europe_pmc,
    semantic_scholar,
    brave_search,
    patient_source_search,
    social_resource_search,
    patient_stories_search,
    clinical_trials,
    genomics_lookup,
)


@dataclass
class ToolContext:
    specialist_id: str
    pubmed_bias: dict | None
    ledger: EvidenceLedger
    # Per-run memo of queries already issued, so a tool can short-circuit an
    # agent that keeps re-asking the same thing. Observed live: the trials agent
    # searched the registry 11 times with two alternating queries because the
    # results contained no site near the patient. Keyed by tool name.
    seen_queries: dict[str, set[str]] = field(default_factory=dict)

    def already_asked(self, tool: str, key: str) -> bool:
        """Record a query and report whether it was issued earlier this run."""
        seen = self.seen_queries.setdefault(tool, set())
        normalized = " ".join((key or "").lower().split())
        if normalized in seen:
            return True
        seen.add(normalized)
        return False


_REGISTRY: dict[str, tuple[dict, Callable[[dict, ToolContext], Awaitable[str]]]] = {
    "pubmed_search":           (pubmed.SEARCH_SCHEMA,            pubmed.run_search),
    "pubmed_fetch":            (pubmed.FETCH_SCHEMA,             pubmed.run_fetch),
    "pubmed_search_and_fetch": (pubmed.SEARCH_AND_FETCH_SCHEMA,  pubmed.run_search_and_fetch),
    "europe_pmc_search":       (europe_pmc.SCHEMA,               europe_pmc.run),
    "semantic_scholar_search": (semantic_scholar.SCHEMA,         semantic_scholar.run),
    "web_search":              (brave_search.SCHEMA,             brave_search.run),
    "patient_source_search":   (patient_source_search.SCHEMA,    patient_source_search.run),
    "social_resource_search":  (social_resource_search.SCHEMA,   social_resource_search.run),
    "patient_stories_search":  (patient_stories_search.SCHEMA,   patient_stories_search.run),
    "clinical_trials_search":  (clinical_trials.SCHEMA,          clinical_trials.run),
    "genomics_lookup":         (genomics_lookup.SCHEMA,          genomics_lookup.run),
}


def schemas_for(allowed: set[str]) -> list[dict]:
    return [
        {"type": "function", "function": _REGISTRY[name][0]}
        for name in allowed
        if name in _REGISTRY
    ]


async def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    if name not in _REGISTRY:
        return f"Tool '{name}' is not available to you."
    _, runner = _REGISTRY[name]
    try:
        return await runner(args, ctx)
    except Exception as e:
        return f"Tool '{name}' failed: {type(e).__name__}: {e}"


def all_tool_names() -> list[str]:
    return list(_REGISTRY.keys())
