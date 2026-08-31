"""Standalone entry point: runs eval/eval_questions.yaml against the real agent.

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

Usage (from repo root, with the venv activated):

    python scripts\\run_eval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from agent.graph import run_agent  # noqa: E402
from agent.state import AgentState  # noqa: E402
from config.settings import configure_logging  # noqa: E402

_EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "eval_questions.yaml"


def _looks_like_only_raw_keys(row: tuple) -> bool:
    """True if every value in `row` is a bare int -- i.e. no readable column at all."""
    return bool(row) and all(isinstance(value, int) for value in row)


def _evaluate_case(case: dict) -> tuple[bool, AgentState]:
    """Runs one eval question and checks it against its expectations.

    Returns:
        (passed, final_state) -- the state is returned too so the caller can
        print the SQL/status on failure without re-running the question.
    """
    state = run_agent(case["question"])
    row_count = state.get("row_count") or 0
    rows = state.get("result_rows") or []
    sql = state.get("sql") or ""

    passed = state.get("status") == "succeeded" and row_count >= case.get("min_rows", 1)
    if passed and case.get("expect_readable_result") and rows:
        passed = not _looks_like_only_raw_keys(rows[0])
    if passed and case.get("expect_tables_used"):
        # Checks the *executed SQL text*, not just what was retrieved into
        # context -- a table can be retrieved and still not end up in the
        # actual join, which is the exact gap this check exists to catch.
        passed = all(table in sql for table in case["expect_tables_used"])
    return passed, state


def main() -> None:
    configure_logging()
    cases = yaml.safe_load(_EVAL_FILE.read_text(encoding="utf-8"))["questions"]

    passed_count = 0
    for case in cases:
        passed, state = _evaluate_case(case)
        passed_count += int(passed)
        outcome = "PASS" if passed else "FAIL"
        print(
            f"[{outcome}] {case['question']!r} -- "
            f"status={state.get('status')} rows={state.get('row_count')}"
        )
        if not passed:
            print(f"         sql={state.get('sql')!r}")

    print(f"\n{passed_count}/{len(cases)} passed.")
    sys.exit(0 if passed_count == len(cases) else 1)


if __name__ == "__main__":
    main()
