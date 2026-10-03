"""Tests for `eval/component_benchmark/` -- Prompt 23 (observability,
evaluation & reliability).

Unlike `eval/`'s own SQL benchmark or `eval/security_benchmark/`, this
harness needs no live database or LLM -- every case calls a real,
deterministic production function directly. That's what makes it possible
(and valuable) to run it on every normal `pytest` invocation: a real
regression in `agent.llm_client._build_mandatory_metrics_block`,
`agent.plan_validator.validate_plan`, `analytics.engine
.compute_analytics_result`, or `recommendation.engine
.generate_recommendations` is now caught by CI the moment it's
introduced, not only by whichever unit test happened to also cover that
exact code path.
"""

from __future__ import annotations

import pytest

from eval.component_benchmark import (
    cases_analytics,
    cases_planning,
    cases_recommendations,
    cases_semantic,
)
from eval.component_benchmark.regression import (
    ComponentRegression,
    compare_against_baseline,
    report_to_baseline_dict,
)
from eval.component_benchmark.runner import run_all
from eval.component_benchmark.schema import (
    ComponentBenchmarkReport,
    ComponentCaseResult,
    ComponentDomainReport,
)

_ALL_DOMAIN_MODULES = {
    "semantic": cases_semantic,
    "planning": cases_planning,
    "analytics": cases_analytics,
    "recommendations": cases_recommendations,
}
_ALL_CASES = [
    pytest.param(domain_name, case, id=f"{domain_name}/{case.name}")
    for domain_name, module in _ALL_DOMAIN_MODULES.items()
    for case in module.CASES
]


class TestEveryComponentCasePasses:
    """Parametrized so a single failing case is reported by name, not
    buried inside one big aggregate assertion."""

    @pytest.mark.parametrize("domain_name,case", _ALL_CASES)
    def test_case_passes(self, domain_name, case):
        passed, detail = case.run()
        assert passed, f"{domain_name}/{case.name} failed: {detail}"


class TestEveryDomainHasCases:
    """A domain module that accidentally ends up with an empty `CASES`
    list would otherwise pass silently -- this would fail loudly."""

    @pytest.mark.parametrize("domain_name,module", list(_ALL_DOMAIN_MODULES.items()))
    def test_domain_has_at_least_one_case(self, domain_name, module):
        assert len(module.CASES) > 0

    def test_every_case_name_is_unique_within_its_domain(self):
        for domain_name, module in _ALL_DOMAIN_MODULES.items():
            names = [case.name for case in module.CASES]
            assert len(names) == len(set(names)), f"duplicate case name within {domain_name}"


class TestRunAll:
    def test_produces_one_domain_report_per_module(self):
        report = run_all()
        assert {d.domain for d in report.domains} == set(_ALL_DOMAIN_MODULES)

    def test_all_passed_matches_reality(self):
        """Every case already passes individually (see above) -- the
        aggregate report must agree."""
        report = run_all()
        assert report.all_passed is True
        assert report.failures() == []

    def test_total_cases_matches_the_sum_of_every_domain(self):
        report = run_all()
        expected = sum(len(m.CASES) for m in _ALL_DOMAIN_MODULES.values())
        assert report.total_cases == expected
        assert report.total_passed == expected

    def test_a_case_that_raises_is_recorded_as_a_failure_not_propagated(self, monkeypatch):
        """The runner's own fail-safe: one case's internal bug must not
        take down the whole run before the remaining cases are tried."""

        def _boom():
            raise RuntimeError("case exploded")

        from dataclasses import replace

        original_cases = list(cases_semantic.CASES)
        broken = replace(
            original_cases[0], run=_boom
        )  # ComponentCase is frozen -- replace, not mutate
        monkeypatch.setattr(cases_semantic, "CASES", [broken, *original_cases[1:]])

        report = run_all()
        semantic_report = next(d for d in report.domains if d.domain == "semantic")
        assert semantic_report.results[0].passed is False
        assert "RuntimeError" in semantic_report.results[0].detail


class TestReportToBaselineDict:
    def test_flattens_to_domain_slash_name_keys(self):
        report = ComponentBenchmarkReport(
            domains=[
                ComponentDomainReport(
                    domain="semantic",
                    results=[
                        ComponentCaseResult(
                            name="case_a", description="d", passed=True, detail="ok"
                        )
                    ],
                )
            ]
        )
        assert report_to_baseline_dict(report) == {"semantic/case_a": True}


class TestCompareAgainstBaseline:
    def _report(self, passed: bool, detail: str = "detail") -> ComponentBenchmarkReport:
        return ComponentBenchmarkReport(
            domains=[
                ComponentDomainReport(
                    domain="semantic",
                    results=[
                        ComponentCaseResult(
                            name="case_a", description="d", passed=passed, detail=detail
                        )
                    ],
                )
            ]
        )

    def test_no_change_is_no_regression(self):
        comparison = compare_against_baseline(self._report(True), {"semantic/case_a": True})
        assert comparison.regressions == []
        assert comparison.has_regressions is False

    def test_pass_to_fail_is_a_regression(self):
        comparison = compare_against_baseline(
            self._report(False, "now broken"), {"semantic/case_a": True}
        )
        assert len(comparison.regressions) == 1
        reg = comparison.regressions[0]
        assert reg == ComponentRegression(
            domain="semantic",
            case_name="case_a",
            baseline_passed=True,
            current_passed=False,
            current_detail="now broken",
        )
        assert comparison.has_regressions is True

    def test_fail_to_pass_is_newly_fixed_not_a_regression(self):
        comparison = compare_against_baseline(self._report(True), {"semantic/case_a": False})
        assert comparison.regressions == []
        assert len(comparison.newly_fixed) == 1
        assert comparison.has_regressions is False

    def test_a_case_not_in_the_baseline_is_a_new_case_not_a_regression(self):
        comparison = compare_against_baseline(self._report(False, "detail"), {})
        assert comparison.regressions == []
        assert comparison.new_cases == ["semantic/case_a"]

    def test_a_baseline_case_no_longer_run_is_reported_as_removed(self):
        comparison = compare_against_baseline(
            self._report(True), {"semantic/case_a": True, "semantic/case_gone": True}
        )
        assert comparison.removed_cases == ["semantic/case_gone"]
        assert comparison.regressions == []


class TestCommittedBaselineIsInSync:
    """The committed `eval/baselines/component_benchmark_latest.json`
    must name exactly the cases that exist today -- a stale baseline
    (one that still lists a renamed/removed case, or is missing a newly
    added one) would silently stop acting as a real regression gate for
    whichever case drifted out of sync."""

    def test_baseline_file_has_no_new_or_removed_cases_against_current_code(self):
        import json
        from pathlib import Path

        baseline_path = (
            Path(__file__).resolve().parent.parent
            / "eval"
            / "baselines"
            / "component_benchmark_latest.json"
        )
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        report = run_all()
        comparison = compare_against_baseline(report, baseline)

        assert comparison.new_cases == [], (
            f"case(s) exist in code but not in the committed baseline: {comparison.new_cases} -- "
            "run `python scripts/run_component_benchmark.py --save-baseline` and commit the update"
        )
        assert comparison.removed_cases == [], (
            f"baseline still lists case(s) no longer in code: {comparison.removed_cases} -- "
            "run `python scripts/run_component_benchmark.py --save-baseline` and commit the update"
        )
        assert comparison.regressions == []
