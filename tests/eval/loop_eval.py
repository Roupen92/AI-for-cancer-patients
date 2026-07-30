"""Synthesis-quality eval loop for the patient-facing cancer board.

Tests whether the final patient answer (a) DEFINES institution abbreviations on
first mention (MSK -> "Memorial Sloan Kettering Cancer Center, a cancer hospital
in New York"), and (b) SYNTHESIZES recommendations into unified guidance instead
of listing "MSK says X, the UK says Y" as disconnected institutional positions.

Subcommands
-----------
  capture   Run the REAL board end-to-end on the haplo-HSCT case. Saves a fixture
            with the final answer + the EXACT user_content sent to the synthesizer
            + specialist drafts + references. (slow; hits the live LLM + search)

  synth     Re-run ONLY the synthesizer against a saved fixture's frozen
            user_content, using the CURRENT prompts.py. Fast + deterministic input
            -> isolates synthesizer-prompt edits. Writes the new answer.

  judge     Score a markdown answer against the two criteria with an LLM judge.
            Prints PASS/FAIL + quoted violations as JSON.

  iterate   synth + judge in one shot against a fixture (the inner loop step).

Run from repo root:  .venv/bin/python -m tests.eval.loop_eval <subcommand> [args]
"""
import argparse
import importlib
import json
import os
import pathlib
import sys

from dotenv import load_dotenv

load_dotenv()  # MUST precede app imports — config.py resolves provider/model at import

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
RUNS = HERE / "runs"
FIXTURES.mkdir(exist_ok=True)
RUNS.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #

JUDGE_SYSTEM = """You are a strict QA reviewer for a patient-facing cancer-support app. A real cancer patient (not a clinician) will read the answer below. You are checking TWO specific things the product owner cares about. Be skeptical and quote exact offending text.

CRITERION A — INSTITUTIONS ARE NAMED IN PLAIN ENGLISH FOR A PATIENT
Every time the answer refers to an institution, guideline body, hospital, charity, or professional organization, the patient must be able to understand WHO that is. On FIRST mention, the answer must give the full name AND a brief plain-English description of what it is.
- "MSK" or "MSKCC" alone = FAIL. Must be e.g. "Memorial Sloan Kettering Cancer Center (a major cancer hospital in New York)".
- "the UK recommends" / "UK guidelines say" = FAIL when no specific body is named. Naming a real body is required, e.g. "Macmillan Cancer Support, a UK cancer charity" or "the UK's National Institute for Health and Care Excellence (NICE)".
- Bare acronyms with no expansion + no description: "NCCN", "ACS", "ESMO", "ESPEN", "ASCO", "NCI", "APTA", "Fred Hutch", "Be The Match" = FAIL on first use.
- A registered-dietitian/physiotherapist/social-worker job title is NOT an institution and does NOT need defining. Drug names, trial IDs, and program names like "FMLA" are out of scope for THIS criterion.
- Citation labels like [1], [3] are fine and expected; they are not a substitute for naming the institution in prose.
PASS only if EVERY institution mentioned in prose is given full name + plain-English description on first mention.

CRITERION B — RECOMMENDATIONS ARE SYNTHESIZED, NOT LISTED BY INSTITUTION
The answer must read as unified, actionable guidance for THIS patient. It must NOT read like a survey of who-said-what.
- FAIL pattern: "MSK recommends X. The UK recommends Y. NCCN says Z." — parallel institutional attributions presented as separate disconnected positions, leaving the patient to reconcile them.
- When sources AGREE, give ONE unified recommendation (a trailing [N] citation, or "supported by major cancer centers [2][4]", is fine — attribution is welcome, fragmentation is not).
- When sources GENUINELY DIFFER, the answer must EXPLAIN the difference in plain English and tell the patient what to do about it (e.g., "Older advice said strict 'neutropenic diet'; newer guidance from [2] found safe food-handling works as well — ask your transplant team which they follow"), NOT just list both and move on.
- Attribution by itself is NOT a violation. The product owner is fine with "data from [a named, described institution] shows X." The violation is FRAGMENTATION: listing institutions instead of synthesizing them.
PASS only if the dominant structure is synthesized guidance, with at most rare, well-explained source-by-source contrasts.

GUARD CRITERION C — SPECIFICS PRESERVED (regression guard, do not let the fix flatten the answer)
Actionable specifics from the sources must survive (numbers, durations, named foods/exercises, concrete steps). If the answer collapsed into vague "do aerobic exercise / eat well" filler, note it. This is a guard, not the main target.

Return ONLY a JSON object with this exact shape:
{
  "criterion_a_institutions": {"pass": true|false, "score": 1-5, "violations": ["exact quoted phrase that fails", ...], "notes": "1-2 sentences"},
  "criterion_b_synthesis":    {"pass": true|false, "score": 1-5, "violations": ["exact quoted phrase that fails", ...], "notes": "1-2 sentences"},
  "criterion_c_specifics":    {"pass": true|false, "score": 1-5, "violations": [...], "notes": "1-2 sentences"},
  "overall_pass": true|false,   // true ONLY if criterion_a AND criterion_b both pass (c is a guard: if c fails hard, overall_pass=false)
  "summary": "2-3 sentences: the single most important thing to fix next, or why it passes."
}
Score 5 = flawless, 1 = egregious. Quote real substrings from the answer in violations. Output ONLY the JSON, no prose, no code fences."""


def judge_markdown(answer_md: str) -> dict:
    from app import llm

    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": "PATIENT ANSWER TO REVIEW:\n\n" + answer_md},
    ]
    return llm.chat_json(messages)


# --------------------------------------------------------------------------- #
# Capture — run the real board, save a fixture
# --------------------------------------------------------------------------- #

def cmd_capture(args):
    import asyncio
    from app import board, llm, prompts
    from tests.eval.case import CASE, LOCATION, PREFERENCES, TARGET_LANGUAGE

    captured = {"synth_user_content": None}
    real_chat = llm.chat

    def chat_spy(messages, **kwargs):
        # The synthesizer call is the one whose system prompt IS prompts.SYNTHESIZER.
        try:
            if messages and messages[0].get("content") == prompts.SYNTHESIZER:
                captured["synth_user_content"] = messages[1]["content"]
        except Exception:
            pass
        return real_chat(messages, **kwargs)

    llm.chat = chat_spy

    drafts = {}
    final_holder = {}

    def emit(t, p):
        if t == "specialist_round_complete":
            drafts[p["specialist"]] = {
                "status": p["status"],
                "draft_markdown": p.get("draft_markdown", ""),
                "recommendation_summary": p.get("recommendation_summary", ""),
                "evidence_labels": p.get("evidence_labels", []),
            }
        elif t == "final":
            final_holder.update(p)
        # progress breadcrumb
        if t in ("board_started", "phase", "synthesis_complete", "final"):
            print(f"  [{t}] {p.get('phase', '') or p.get('target_language', '') or ''}", file=sys.stderr)

    print("Running REAL board (this hits the live LLM + search; ~1-4 min)…", file=sys.stderr)
    result = asyncio.run(
        board.run_board(CASE, LOCATION, TARGET_LANGUAGE, emit, preferences=PREFERENCES)
    )
    llm.chat = real_chat

    fixture = {
        "case": CASE,
        "location": LOCATION,
        "preferences": PREFERENCES,
        "target_language": TARGET_LANGUAGE,
        "synth_user_content": captured["synth_user_content"],
        "specialist_drafts": drafts,
        "english_markdown": result["english_markdown"],
        "references": result.get("references", []),
    }
    out = FIXTURES / (args.name + ".json")
    out.write_text(json.dumps(fixture, indent=2))
    print(f"\nFixture saved: {out}", file=sys.stderr)
    if not captured["synth_user_content"]:
        print("WARNING: did not capture synthesizer user_content!", file=sys.stderr)
    # Also drop the baseline answer where judge/synth can read it.
    (RUNS / (args.name + "_baseline.md")).write_text(result["english_markdown"])
    print(f"Baseline answer: {RUNS / (args.name + '_baseline.md')}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Synth — re-run ONLY the synthesizer against a frozen fixture
# --------------------------------------------------------------------------- #

def _resynthesize(synth_user_content: str) -> str:
    import app.prompts as prompts
    importlib.reload(prompts)  # pick up live edits to prompts.py
    from app import llm

    messages = [
        {"role": "system", "content": prompts.SYNTHESIZER},
        {"role": "user", "content": synth_user_content},
    ]
    resp = llm.chat(messages, tools=None)
    return resp.choices[0].message.content or "(empty)"


def cmd_synth(args):
    fixture = json.loads((FIXTURES / (args.name + ".json")).read_text())
    if not fixture.get("synth_user_content"):
        sys.exit("Fixture has no synth_user_content — re-run `capture`.")
    md = _resynthesize(fixture["synth_user_content"])
    out = RUNS / (args.name + f"_synth_{args.tag}.md")
    out.write_text(md)
    print(f"Wrote {out}")


# --------------------------------------------------------------------------- #
# Judge — score a markdown file
# --------------------------------------------------------------------------- #

def cmd_judge(args):
    md = pathlib.Path(args.path).read_text()
    verdict = judge_markdown(md)
    print(json.dumps(verdict, indent=2))


def cmd_gloss(args):
    """Run ONLY the institution-glossary safety pass on an existing answer, then
    judge the result. Deterministic test that the safety net repairs bare orgs."""
    import importlib
    import app.prompts as prompts
    importlib.reload(prompts)
    import app.board as board
    importlib.reload(board)

    src = pathlib.Path(args.path)
    md_in = src.read_text()
    md_out = board._gloss_institutions(md_in)
    out = src.with_name(src.stem + "_glossed.md")
    out.write_text(md_out)
    delta = len(md_out) - len(md_in)
    print(f"Glossed: {src.name} -> {out.name}  ({len(md_in)} -> {len(md_out)} chars, {delta:+d})")
    if args.judge:
        print(json.dumps(judge_markdown(md_out), indent=2))


# --------------------------------------------------------------------------- #
# Iterate — resynthesize from fixture, then judge, in one step
# --------------------------------------------------------------------------- #

def cmd_iterate(args):
    fixture = json.loads((FIXTURES / (args.name + ".json")).read_text())
    if not fixture.get("synth_user_content"):
        sys.exit("Fixture has no synth_user_content — re-run `capture`.")
    md = _resynthesize(fixture["synth_user_content"])
    out = RUNS / (args.name + f"_synth_{args.tag}.md")
    out.write_text(md)
    verdict = judge_markdown(md)
    (RUNS / (args.name + f"_verdict_{args.tag}.json")).write_text(json.dumps(verdict, indent=2))
    print(f"=== ANSWER: {out} ({len(md)} chars) ===")
    print(json.dumps(verdict, indent=2))


def cmd_batch(args):
    """Run a set of Reddit-derived cases through the REAL board, judge each final
    answer, and write per-case answers + a summary report. Sequential (each board
    already parallelizes its 5 specialists internally)."""
    import asyncio
    from app import board
    from tests.eval.reddit_cases import CASES

    wanted = set(p.strip() for p in args.ids.split(",")) if args.ids else None
    cases = [c for c in CASES if (wanted is None or c["id"] in wanted or c["id"].split("_")[0] in wanted)]
    if not cases:
        sys.exit(f"No matching cases. Available ids: {[c['id'] for c in CASES]}")

    results = []
    for i, c in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] running case {c['id']} — {c['theme']}", file=sys.stderr)

        def emit(t, p):
            if t in ("synthesis_complete", "final"):
                print(f"    [{t}]", file=sys.stderr)

        try:
            res = asyncio.run(board.run_board(
                c["case"], c["location"], c["target_language"], emit,
                preferences=c["preferences"],
            ))
            md = res["english_markdown"]
            (RUNS / f"reddit_{c['id']}.md").write_text(md)
            verdict = judge_markdown(md)
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            results.append({"id": c["id"], "theme": c["theme"], "error": str(e)})
            continue

        row = {
            "id": c["id"], "theme": c["theme"], "location": c["location"],
            "chars": len(md),
            "A_institutions": [verdict["criterion_a_institutions"]["pass"], verdict["criterion_a_institutions"]["score"]],
            "B_synthesis": [verdict["criterion_b_synthesis"]["pass"], verdict["criterion_b_synthesis"]["score"]],
            "C_specifics": [verdict["criterion_c_specifics"]["pass"], verdict["criterion_c_specifics"]["score"]],
            "overall_pass": verdict["overall_pass"],
            "A_violations": verdict["criterion_a_institutions"]["violations"],
            "B_violations": verdict["criterion_b_synthesis"]["violations"],
            "summary": verdict["summary"],
        }
        results.append(row)
        v = "PASS" if row["overall_pass"] else "FAIL"
        print(f"    -> {v}  A={row['A_institutions']} B={row['B_synthesis']} C={row['C_specifics']}", file=sys.stderr)

    report = RUNS / "reddit_batch_report.json"
    report.write_text(json.dumps(results, indent=2))
    # Compact table to stdout
    print("\n=== BATCH REPORT ===")
    passed = sum(1 for r in results if r.get("overall_pass"))
    print(f"{passed}/{len(results)} overall PASS\n")
    for r in results:
        if "error" in r:
            print(f"  {r['id']:24} ERROR: {r['error'][:60]}")
            continue
        v = "PASS" if r["overall_pass"] else "FAIL"
        print(f"  {r['id']:24} {v}  A={r['A_institutions'][1]} B={r['B_synthesis'][1]} C={r['C_specifics'][1]}"
              + (f"  | A-viol: {r['A_violations'][:2]}" if r["A_violations"] else "")
              + (f"  | B-viol: {r['B_violations'][:1]}" if r["B_violations"] else ""))
    print(f"\nfull report: {report}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("capture"); p.add_argument("--name", default="haplo"); p.set_defaults(func=cmd_capture)
    p = sub.add_parser("synth"); p.add_argument("--name", default="haplo"); p.add_argument("--tag", default="v"); p.set_defaults(func=cmd_synth)
    p = sub.add_parser("judge"); p.add_argument("path"); p.set_defaults(func=cmd_judge)
    p = sub.add_parser("gloss"); p.add_argument("path"); p.add_argument("--judge", action="store_true"); p.set_defaults(func=cmd_gloss)
    p = sub.add_parser("iterate"); p.add_argument("--name", default="haplo"); p.add_argument("--tag", default="v"); p.set_defaults(func=cmd_iterate)
    p = sub.add_parser("batch"); p.add_argument("--ids", default=""); p.set_defaults(func=cmd_batch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
