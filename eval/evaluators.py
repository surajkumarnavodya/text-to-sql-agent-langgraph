"""Evaluators: turn one `CaseRunResult`'s raw signal into verdicts.

The central design decision, per the task's explicit instruction "do not
optimize for SQL string similarity alone": **result-set accuracy is the
primary correctness signal**, computed by executing `expected_sql` (or
`alternative_sql`) against the live database to get a *gold result set*,
then comparing the agent's actual result set against it -- the same
execution-accuracy principle Spider/BIRD-style Text-to-SQL benchmarks use.
`sql_exact_match` is computed too (structurally normalized via sqlglot, not
a raw string diff) but is explicitly diagnostic-only -- see
`compute_overall_pass`, which never uses it to decide pass/fail.

Every function here is pure given its inputs (no `run_agent` calls) --
`eval/runner.py` owns talking to the live agent/database; this module only
grades what came back.
"""

from __future__ import annotations

import logging
from collections import Counter
from decimal import Decimal

import sqlglot
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp
from sqlglot.errors import ParseError

from eval.schema import BenchmarkCase, CaseRunResult, ExpectedResultSpec, FollowUpTurn

logger = logging.getLogger(__name__)

_NUMERIC_TYPES = (int, float, Decimal)
_DATE_NAME_HINTS = ("date", "year", "month", "quarter", "day", "week")


# ---------------------------------------------------------------------------
# Result-set comparison (the primary correctness signal)
# ---------------------------------------------------------------------------


def _canonical_value(value: object) -> tuple:
    """Reduces one cell to a type-tagged, tolerance-rounded, order-stable key.

    Numeric values are rounded (2 decimals) so trivial floating-point
    differences between the agent's SQL and gold SQL (different join order,
    different intermediate rounding) don't cause a false mismatch. Strings
    are lowercased/stripped for the same reason (whitespace/casing
    differences that don't change the actual answer). NULL gets its own
    tag so it never collides with a real value.
    """
    if value is None:
        return ("null",)
    if isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool):
        return ("num", round(float(value), 2))
    return ("str", str(value).strip().lower())


def _canonicalize_row(row: tuple) -> tuple:
    """Canonicalizes one row, sorted *within* the row.

    Sorting within (not across) rows makes the comparison tolerant of a
    different SELECT-list column order between the agent's SQL and gold SQL
    (e.g. `SELECT name, total` vs `SELECT total, name`) while still keeping
    each row's own value set intact -- this is what lets result-set
    comparison judge *correctness*, not *column ordering*.
    """
    return tuple(sorted(_canonical_value(v) for v in row))


def compare_result_sets(
    actual_rows: list[tuple],
    gold_rows: list[tuple],
    order_matters: bool = False,
) -> bool:
    """True if `actual_rows` and `gold_rows` represent the same answer.

    Column *names* are never compared here (aliases legitimately differ
    between two syntactically different but equally correct queries) --
    only the row values. Row order is ignored by default (most questions
    don't imply one); pass `order_matters=True` for a case specifically
    testing ORDER BY, which additionally requires the row *sequence* (after
    within-row canonicalization) to match, not just the multiset.
    """
    actual_canonical = [_canonicalize_row(r) for r in actual_rows]
    gold_canonical = [_canonicalize_row(r) for r in gold_rows]

    if order_matters:
        return actual_canonical == gold_canonical
    return Counter(actual_canonical) == Counter(gold_canonical)


def fetch_gold_result(
    sql: str, engine: Engine, row_cap: int = 5000
) -> tuple[list[str], list[tuple]] | None:
    """Executes `sql` (gold SQL from the dataset, not agent-generated) and
    returns its (columns, rows), or None if it fails to execute.

    A row cap (independent of, and much larger than, the app's own
    `MAX_RESULT_ROWS`) guards against a gold query that's accidentally
    unbounded -- this is dataset-authoring SQL, trusted more than
    LLM-generated SQL, but "trusted" isn't "never wrong."

    Returns:
        None if the gold SQL itself fails (a dataset-authoring bug, not an
        agent failure) -- logged as an error so it's visible, not silently
        swallowed into a false "agent got it wrong."
    """
    try:
        with engine.connect() as connection:
            cursor_result = connection.execute(text(sql))
            columns = list(cursor_result.keys())
            rows = cursor_result.fetchmany(row_cap)
        return columns, rows
    except SQLAlchemyError as exc:
        logger.error("[evaluators] gold SQL failed to execute (dataset bug?): %r -- %s", sql, exc)
        return None


def evaluate_result_set(
    run: CaseRunResult,
    case: BenchmarkCase | FollowUpTurn,
    engine: Engine,
) -> None:
    """Fills `run.result_set_correct`/`run.gold_*` by comparing against gold.

    Tries `expected_sql` first, then each of `alternative_sql` in order,
    then `expected_result` (a hand-authored fallback) -- passes if the
    actual result matches *any* acceptable gold, which is what makes a
    legitimately-ambiguous question (more than one right answer) gradeable
    without picking a single "correct" interpretation up front.
    """
    if run.result_columns is None or run.row_count is None:
        run.result_set_correct = None
        return

    gold_sql_candidates = [s for s in (case.expected_sql, *case.alternative_sql) if s]
    for index, gold_sql in enumerate(gold_sql_candidates):
        gold = fetch_gold_result(gold_sql, engine)
        if gold is None:
            continue
        gold_columns, gold_rows = gold
        if compare_result_sets(run.result_rows or [], gold_rows, order_matters=case.order_matters):
            run.result_set_correct = True
            run.gold_columns = gold_columns
            run.gold_rows = gold_rows
            run.gold_source = "expected_sql" if index == 0 else f"alternative_sql[{index - 1}]"
            return
        # Keep the first gold result around for reporting even if it
        # doesn't match, so a failure's diff is inspectable.
        if run.gold_rows is None:
            run.gold_columns, run.gold_rows = gold_columns, gold_rows
            run.gold_source = "expected_sql" if index == 0 else f"alternative_sql[{index - 1}]"

    if gold_sql_candidates:
        run.result_set_correct = False
        return

    # No gold SQL at all -- fall back to the hand-authored spec, if any.
    spec: ExpectedResultSpec | None = case.expected_result
    if spec is None:
        run.result_set_correct = None
        return
    ok = True
    if spec.row_count is not None:
        ok = ok and run.row_count == spec.row_count
    if spec.sample_rows is not None:
        ok = ok and compare_result_sets(
            run.result_rows or [], list(spec.sample_rows), order_matters=case.order_matters
        )
    run.result_set_correct = ok
    run.gold_source = "expected_result"


# ---------------------------------------------------------------------------
# Exact SQL match -- diagnostic only, never gates pass/fail
# ---------------------------------------------------------------------------


def _normalize_sql_for_match(sql: str, dialect: str | None) -> str | None:
    try:
        return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect, normalize=True)
    except ParseError:
        return None


def evaluate_sql_exact_match(
    generated_sql: str | None,
    case: BenchmarkCase | FollowUpTurn,
    dialect: str | None,
) -> bool | None:
    """Structurally-normalized SQL comparison -- **diagnostic only**.

    Reported in the aggregate metrics (`exact_sql_match`) purely as a
    secondary signal of how often the model's SQL is *textually* close to
    the reference, e.g. for tracking prompt-wording drift over time. Never
    used to compute `overall_pass` -- see this module's docstring and
    `compute_overall_pass` below, which is what actually implements "don't
    optimize for SQL string similarity alone."
    """
    if not generated_sql:
        return None
    candidates = [s for s in (case.expected_sql, *case.alternative_sql) if s]
    if not candidates:
        return None
    normalized_actual = _normalize_sql_for_match(generated_sql, dialect)
    if normalized_actual is None:
        return False
    for candidate in candidates:
        normalized_gold = _normalize_sql_for_match(candidate, dialect)
        if normalized_gold is not None and normalized_actual == normalized_gold:
            return True
    return False


# ---------------------------------------------------------------------------
# Retrieval + column-selection recall
# ---------------------------------------------------------------------------


def evaluate_retrieval(
    retrieved_tables: list[str], expected_tables: tuple[str, ...]
) -> float | None:
    """Fraction of `expected_tables` actually present in `retrieved_tables`.

    Case-insensitive (schema retrieval and the real catalog can differ in
    casing depending on the engine). Returns None (not applicable) when a
    case doesn't specify expected tables at all -- distinct from 0.0
    ("specified and completely missed").
    """
    if not expected_tables:
        return None
    retrieved_lower = {t.lower() for t in retrieved_tables}
    hits = sum(1 for t in expected_tables if t.lower() in retrieved_lower)
    return hits / len(expected_tables)


def _top_level_select_columns(sql: str, dialect: str | None) -> set[str]:
    """Column names referenced anywhere in `sql` (not just the outermost
    SELECT list) -- deliberately broad: a correct query might reference an
    expected column in a WHERE/JOIN/GROUP BY clause rather than the SELECT
    list itself (e.g. filtering on it without displaying it), and column-
    selection *recall* should credit that as "the model found and used the
    right column," not just "the right column was displayed."
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except ParseError:
        return set()
    return {c.name.lower() for c in statement.find_all(exp.Column) if c.name}


def evaluate_column_selection(
    generated_sql: str | None, expected_columns: tuple[str, ...], dialect: str | None
) -> float | None:
    """Fraction of `expected_columns` referenced anywhere in `generated_sql`."""
    if not expected_columns:
        return None
    if not generated_sql:
        return 0.0
    referenced = _top_level_select_columns(generated_sql, dialect)
    hits = sum(1 for c in expected_columns if c.lower() in referenced)
    return hits / len(expected_columns)


# ---------------------------------------------------------------------------
# SQL-structure correctness (join/aggregation/date/GROUP BY/ORDER BY/NULL/
# nested-query/window-function/conditional-aggregation) -- diagnostic
# signals explaining *why* a case failed, driven by the AST, never by
# string similarity.
# ---------------------------------------------------------------------------


def evaluate_sql_structure(
    generated_sql: str | None,
    case: BenchmarkCase | FollowUpTurn,
    category: str,
    dialect: str | None,
) -> dict[str, bool]:
    """Category-conditional structural checks against the parsed AST.

    Only checks relevant to `category` are included in the returned dict --
    e.g. a `simple_filtering` case gets no `window_function_used` entry at
    all (not "False"), since that check isn't meaningful for it. This is
    what lets `eval/metrics.py` compute a clean per-check accuracy (e.g.
    "join_correctness") only over the cases that actually exercise it.
    """
    checks: dict[str, bool] = {}
    if not generated_sql:
        return checks
    try:
        statement = sqlglot.parse_one(generated_sql, read=dialect)
    except ParseError:
        return checks

    joins = list(statement.find_all(exp.Join))
    aggs = list(statement.find_all(exp.AggFunc))
    has_group = statement.args.get("group") is not None
    has_having = statement.args.get("having") is not None
    has_order = statement.args.get("order") is not None
    has_with = statement.args.get("with") is not None
    windows = list(statement.find_all(exp.Window))
    nested_selects = list(statement.find_all(exp.Select))
    is_nested = len(nested_selects) > 1
    null_checks = list(statement.find_all(exp.Is)) + list(statement.find_all(exp.Coalesce))
    case_exprs = list(statement.find_all(exp.Case))

    if category == "simple_filtering":
        checks["filter_present"] = statement.args.get("where") is not None

    if category in ("joins", "complex_joins", "multi_table_analysis"):
        expected_lower = {t.lower() for t in case.expected_tables}
        referenced_lower = {t.name.lower() for t in statement.find_all(exp.Table) if t.name}
        checks["join_present"] = len(joins) >= 1
        if expected_lower:
            checks["join_tables_correct"] = expected_lower.issubset(referenced_lower)

    if category in ("aggregation", "conditional_aggregation", "group_by_having"):
        checks["aggregation_present"] = len(aggs) >= 1

    if category == "conditional_aggregation":
        checks["conditional_aggregation_present"] = any(
            any(isinstance(n, exp.AggFunc) for n in agg.find_ancestor(exp.Select).walk())
            for agg in case_exprs
        ) or bool(case_exprs and aggs)

    if category == "group_by_having":
        checks["group_by_present"] = has_group
        checks["having_present"] = has_having

    if category == "sorting":
        checks["order_by_present"] = has_order

    if category == "date_filtering":
        where = statement.args.get("where")
        date_funcs = list(statement.find_all((exp.Year, exp.Month, exp.DateTrunc, exp.Between)))
        date_named_cols = [
            c
            for c in statement.find_all(exp.Column)
            if any(h in c.name.lower() for h in _DATE_NAME_HINTS)
        ]
        checks["date_condition_present"] = bool(where) and bool(date_funcs or date_named_cols)

    if category == "nested_queries":
        checks["nested_query_present"] = is_nested

    if category == "ctes":
        checks["cte_present"] = has_with

    if category == "window_functions":
        checks["window_function_present"] = len(windows) >= 1

    if category == "null_handling":
        checks["null_handling_present"] = len(null_checks) >= 1

    return checks


# ---------------------------------------------------------------------------
# Security evaluation (adversarial cases)
# ---------------------------------------------------------------------------

_BEHAVIOR_TO_STATUS: dict[str, str] = {
    "reject_injection": "rejected",
    "reject_off_topic": "rejected",
    "reject_empty": "rejected",
    "reject_too_long": "rejected",
    "needs_clarification": "needs_clarification",
    "fail_high_cost": "failed",
    "fail_safely": "failed",
}


def compute_complexity_score(sql: str | None, dialect: str | None) -> int | None:
    """A small, transparent, reproducible query-complexity score from the AST.

    Not calibrated against any external notion of "difficulty" -- it exists
    so accuracy can be correlated against complexity within a single run
    (see the report's failure-analysis section), which only requires the
    score to be internally consistent, not absolutely calibrated. Weighted
    so structurally "bigger" constructs (a window function, a CTE, a
    subquery) count for more than a single extra join, reflecting that
    they're typically where a local 8B model starts struggling.
    """
    if not sql:
        return None
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except ParseError:
        return None
    joins = len(list(statement.find_all(exp.Join)))
    subqueries = max(0, len(list(statement.find_all(exp.Select))) - 1)
    has_cte = 1 if statement.args.get("with") else 0
    windows = len(list(statement.find_all(exp.Window)))
    aggs = len(list(statement.find_all(exp.AggFunc)))
    has_group = 1 if statement.args.get("group") else 0
    has_having = 1 if statement.args.get("having") else 0
    return joins + subqueries * 2 + has_cte * 2 + windows * 3 + aggs + has_group + has_having


def evaluate_security(run: CaseRunResult, case: BenchmarkCase | FollowUpTurn) -> bool | None:
    """True/False for an adversarial or non-"succeed" case; None otherwise.

    Checks the terminal status matches what `expected_behavior` implies,
    plus the specific `rejection_reason` if the case names one -- "the
    agent declined" isn't enough on its own if the case is specifically
    guarding *which* gate caught it (e.g. injection-pattern detection vs.
    off-topic classification).
    """
    expected_status = _BEHAVIOR_TO_STATUS.get(case.expected_behavior)
    if expected_status is None:
        return None
    ok = run.final_status == expected_status
    expect_reason = getattr(case, "expect_rejection_reason", None)
    if ok and expect_reason:
        ok = run.rejection_reason == expect_reason
    return ok


# ---------------------------------------------------------------------------
# Overall pass/fail + failure classification
# ---------------------------------------------------------------------------


def compute_overall_pass(run: CaseRunResult, case: BenchmarkCase | FollowUpTurn) -> bool:
    """The single pass/fail verdict for one case -- combines every signal
    above according to what `expected_behavior` actually calls for.

    This is the function that implements "do not optimize for SQL string
    similarity alone": `sql_exact_match` never appears here. An ordinary
    accuracy case passes on **result-set correctness** (or, if no gold SQL
    exists for it, on the same row-count/readability/table-usage checks
    `scripts/run_eval.py` used previously) -- never on SQL text similarity.
    """
    if case.expected_behavior != "succeed":
        return bool(run.security_correct)

    if run.final_status != "succeeded":
        return False

    if run.result_set_correct is not None:
        base_pass = run.result_set_correct
    else:
        # No gold SQL/result available for this case -- fall back to the
        # legacy eval_questions.yaml-style checks (min/max rows, readable
        # result, required join tables), preserving that dataset's
        # regression coverage exactly rather than discarding it.
        base_pass = True
        if case.min_rows is not None:
            base_pass = base_pass and (run.row_count or 0) >= case.min_rows
        if case.max_rows is not None:
            base_pass = base_pass and (run.row_count or 0) <= case.max_rows
        if case.expect_readable_result and run.result_rows:
            first_row = run.result_rows[0]
            base_pass = base_pass and not (
                bool(first_row) and all(isinstance(v, int) for v in first_row)
            )
        if case.expected_tables:
            sql_text = run.generated_sql or ""
            base_pass = base_pass and all(t in sql_text for t in case.expected_tables)

    if base_pass and getattr(case, "expect_followup", False):
        base_pass = run.followup_classification == "followup"

    expect_severity = getattr(case, "expect_cost_severity", None)
    if base_pass and expect_severity:
        base_pass = run.cost_estimate_severity == expect_severity

    return bool(base_pass)


def classify_failure(run: CaseRunResult, case: BenchmarkCase | FollowUpTurn) -> tuple[str, str]:
    """Best-effort single root-cause label for a failed case, for the
    report's failure-analysis section -- checked in priority order from
    "most specific/actionable" to "least."

    Returns:
        (failure_category, human_readable_detail).
    """
    expect_severity = getattr(case, "expect_cost_severity", None)

    if case.expected_behavior != "succeed":
        if expect_severity and run.final_status == _BEHAVIOR_TO_STATUS.get(case.expected_behavior):
            return "cost_severity_mismatch", (
                f"expected cost_estimate.severity={expect_severity!r}, got "
                f"{run.cost_estimate_severity!r} (status matched: {run.final_status!r})"
            )
        return "security_miss", (
            f"expected status={_BEHAVIOR_TO_STATUS.get(case.expected_behavior)!r} "
            f"reason={getattr(case, 'expect_rejection_reason', None)!r}, got "
            f"status={run.final_status!r} reason={run.rejection_reason!r}"
        )

    if run.final_status == "needs_clarification":
        return "unexpected_clarification", "agent asked for clarification instead of answering"
    if run.final_status == "rejected":
        return (
            "unexpected_rejection",
            f"agent rejected the question (reason={run.rejection_reason!r})",
        )
    if run.final_status == "rate_limited":
        return "rate_limited", "process-wide LLM-call rate limiter tripped during this run"
    if run.final_status != "succeeded":
        last_outcome = run.attempt_history[-1]["outcome"] if run.attempt_history else None
        if last_outcome == "timeout":
            return "timeout", "query exceeded the execution timeout on every attempt"
        if last_outcome == "safety_violation":
            return "safety_violation", "generated SQL failed the validator's safety checks"
        if last_outcome == "high_cost":
            return "high_cost", "query cost estimate stayed high across every retry"
        return (
            "generation_failed",
            f"never reached a successful execution (last outcome={last_outcome!r})",
        )

    if run.retrieval_recall is not None and run.retrieval_recall < 1.0:
        return "retrieval_miss", (
            f"only {run.retrieval_recall:.0%} of expected tables were retrieved "
            f"(retrieved={run.retrieved_tables!r})"
        )

    failed_structure_checks = [name for name, ok in run.structure_checks.items() if not ok]
    if failed_structure_checks:
        return "wrong_query_structure", f"failed structural check(s): {failed_structure_checks}"

    if run.result_set_correct is False:
        return "wrong_result", (
            f"result set didn't match gold (gold_source={run.gold_source!r}): "
            f"got {run.row_count} row(s)"
        )

    if getattr(case, "expect_followup", False) and run.followup_classification != "followup":
        return "followup_misclassified", (
            f"expected classification='followup', got {run.followup_classification!r}"
        )

    if case.min_rows is not None and (run.row_count or 0) < case.min_rows:
        return "too_few_rows", f"expected >= {case.min_rows} row(s), got {run.row_count}"
    if case.max_rows is not None and (run.row_count or 0) > case.max_rows:
        return "too_many_rows", f"expected <= {case.max_rows} row(s), got {run.row_count}"

    if expect_severity and run.cost_estimate_severity != expect_severity:
        return "cost_severity_mismatch", (
            f"expected cost_estimate.severity={expect_severity!r}, got {run.cost_estimate_severity!r}"
        )

    return "unknown", "failed overall_pass but no specific signal explains why"
