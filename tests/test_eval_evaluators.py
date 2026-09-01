"""Unit tests for eval/evaluators.py -- the core grading logic. Fully
offline (a fake SQLAlchemy engine stands in for the live database used by
`evaluate_result_set`'s gold-SQL execution)."""

from __future__ import annotations

import dataclasses

from eval.evaluators import (
    classify_failure,
    compare_result_sets,
    compute_complexity_score,
    compute_overall_pass,
    evaluate_column_selection,
    evaluate_result_set,
    evaluate_retrieval,
    evaluate_security,
    evaluate_sql_exact_match,
    evaluate_sql_structure,
)
from eval.schema import BenchmarkCase, CaseRunResult

_BASE_CASE = BenchmarkCase(
    id="c1", question="q", database="d", difficulty="easy", category="aggregation"
)
_BASE_RUN = CaseRunResult(
    case_id="c1",
    question="q",
    difficulty="easy",
    category="aggregation",
    security_classification="benign",
)


def _case(**overrides: object) -> BenchmarkCase:
    """See `tests/test_connection.py::_settings` for why `dataclasses.replace`
    is used here instead of spreading a dict into `BenchmarkCase(**...)`
    directly."""
    return dataclasses.replace(_BASE_CASE, **overrides)  # type: ignore[arg-type]


def _run(**overrides: object) -> CaseRunResult:
    return dataclasses.replace(_BASE_RUN, **overrides)  # type: ignore[arg-type]


class _FakeCursorResult:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def keys(self):
        return self._columns

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeConnection:
    def __init__(self, table: dict[str, tuple]) -> None:
        self._table = table

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        text = str(sql)
        columns, rows = self._table[text]
        return _FakeCursorResult(columns, rows)


class _FakeEngine:
    """Maps exact SQL text -> (columns, rows) -- a deterministic stand-in
    for a live database, since evaluate_result_set only ever needs "run
    this exact gold SQL and get this exact result back."""

    def __init__(self, table: dict[str, tuple]) -> None:
        self._table = table

    def connect(self):
        return _FakeConnection(self._table)


class TestCompareResultSets:
    def test_identical_rows_match(self):
        assert compare_result_sets([(1, "a")], [(1, "a")])

    def test_row_order_ignored_by_default(self):
        assert compare_result_sets([(1,), (2,)], [(2,), (1,)])

    def test_order_matters_flag_requires_sequence_match(self):
        assert not compare_result_sets([(1,), (2,)], [(2,), (1,)], order_matters=True)
        assert compare_result_sets([(1,), (2,)], [(1,), (2,)], order_matters=True)

    def test_column_order_within_row_ignored(self):
        assert compare_result_sets([("a", 1)], [(1, "a")])

    def test_numeric_tolerance(self):
        assert compare_result_sets([(100.001,)], [(100.0,)])

    def test_mismatch_detected(self):
        assert not compare_result_sets([(1,)], [(2,)])

    def test_different_row_counts_mismatch(self):
        assert not compare_result_sets([(1,), (2,)], [(1,)])


class TestEvaluateResultSet:
    def test_matches_gold_from_expected_sql(self):
        engine = _FakeEngine({"SELECT COUNT(*) FROM t": (["cnt"], [(5,)])})
        case = _case(expected_sql="SELECT COUNT(*) FROM t")
        run = _run(result_columns=["x"], result_rows=[(5,)], row_count=1)
        evaluate_result_set(run, case, engine)
        assert run.result_set_correct is True
        assert run.gold_source == "expected_sql"

    def test_falls_back_to_alternative_sql(self):
        engine = _FakeEngine(
            {
                "SELECT 1": (["x"], [(1,)]),
                "SELECT 2": (["x"], [(2,)]),
            }
        )
        case = _case(expected_sql="SELECT 1", alternative_sql=("SELECT 2",))
        run = _run(result_columns=["x"], result_rows=[(2,)], row_count=1)
        evaluate_result_set(run, case, engine)
        assert run.result_set_correct is True
        assert run.gold_source == "alternative_sql[0]"

    def test_no_match_is_false(self):
        engine = _FakeEngine({"SELECT 1": (["x"], [(1,)])})
        case = _case(expected_sql="SELECT 1")
        run = _run(result_columns=["x"], result_rows=[(999,)], row_count=1)
        evaluate_result_set(run, case, engine)
        assert run.result_set_correct is False

    def test_no_gold_sql_and_no_expected_result_is_not_applicable(self):
        engine = _FakeEngine({})
        case = _case()
        run = _run(result_columns=["x"], result_rows=[(1,)], row_count=1)
        evaluate_result_set(run, case, engine)
        assert run.result_set_correct is None

    def test_agent_did_not_execute_is_not_applicable(self):
        engine = _FakeEngine({})
        case = _case(expected_sql="SELECT 1")
        run = _run(result_columns=None, row_count=None)
        evaluate_result_set(run, case, engine)
        assert run.result_set_correct is None


class TestEvaluateSqlExactMatch:
    def test_structurally_identical_but_textually_different_is_true(self):
        case = _case(expected_sql="select count(*) from t")
        assert evaluate_sql_exact_match("SELECT COUNT(*) FROM t", case, None) is True

    def test_different_query_is_false(self):
        case = _case(expected_sql="SELECT COUNT(*) FROM t")
        assert evaluate_sql_exact_match("SELECT COUNT(1) FROM t", case, None) is False

    def test_no_expected_sql_is_not_applicable(self):
        case = _case()
        assert evaluate_sql_exact_match("SELECT 1", case, None) is None

    def test_no_generated_sql_is_not_applicable(self):
        case = _case(expected_sql="SELECT 1")
        assert evaluate_sql_exact_match(None, case, None) is None


class TestEvaluateRetrieval:
    def test_full_recall(self):
        assert evaluate_retrieval(["A", "B", "C"], ("A", "B")) == 1.0

    def test_partial_recall(self):
        assert evaluate_retrieval(["A"], ("A", "B")) == 0.5

    def test_case_insensitive(self):
        assert evaluate_retrieval(["dimcustomer"], ("DimCustomer",)) == 1.0

    def test_no_expected_tables_not_applicable(self):
        assert evaluate_retrieval(["A"], ()) is None


class TestEvaluateColumnSelection:
    def test_recall_over_referenced_columns(self):
        sql = "SELECT a, b FROM t WHERE c = 1"
        assert evaluate_column_selection(sql, ("a", "b", "c"), None) == 1.0

    def test_partial_recall(self):
        sql = "SELECT a FROM t"
        assert evaluate_column_selection(sql, ("a", "missing"), None) == 0.5

    def test_no_expected_columns_not_applicable(self):
        assert evaluate_column_selection("SELECT 1", (), None) is None

    def test_no_sql_is_zero(self):
        assert evaluate_column_selection(None, ("a",), None) == 0.0


class TestEvaluateSqlStructure:
    def test_only_relevant_checks_included(self):
        case = _case(category="joins", expected_tables=("A", "B"))
        checks = evaluate_sql_structure("SELECT * FROM A JOIN B ON A.id=B.id", case, "joins", None)
        assert "join_present" in checks
        assert "window_function_present" not in checks

    def test_no_sql_returns_empty(self):
        case = _case(category="joins")
        assert evaluate_sql_structure(None, case, "joins", None) == {}


class TestComputeComplexityScore:
    def test_none_for_no_sql(self):
        assert compute_complexity_score(None, None) is None

    def test_simple_query_is_low(self):
        assert compute_complexity_score("SELECT 1", None) == 0

    def test_more_complex_query_scores_higher(self):
        simple = compute_complexity_score("SELECT * FROM t", None)
        complex_ = compute_complexity_score(
            "WITH x AS (SELECT 1 a) SELECT RANK() OVER (ORDER BY a) FROM x", None
        )
        assert simple is not None
        assert complex_ is not None
        assert complex_ > simple


class TestEvaluateSecurity:
    def test_matches_expected_status_and_reason(self):
        case = _case(
            expected_behavior="reject_injection",
            expect_rejection_reason="injection_detected",
            security_classification="adversarial",
        )
        run = _run(final_status="rejected", rejection_reason="injection_detected")
        assert evaluate_security(run, case) is True

    def test_wrong_reason_fails(self):
        case = _case(
            expected_behavior="reject_injection", expect_rejection_reason="injection_detected"
        )
        run = _run(final_status="rejected", rejection_reason="off_topic")
        assert evaluate_security(run, case) is False

    def test_succeed_case_is_not_applicable(self):
        case = _case(expected_behavior="succeed")
        run = _run(final_status="succeeded")
        assert evaluate_security(run, case) is None


class TestComputeOverallPass:
    def test_adversarial_case_uses_security_correct(self):
        case = _case(expected_behavior="reject_off_topic", security_classification="adversarial")
        run = _run(final_status="rejected")
        run.security_correct = True
        assert compute_overall_pass(run, case) is True

    def test_ordinary_case_requires_succeeded_status(self):
        case = _case(expected_sql="SELECT 1")
        run = _run(final_status="failed")
        assert compute_overall_pass(run, case) is False

    def test_ordinary_case_uses_result_set_correct_when_available(self):
        case = _case(expected_sql="SELECT 1")
        run = _run(final_status="succeeded")
        run.result_set_correct = True
        assert compute_overall_pass(run, case) is True
        run.result_set_correct = False
        assert compute_overall_pass(run, case) is False

    def test_fallback_to_min_rows_when_no_gold(self):
        case = _case(min_rows=2)
        run = _run(final_status="succeeded", row_count=1)
        assert compute_overall_pass(run, case) is False
        run.row_count = 3
        assert compute_overall_pass(run, case) is True

    def test_cost_severity_gate(self):
        case = _case(expect_cost_severity="moderate")
        run = _run(final_status="succeeded")
        run.cost_estimate_severity = "low"
        assert compute_overall_pass(run, case) is False
        run.cost_estimate_severity = "moderate"
        assert compute_overall_pass(run, case) is True


class TestClassifyFailure:
    def test_security_miss_labeled(self):
        case = _case(expected_behavior="reject_off_topic", expect_rejection_reason="off_topic")
        run = _run(final_status="succeeded")
        category, _ = classify_failure(run, case)
        assert category == "security_miss"

    def test_wrong_result_labeled(self):
        case = _case(expected_sql="SELECT 1")
        run = _run(final_status="succeeded", row_count=1)
        run.result_set_correct = False
        category, _ = classify_failure(run, case)
        assert category == "wrong_result"

    def test_cost_severity_mismatch_labeled(self):
        case = _case(expect_cost_severity="high", expected_behavior="fail_high_cost")
        run = _run(final_status="failed", cost_estimate_severity="moderate")
        category, _ = classify_failure(run, case)
        assert category == "cost_severity_mismatch"
