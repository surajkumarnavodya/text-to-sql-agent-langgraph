"""Standalone entry point: runs eval/eval_questions.yaml against the real agent.

**Superseded by `scripts/run_benchmark.py`** (a rigorous benchmark
architecture -- execution-based result-set grading instead of row-count/
readability heuristics, retrieval/join/aggregation/date/GROUP BY/ORDER BY/
NULL/nested-query/window-function correctness, latency/retry/token/cost
metrics, and regression detection against a stored baseline -- see
`eval/schema.py`, `eval/runner.py`, `eval/metrics.py`). Every case this
file's `eval/eval_questions.yaml` had was migrated into
`eval/benchmark/*.yaml` (see `adversarial.yaml`, `follow_up.yaml`,
`cost_estimation.yaml`, and `real_world.yaml`'s `ambig_bikes_top_territory`
for the ProductLine regression case specifically) -- nothing here is losing
coverage by going unused. Kept, unmodified, for anyone with a workflow
still pointed at it directly; not recommended for new work.

Requires a live DB connection + Ollama, like scripts/integration_test.py --
not part of the pytest suite, never run by CI. Each question gets a
pass/fail from machine-checkable expectations (minimum row count, whether
the result looks like a readable value rather than a raw surrogate key, and
optionally which tables the join *must* have gone through), so join/filter/
output regressions show up here instead of shipping silently.

The `expect_tables_used` check exists because row count and readability
alone aren't sufficient: a query can skip a required join hop (e.g.
DimProduct -> DimProductSubcategory -> DimProductCategory collapsed into a
direct DimProduct-to-DimProductCategory comparison) and still return a
plausible-looking non-empty, readable result purely by key-range
coincidence. Asserting a specific table name actually appears in the
executed SQL text catches that class of false-positive pass.

`two_turn_sequences` (alongside `questions`) covers the follow-up feature:
each entry is a real two-turn conversation, run turn by turn through the
same `build_conversation_history` / `run_agent` path the UI uses (see
`ui/session_history.py`) rather than a single isolated question -- so a
regression in follow-up classification or reference resolution shows up
here the same way a join/filter regression does for a standalone question.

`expect_grounded_insight` (a field on any question/turn) covers the insight
feature: it asserts a plain-English insight was actually produced *and*
passes `agent.insight.is_insight_grounded` -- the same basic check that
extracts every number in the insight text and confirms it's supported by
the result summary (or a literal value from the question/SQL). Only set
this on cases expected to produce a non-skipped insight (multi-row, not a
single redundant value) -- see eval/eval_questions.yaml's field docs.

`adversarial_cases` is a permanent regression category for the input-
hardening work (prompt injection, off-topic, malformed input): each entry
asserts the agent lands on a specific `status`/`rejection_reason`, run
through the real `sanitize_input_node` gate (and, for anything that were
to slip past it, the model's own system-prompt-driven refusal) rather than
a mocked shortcut. Kept separate from `questions`'s accuracy checks so a
future prompt or model swap gets checked against this set automatically,
not just "does it produce correct SQL."

`cost_estimation_cases` is the regression category for the proactive-cost-
estimation work (db/query_cost.py): deliberately broad, unfiltered
questions, each asserting a specific `status` (a "moderate" case still
succeeds, just with a cost_notice; a "high" case never executes at all, so
it exhausts its retries and ends "failed") and `cost_estimate.severity`.
Both real cases here were discovered by actually running the questions
against the live model, not authored from a guess -- see each entry's
notes for exactly what SQL the model produced.

Usage (from repo root, with the venv activated):

    python scripts\\run_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from agent.graph import run_agent  # noqa: E402
from agent.insight import is_insight_grounded  # noqa: E402
from agent.state import AgentState  # noqa: E402
from config.settings import configure_logging  # noqa: E402
from ui.session_history import (  # noqa: E402
    QueryHistoryEntry,
    build_conversation_history,
    new_history_entry,
)

_EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "eval_questions.yaml"


def _looks_like_only_raw_keys(row: tuple) -> bool:
    """True if every value in `row` is a bare int -- i.e. no readable column at all."""
    return bool(row) and all(isinstance(value, int) for value in row)


def _check_expectations(state: AgentState, expectations: dict) -> bool:
    """Checks one turn's final state against its machine-checkable expectations.

    Shared by both standalone questions and each turn of a two-turn
    sequence, so a follow-up turn is held to exactly the same bar as a
    standalone one (row count, readability, required join tables) -- a
    follow-up is not exempt from any existing accuracy check.
    """
    row_count = state.get("row_count") or 0
    rows = state.get("result_rows") or []
    sql = state.get("sql") or ""

    passed = state.get("status") == "succeeded" and row_count >= expectations.get("min_rows", 1)
    if passed and expectations.get("max_rows") is not None:
        passed = row_count <= expectations["max_rows"]
    if passed and expectations.get("expect_readable_result") and rows:
        passed = not _looks_like_only_raw_keys(rows[0])
    if passed and expectations.get("expect_tables_used"):
        # Checks the *executed SQL text*, not just what was retrieved into
        # context -- a table can be retrieved and still not end up in the
        # actual join, which is the exact gap this check exists to catch.
        passed = all(table in sql for table in expectations["expect_tables_used"])
    if passed and expectations.get("expect_followup"):
        # Guards the classifier itself: a two-turn eval entry exists to
        # prove the second turn was *resolved as a follow-up*, not just
        # that it happened to produce a plausible query on its own.
        passed = state.get("followup_classification") == "followup"
    if passed and expectations.get("expect_grounded_insight"):
        # Guards the insight feature: an insight must have actually been
        # produced (not skipped/dropped) and every number in it must be
        # traceable to the result summary or a literal value already in the
        # question/SQL -- the same basic grounding check the runtime itself
        # uses to decide whether to show an insight at all.
        insight = state.get("insight")
        summary = state.get("insight_summary")
        passed = (
            insight is not None
            and summary is not None
            and is_insight_grounded(
                insight, summary, question=state.get("question", ""), sql=state.get("sql") or ""
            )
        )
    return passed


def _evaluate_case(case: dict) -> tuple[bool, AgentState]:
    """Runs one standalone eval question and checks it against its expectations.

    Returns:
        (passed, final_state) -- the state is returned too so the caller can
        print the SQL/status on failure without re-running the question.
    """
    state = run_agent(case["question"])
    return _check_expectations(state, case), state


def _evaluate_two_turn_case(case: dict) -> list[tuple[bool, AgentState]]:
    """Runs a two-turn conversation, turn by turn, through the real follow-up path.

    Turn 2 (and beyond) is run with `conversation_history` built from turn
    1's actual result via `ui.session_history.build_conversation_history` --
    the same function the UI uses -- so this exercises the real
    classify-then-resolve path, not a hand-constructed shortcut.

    Returns:
        One (passed, final_state) pair per turn, in order.
    """
    history: list[QueryHistoryEntry] = []
    results: list[tuple[bool, AgentState]] = []
    for turn in case["turns"]:
        conversation_history = build_conversation_history(history)
        state = run_agent(turn["question"], conversation_history)
        results.append((_check_expectations(state, turn), state))
        history.append(new_history_entry(turn["question"], state))
    return results


def _evaluate_adversarial_case(case: dict) -> tuple[bool, AgentState]:
    """Runs one adversarial-input case and checks it landed on the expected outcome.

    Unlike `_evaluate_case`, this doesn't check row count/readability --
    the whole point is that these questions should never reach execution
    (most are caught by `sanitize_input_node` before any LLM/DB call at
    all). "Passed" here means "the agent correctly declined," not "the
    agent answered well."
    """
    state = run_agent(case["question"])
    passed = state.get("status") == case["expect_status"]
    if passed and case.get("expect_rejection_reason"):
        passed = state.get("rejection_reason") == case["expect_rejection_reason"]
    return passed, state


def _evaluate_cost_estimation_case(case: dict) -> tuple[bool, AgentState]:
    """Runs one deliberately-broad-question case and checks the cost-estimation outcome.

    Like `_evaluate_adversarial_case`, "passed" isn't about row count/
    readability -- it's about whether `estimate_query_cost_node` correctly
    classified the query's severity and the graph reacted the way that
    severity should (moderate: succeed anyway, with a notice; high: never
    execute at all, retry, eventually fail cleanly).
    """
    state = run_agent(case["question"])
    passed = state.get("status") == case["expect_status"]
    if passed and case.get("expect_cost_severity"):
        estimate = state.get("cost_estimate")
        passed = estimate is not None and estimate.severity == case["expect_cost_severity"]
    return passed, state


def main() -> None:
    configure_logging()
    eval_data = yaml.safe_load(_EVAL_FILE.read_text(encoding="utf-8"))
    cases = eval_data.get("questions", [])
    sequences = eval_data.get("two_turn_sequences", [])
    adversarial_cases = eval_data.get("adversarial_cases", [])
    cost_estimation_cases = eval_data.get("cost_estimation_cases", [])

    passed_count = 0
    total_count = 0

    for case in cases:
        passed, state = _evaluate_case(case)
        passed_count += int(passed)
        total_count += 1
        outcome = "PASS" if passed else "FAIL"
        print(
            f"[{outcome}] {case['question']!r} -- "
            f"status={state.get('status')} rows={state.get('row_count')}"
        )
        if not passed:
            print(f"         sql={state.get('sql')!r}")

    for sequence in sequences:
        turn_results = _evaluate_two_turn_case(sequence)
        for turn_number, (turn, (passed, state)) in enumerate(
            zip(sequence["turns"], turn_results, strict=True), start=1
        ):
            passed_count += int(passed)
            total_count += 1
            outcome = "PASS" if passed else "FAIL"
            print(
                f"[{outcome}] turn {turn_number}: {turn['question']!r} -- "
                f"status={state.get('status')} rows={state.get('row_count')} "
                f"followup_classification={state.get('followup_classification')}"
            )
            if not passed:
                print(f"         sql={state.get('sql')!r}")

    for case in adversarial_cases:
        passed, state = _evaluate_adversarial_case(case)
        passed_count += int(passed)
        total_count += 1
        outcome = "PASS" if passed else "FAIL"
        print(
            f"[{outcome}] [adversarial] {case['question']!r} -- "
            f"status={state.get('status')} rejection_reason={state.get('rejection_reason')}"
        )
        if not passed:
            print(
                f"         expected status={case['expect_status']!r} "
                f"reason={case.get('expect_rejection_reason')!r}"
            )

    for case in cost_estimation_cases:
        passed, state = _evaluate_cost_estimation_case(case)
        passed_count += int(passed)
        total_count += 1
        outcome = "PASS" if passed else "FAIL"
        estimate = state.get("cost_estimate")
        print(
            f"[{outcome}] [cost] {case['question']!r} -- status={state.get('status')} "
            f"severity={estimate.severity if estimate else None} "
            f"rows={estimate.estimated_rows if estimate else None}"
        )
        if not passed:
            print(f"         sql={state.get('sql')!r}")

    print(f"\n{passed_count}/{total_count} passed.")
    sys.exit(0 if passed_count == total_count else 1)


if __name__ == "__main__":
    main()
