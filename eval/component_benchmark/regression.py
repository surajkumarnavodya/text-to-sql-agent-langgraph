"""A simple, case-level regression gate for the component benchmark.

Deliberately not a reuse of `eval.regression`'s own tolerance-band/metric-
drift logic -- that module's vocabulary (`_LOWER_IS_WORSE`/
`_HIGHER_IS_WORSE`, a named-metric allowlist, percentage tolerance bands)
is specific to the SQL benchmark's accuracy/latency/cost metrics, which
have no equivalent here: every component-benchmark case is a binary
pass/fail, not a scored percentage, so there is no "drifted but within
tolerance" case to reason about -- only "did a named case flip from pass
to fail." A generalized, metric-shape-agnostic version of `eval.regression`
would have had to either lose that module's own tolerance-band precision
or grow a branch for this shape; a second, much smaller module was the
better reuse-vs-complexity tradeoff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from eval.component_benchmark.schema import ComponentBenchmarkReport


@dataclass
class ComponentRegression:
    """One case that passed in the baseline and fails now (or vice versa --
    see `Comparison.newly_fixed`)."""

    domain: str
    case_name: str
    baseline_passed: bool
    current_passed: bool
    current_detail: str


@dataclass
class ComponentBenchmarkComparison:
    regressions: list[ComponentRegression] = field(default_factory=list)
    newly_fixed: list[ComponentRegression] = field(default_factory=list)
    new_cases: list[str] = field(default_factory=list)
    removed_cases: list[str] = field(default_factory=list)

    @property
    def has_regressions(self) -> bool:
        return bool(self.regressions)


def report_to_baseline_dict(report: ComponentBenchmarkReport) -> dict:
    """Serializes a report into the flat `{"domain/case_name": passed}`
    shape the baseline JSON file stores -- deliberately not the full
    report (detail strings, descriptions) so a baseline diff in version
    control only ever shows a real pass/fail change, never unrelated
    wording-only noise."""
    return {
        f"{domain.domain}/{result.name}": result.passed
        for domain in report.domains
        for result in domain.results
    }


def load_baseline(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(report: ComponentBenchmarkReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report_to_baseline_dict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compare_against_baseline(
    report: ComponentBenchmarkReport, baseline: dict[str, bool]
) -> ComponentBenchmarkComparison:
    """Flags every case whose outcome changed since `baseline`.

    A case present in the current run but not the baseline (a newly added
    case) is reported separately (`new_cases`), never treated as a
    regression -- there's nothing to regress *from*. Symmetrically, a case
    removed since the baseline is reported (`removed_cases`) but likewise
    never a regression.
    """
    comparison = ComponentBenchmarkComparison()
    current = {
        f"{domain.domain}/{result.name}": result
        for domain in report.domains
        for result in domain.results
    }

    for key, result in current.items():
        if key not in baseline:
            comparison.new_cases.append(key)
            continue
        baseline_passed = baseline[key]
        if baseline_passed and not result.passed:
            domain, case_name = key.split("/", 1)
            comparison.regressions.append(
                ComponentRegression(
                    domain=domain,
                    case_name=case_name,
                    baseline_passed=True,
                    current_passed=False,
                    current_detail=result.detail,
                )
            )
        elif not baseline_passed and result.passed:
            domain, case_name = key.split("/", 1)
            comparison.newly_fixed.append(
                ComponentRegression(
                    domain=domain,
                    case_name=case_name,
                    baseline_passed=False,
                    current_passed=True,
                    current_detail=result.detail,
                )
            )

    comparison.removed_cases = sorted(set(baseline) - set(current))
    return comparison
