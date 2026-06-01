"""Run a single patient-care specialist as a tool-loop, with self-check and
event emission. Mirrors the Tumor Board specialist runner; the main difference
is a `soft_citation_gate` config flag — the Patient Navigator's value lies in
naming real-world programs, not citing PubMed papers, so it isn't forced to
abstain when literature evidence is thin."""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from app import llm, prompts
from app.config import SPECIALIST_CONFIGS, MAX_TOOL_ITERATIONS
from app.evidence import EvidenceLedger
from app.tools import ToolContext, schemas_for, dispatch

log = logging.getLogger(__name__)


@dataclass
class SpecialistResult:
    specialist_id: str
    status: str
    draft_markdown: str = ""
    recommendation_summary: str = ""
    evidence_labels: list[str] = field(default_factory=list)
    error: str = ""


SKIP_MARKER = re.compile(r"^\s*SKIP\s*:", re.IGNORECASE | re.MULTILINE)
RECOMMENDATION_MARKER = re.compile(r"RECOMMENDATION\s+SUMMARY\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
ABSTAIN_MARKER = re.compile(r"^\s*ABSTAIN\s*:", re.IGNORECASE | re.MULTILINE)

MAX_TOOL_RESULT_CHARS_IN_HISTORY = 1800


def _tool_call_dict(tc) -> dict:
    """Serialize a tool_call, preserving Gemini thought_signature via extra_content."""
    d = {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
    }
    extra = (getattr(tc, "model_extra", None) or {}).get("extra_content")
    if extra:
        d["extra_content"] = extra
    return d


async def _timed_chat(messages, emit, *, tools=None, phase="llm"):
    t0 = time.perf_counter()
    resp = await asyncio.to_thread(llm.chat, messages, tools=tools)
    emit("llm_timing", {"seconds": round(time.perf_counter() - t0, 3), "phase": phase})
    return resp


async def _dispatch_tool_calls(tool_calls, ctx, emit, messages, result_char_cap):
    parsed = []
    for tc in tool_calls:
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        emit("tool_call", {"tool": name, "args": args})
        parsed.append((tc, name, args))

    async def _timed_dispatch(name, args):
        t0 = time.perf_counter()
        result = await dispatch(name, args, ctx)
        return result, time.perf_counter() - t0

    results = await asyncio.gather(*(_timed_dispatch(name, args) for (_, name, args) in parsed))
    for (tc, name, _), (result, dt) in zip(parsed, results):
        preview = (result[:280] + "…") if len(result) > 280 else result
        emit("tool_result", {"tool": name, "preview": preview, "seconds": round(dt, 3)})
        stored = (
            result[:result_char_cap]
            + "\n\n…[result truncated; full content was used to inform earlier reasoning]"
            if len(result) > result_char_cap
            else result
        )
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": stored})


_SUMMARY_HEADER_ONLY = re.compile(r"#*\s*RECOMMENDATION\s+SUMMARY\s*:?\s*", re.IGNORECASE)


def _extract_summary(draft: str) -> str:
    m = RECOMMENDATION_MARKER.search(draft)
    if m:
        text = m.group(1).strip()
        text = re.sub(r"^\s*(?:\*{1,3}|_{1,3}|#+)(?=\s|$)", "", text).strip()
        if text:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            joined = " ".join(sentences[:3]).strip()
            if joined:
                return joined
    for para in draft.split("\n\n"):
        para = para.strip()
        if para and not _SUMMARY_HEADER_ONLY.fullmatch(para):
            return para[:600]
    return (draft or "").strip()[:300]


async def _run_tool_loop(
    spec_id: str,
    case: str,
    context_prefix: str,
    ledger: EvidenceLedger,
    emit,
    *,
    result_char_cap: int = MAX_TOOL_RESULT_CHARS_IN_HISTORY,
) -> tuple[str, list[dict]]:
    cfg = SPECIALIST_CONFIGS[spec_id]
    tools = schemas_for(cfg["allowed_tools"])
    ctx = ToolContext(
        specialist_id=spec_id,
        pubmed_bias=cfg.get("pubmed_bias"),
        ledger=ledger,
    )

    user_content = (context_prefix + "\n\n---\n\n" + case) if context_prefix else case

    # Tell each agent which trusted_sources to pass to patient_source_search.
    trusted = cfg.get("trusted_sources") or []
    if trusted:
        trusted_block = (
            "TRUSTED SOURCES FOR YOUR ROLE (pass this list as `sources` to "
            "patient_source_search; if Tier 1 alone is too narrow, you may "
            "also use web_search as a last resort):\n"
            + ", ".join(trusted)
        )
        user_content = user_content + "\n\n" + trusted_block

    messages = [
        {"role": "system", "content": cfg["system_prompt"]},
        {"role": "user", "content": user_content},
    ]

    emit("started", {"allowed_tools": sorted(cfg["allowed_tools"])})

    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        emit("thinking", {"iteration": iteration})
        resp = await _timed_chat(messages, emit, tools=tools, phase="tool_loop")
        msg = resp.choices[0].message

        assistant_dict: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [_tool_call_dict(tc) for tc in msg.tool_calls]
        messages.append(assistant_dict)

        if not msg.tool_calls:
            return msg.content or "", messages

        await _dispatch_tool_calls(msg.tool_calls, ctx, emit, messages, result_char_cap)

    emit("tool_loop_capped", {"iterations": MAX_TOOL_ITERATIONS})
    messages.append(
        {
            "role": "user",
            "content": (
                "You've reached the tool-call budget. Produce your final answer "
                "now using the evidence already retrieved. Use the [N] labels you've seen "
                "in tool results."
            ),
        }
    )
    resp = await _timed_chat(messages, emit, tools=None, phase="wrapup")
    final = resp.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": final})
    return final, messages


async def _continue_tool_loop(
    spec_id: str,
    messages: list[dict],
    ledger: EvidenceLedger,
    emit,
    *,
    result_char_cap: int = MAX_TOOL_RESULT_CHARS_IN_HISTORY,
) -> tuple[str, list[dict]]:
    cfg = SPECIALIST_CONFIGS[spec_id]
    tools = schemas_for(cfg["allowed_tools"])
    ctx = ToolContext(
        specialist_id=spec_id,
        pubmed_bias=cfg.get("pubmed_bias"),
        ledger=ledger,
    )
    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        emit("thinking", {"iteration": f"retry-{iteration}"})
        resp = await _timed_chat(messages, emit, tools=tools, phase="retry")
        msg = resp.choices[0].message
        assistant_dict: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [_tool_call_dict(tc) for tc in msg.tool_calls]
        messages.append(assistant_dict)
        if not msg.tool_calls:
            return msg.content or "", messages
        await _dispatch_tool_calls(msg.tool_calls, ctx, emit, messages, result_char_cap)
    emit("error", {"message": "Retry tool loop exhausted budget; specialist will abstain."})
    return "", messages


async def _self_check(draft: str, messages: list[dict], emit) -> str:
    emit("self_checking", {})
    messages = messages + [{"role": "user", "content": prompts.SELF_CHECK}]
    resp = await _timed_chat(messages, emit, tools=None, phase="self_check")
    return resp.choices[0].message.content or draft


RETRIEVE_OR_ABSTAIN_PROMPT = (
    "You attempted to finalize an answer but you have NOT registered any evidence "
    "in the team's evidence ledger. The rule is: an agent that does not retrieve "
    "information must not answer. Right now, you MUST either (a) call your "
    "retrieval tools (`patient_source_search` first, then `pubmed_search` if "
    "needed) until at least one piece of evidence is registered with a [N] label, "
    "or (b) abstain by responding with EXACTLY this and nothing else:\n\n"
    "ABSTAIN: insufficient evidence available for me to answer responsibly.\n\n"
    "Do not produce a recommendation without retrieved evidence."
)


def _parse_cited_labels(text: str, ledger: EvidenceLedger) -> list[str]:
    cited: set[int] = set()
    for group in re.findall(r"\[\d{1,3}(?:\s*[-–,;]\s*\d{1,3})*\]", text):
        nums = [int(n) for n in re.findall(r"\d+", group)]
        if len(nums) == 2 and re.search(r"[-–]", group):
            lo, hi = nums
            if hi >= lo and hi - lo < 50:
                cited.update(range(lo, hi + 1))
                continue
        cited.update(nums)
    return [str(n) for n in sorted(cited) if ledger.get_by_label(str(n)) is not None]


async def finalize_draft(
    spec_id: str,
    draft: str,
    messages: list[dict],
    ledger: EvidenceLedger,
    emit,
) -> SpecialistResult:
    cfg = SPECIALIST_CONFIGS[spec_id]
    soft_gate = cfg.get("soft_citation_gate", False)

    revised = await _self_check(draft, messages, emit)

    if ABSTAIN_MARKER.search(revised.strip().splitlines()[0] if revised.strip() else ""):
        emit("no_evidence", {"reason": revised.strip()})
        return SpecialistResult(
            specialist_id=spec_id,
            status="no_evidence",
            draft_markdown=revised.strip(),
            recommendation_summary="(abstained — no evidence retrieved)",
        )

    labels = _parse_cited_labels(revised, ledger)
    for label in labels:
        ledger.mark_cited(label, spec_id)

    # Hard gate (default): zero citations → force abstain.
    # Soft gate (Patient Navigator): allow a non-empty draft through with no
    # citations, since some practical guidance (e.g., "ask your hospital for
    # an oncology social worker") is true by general framing rather than by
    # a specific cited program.
    if not labels:
        if soft_gate and revised.strip():
            summary = _extract_summary(revised)
            emit("done", {"summary": summary, "evidence_labels": []})
            return SpecialistResult(
                specialist_id=spec_id,
                status="done",
                draft_markdown=revised,
                recommendation_summary=summary,
                evidence_labels=[],
            )
        emit("no_evidence", {"reason": "draft has no [N] citations after self-check"})
        return SpecialistResult(
            specialist_id=spec_id,
            status="no_evidence",
            draft_markdown=(
                "ABSTAIN: my draft did not include any citations to retrieved "
                "evidence. Per the team's rule against answering from training "
                "knowledge, I am abstaining."
            ),
            recommendation_summary="(abstained — draft was not citation-grounded)",
        )

    summary = _extract_summary(revised)
    emit("done", {"summary": summary, "evidence_labels": labels})
    return SpecialistResult(
        specialist_id=spec_id,
        status="done",
        draft_markdown=revised,
        recommendation_summary=summary,
        evidence_labels=labels,
    )


async def run_specialist(
    spec_id: str,
    case: str,
    context_prefix: str,
    ledger: EvidenceLedger,
    emit,
) -> SpecialistResult:
    cfg = SPECIALIST_CONFIGS[spec_id]
    soft_gate = cfg.get("soft_citation_gate", False)
    try:
        cap = cfg.get("result_char_cap", MAX_TOOL_RESULT_CHARS_IN_HISTORY)
        draft, messages = await _run_tool_loop(
            spec_id, case, context_prefix, ledger, emit, result_char_cap=cap
        )

        if SKIP_MARKER.search(draft.strip().splitlines()[0] if draft.strip() else ""):
            emit("skipped", {"reason": draft.strip()})
            return SpecialistResult(
                specialist_id=spec_id,
                status="skipped",
                draft_markdown=draft.strip(),
                recommendation_summary="(skipped — not applicable to this case)",
            )

        # Retrieve-or-abstain gate: skipped for soft-gate agents.
        if ledger.count_for(spec_id) == 0 and not soft_gate:
            emit("retrieve_or_abstain", {"reason": "no evidence registered in first pass"})
            messages.append({"role": "user", "content": RETRIEVE_OR_ABSTAIN_PROMPT})
            draft, messages = await _continue_tool_loop(
                spec_id, messages, ledger, emit, result_char_cap=cap
            )

            if ABSTAIN_MARKER.search(draft.strip().splitlines()[0] if draft.strip() else ""):
                emit("no_evidence", {"reason": draft.strip()})
                return SpecialistResult(
                    specialist_id=spec_id,
                    status="no_evidence",
                    draft_markdown=draft.strip(),
                    recommendation_summary="(abstained — no evidence retrieved)",
                )

            if ledger.count_for(spec_id) == 0:
                emit("no_evidence", {"reason": "no evidence after retry"})
                return SpecialistResult(
                    specialist_id=spec_id,
                    status="no_evidence",
                    draft_markdown=(
                        "ABSTAIN: I was unable to retrieve evidence to support an "
                        "answer for this question. Per the team's rule, I am "
                        "abstaining rather than answering from training data alone."
                    ),
                    recommendation_summary="(abstained — no evidence retrieved)",
                )

        return await finalize_draft(spec_id, draft, messages, ledger, emit)
    except llm.QuotaExceeded as e:
        log.warning("Specialist %s hit LLM quota: %s", spec_id, e)
        emit("error", {"message": "LLM quota exceeded — see Settings → Billing."})
        return SpecialistResult(
            specialist_id=spec_id,
            status="error",
            error="LLM quota exceeded. Verify billing is enabled on your provider account.",
        )
    except Exception as e:
        log.exception("Specialist %s failed", spec_id)
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:197] + "…"
        emit("error", {"message": f"{type(e).__name__}: {msg}"})
        return SpecialistResult(specialist_id=spec_id, status="error", error=msg)
