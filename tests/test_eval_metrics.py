"""Unit tests for eval/metrics.py. Fully offline -- synthetic
`CaseRunResult`s, no live agent/DB involved."""

from __future__ import annotations

import dataclasses

from eval.metrics import build_report, compute_breakdown, compute_metrics
from eval.schema import CaseRunResult

_BASE_CASE = CaseRunResult(
    case_id="c1",
    question="q",
    difficulty="easy",
    category="aggregation",
    security_classification="benign",
    final_status="succeeded",
    row_count=1,
    retry_count=0,
    llm_call_count=1,
    wall_time_seconds=10.0,
)


def _passing_case(**overrides: object) -> CaseRunResult:
    """A `_BASE_CASE` copy with `overrides` applied -- see
    `tests/test_connection.py::_settings` for why `dataclasses.replace` is
    used here instead of spreading a dict into `CaseRunResult(**...)`
    directly."""
    run = dataclasses.replace(_BASE_CASE, **overrides)  # type: ignore[arg-type]
    run.overall_pass = True
    run.result_set_correct = True
    return run


class TestComputeMetrics:
    def test_not_applicable_metrics_are_none_not_zero(self):
        """A run with no join-category cases must report join_correctness
        as None ('not measured'), never silently 0.0 ('measured, 0%
        correct') -- these mean very different things to a reader."""
        results = [_passing_case()]
        metrics = compute_metrics(results)
        assert metrics["join_correctness"] is None

    def test_final_accuracy_reflects_pass_rate(self):
        results = [_passing_case(case_id="a"), _passing_case(case_id="b")]
        results[1].overall_pass = False
        metrics = compute_metrics(results)
        assert metrics["final_accuracy"] == 0.5

    def test_security_rejection_accuracy_only_over_adversarial_cases(self):
        benign = _passing_case(case_id="a")
        adv = _passing_case(
            case_id="b", security_classification="adversarial", final_status="rejected"
        )
        adv.security_correct = True
        adv.result_set_correct = None
        metrics = compute_metrics([benign, adv])
        assert metrics["security_rejection_accuracy"] == 1.0
        # The adversarial case must not count toward ordinary accuracy metrics.
        assert metrics["final_accuracy"] == 1.0  # only `benign` is in scope for this metric

    def test_first_attempt_vs_retry_success_rate(self):
        first_try = _passing_case(case_id="a", retry_count=0)
        retried_and_succeeded = _passing_case(case_id="b", retry_count=1)
        retried_and_failed = _passing_case(case_id="c", retry_count=2, final_status="failed")
        retried_and_failed.overall_pass = False
        metrics = compute_metrics([first_try, retried_and_succeeded, retried_and_failed])
        assert metrics["first_attempt_success_rate"] == 1.0  # only `first_try` had retry_count==0
        assert metrics["retry_success_rate"] == 0.5  # 1 of 2 retried cases ultimately succeeded

    def test_latency_metrics(self):
        results = [_passing_case(case_id=str(i), wall_time_seconds=float(i)) for i in range(1, 11)]
        metrics = compute_metrics(results)
        assert metrics["average_latency_seconds"] == 5.5
        assert metrics["p95_latency_seconds"] == 10.0

    def test_structural_correctness_only_over_matching_category(self):
        join_case = _passing_case(case_id="a", category="joins")
        join_case.structure_checks = {"join_present": True, "join_tables_correct": False}
        other_case = _passing_case(case_id="b", category="sorting")
        metrics = compute_metrics([join_case, other_case])
        assert metrics["join_correctness"] is not None
        assert 0.0 <= metrics["join_correctness"] <= 1.0


class TestComputeBreakdown:
    def test_groups_by_category(self):
        results = [
            _passing_case(case_id="a", category="joins"),
            _passing_case(case_id="b", category="joins"),
            _passing_case(case_id="c", category="sorting"),
        ]
        results[1].overall_pass = False
        breakdown = compute_breakdown(results, "category")
        assert breakdown["joins"]["count"] == 2.0
        assert breakdown["joins"]["pass_rate"] == 0.5
        assert breakdown["sorting"]["count"] == 1.0


class TestBuildReport:
    def test_report_has_failures_property(self):
        passing = _passing_case(case_id="a")
        failing = _passing_case(case_id="b")
        failing.overall_pass = False
        report = build_report("run1", "2026-01-01", "model", "db", [passing, failing])
        assert report.total_cases == 2
        assert len(report.failures) == 1
        assert report.failures[0].case_id == "b"
