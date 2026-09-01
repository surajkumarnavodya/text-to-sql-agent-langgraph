"""Unit tests for eval/regression.py. Fully offline."""

from __future__ import annotations

import dataclasses

from eval.metrics import build_report
from eval.regression import detect_regression
from eval.reporting import report_to_dict
from eval.schema import CaseRunResult


def _case(case_id: str, overall_pass: bool, **overrides: object) -> CaseRunResult:
    """See `tests/test_connection.py::_settings` for why `dataclasses.replace`
    is used here instead of spreading a dict into `CaseRunResult(**...)`
    directly."""
    base = CaseRunResult(
        case_id=case_id,
        question="q",
        difficulty="easy",
        category="aggregation",
        security_classification="benign",
        final_status="succeeded" if overall_pass else "failed",
    )
    run = dataclasses.replace(base, **overrides)  # type: ignore[arg-type]
    run.overall_pass = overall_pass
    return run


class TestDetectRegression:
    def test_no_change_is_no_regression(self):
        baseline_report = build_report(
            "run1", "t1", "m", "db", [_case("a", True), _case("b", True)]
        )
        baseline = report_to_dict(baseline_report)
        current_report = build_report("run2", "t2", "m", "db", [_case("a", True), _case("b", True)])

        result = detect_regression(baseline, current_report)
        assert result.has_regression is False

    def test_accuracy_drop_beyond_tolerance_is_flagged(self):
        baseline_report = build_report(
            "run1", "t1", "m", "db", [_case(str(i), True) for i in range(10)]
        )
        baseline = report_to_dict(baseline_report)
        # 5/10 now fail -- a large, real drop.
        current_results = [_case(str(i), i < 5) for i in range(10)]
        current_report = build_report("run2", "t2", "m", "db", current_results)

        result = detect_regression(baseline, current_report)
        assert result.has_regression is True
        assert any(r.metric == "final_accuracy" for r in result.metric_regressions)

    def test_small_change_within_tolerance_is_not_flagged(self):
        baseline_report = build_report(
            "run1", "t1", "m", "db", [_case(str(i), True) for i in range(100)]
        )
        baseline = report_to_dict(baseline_report)
        # 99/100 pass -- a 1% drop, well within the default 3% tolerance.
        current_results = [_case(str(i), i != 0) for i in range(100)]
        current_report = build_report("run2", "t2", "m", "db", current_results)

        result = detect_regression(baseline, current_report)
        assert not any(r.metric == "final_accuracy" for r in result.metric_regressions)

    def test_specific_case_flip_is_detected_even_if_aggregate_unmoved(self):
        """A single case flipping from pass to fail in a large run barely
        moves the aggregate accuracy -- the per-case check must still catch
        it by name."""
        baseline_report = build_report(
            "run1", "t1", "m", "db", [_case(str(i), True) for i in range(50)]
        )
        baseline = report_to_dict(baseline_report)
        current_results = [_case(str(i), i != 3) for i in range(50)]
        current_report = build_report("run2", "t2", "m", "db", current_results)

        result = detect_regression(baseline, current_report)
        assert "3" in result.newly_failing_cases

    def test_latency_regression_uses_relative_tolerance(self):
        baseline_report = build_report(
            "run1", "t1", "m", "db", [_case("a", True, wall_time_seconds=10.0)]
        )
        baseline = report_to_dict(baseline_report)
        # 2x slower -- well past the default 20% relative tolerance.
        current_report = build_report(
            "run2", "t2", "m", "db", [_case("a", True, wall_time_seconds=20.0)]
        )

        result = detect_regression(baseline, current_report)
        assert any(r.metric == "average_latency_seconds" for r in result.metric_regressions)

    def test_case_removed_from_dataset_is_not_a_regression(self):
        baseline_report = build_report(
            "run1", "t1", "m", "db", [_case("a", True), _case("removed_case", True)]
        )
        baseline = report_to_dict(baseline_report)
        current_report = build_report("run2", "t2", "m", "db", [_case("a", True)])

        result = detect_regression(baseline, current_report)
        assert result.newly_failing_cases == []

    def test_summary_is_human_readable(self):
        baseline_report = build_report("run1", "t1", "m", "db", [_case("a", True)])
        baseline = report_to_dict(baseline_report)
        current_report = build_report("run2", "t2", "m", "db", [_case("a", False)])

        result = detect_regression(baseline, current_report)
        assert "a" in result.summary()

    def test_no_regression_summary(self):
        baseline_report = build_report("run1", "t1", "m", "db", [_case("a", True)])
        baseline = report_to_dict(baseline_report)
        current_report = build_report("run2", "t2", "m", "db", [_case("a", True)])

        result = detect_regression(baseline, current_report)
        assert result.summary() == "No regression detected."
