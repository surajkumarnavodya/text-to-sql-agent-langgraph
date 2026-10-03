"""Runs every domain's cases and produces a `ComponentBenchmarkReport`.

No live-agent/DB/LLM dependency at all -- every case calls a real
production function directly and synchronously, which is what makes this
runnable both from `scripts/run_component_benchmark.py` (operator use) and
directly inside the normal pytest suite (`tests/test_eval_component_
benchmark.py`), unlike `eval.runner`/`eval.security_benchmark.runner`.
"""

from __future__ import annotations

from eval.component_benchmark import (
    cases_analytics,
    cases_planning,
    cases_recommendations,
    cases_semantic,
)
from eval.component_benchmark.schema import (
    ComponentBenchmarkReport,
    ComponentCaseResult,
    ComponentDomainReport,
)

#: One entry per domain, in report order -- the single place that knows
#: about all four domain modules; adding a fifth domain means adding one
#: line here; no other module needs to change.
_DOMAIN_MODULES = {
    "semantic": cases_semantic,
    "planning": cases_planning,
    "analytics": cases_analytics,
    "recommendations": cases_recommendations,
}


def run_all() -> ComponentBenchmarkReport:
    """Runs every case in every domain and returns the full report.

    A case whose `run()` itself raises is recorded as a failure (not
    propagated) -- an unexpected exception from a production function is
    exactly the kind of regression this harness exists to catch, not a
    reason to crash the whole run before the remaining cases are tried.
    """
    domain_reports: list[ComponentDomainReport] = []
    for domain_name, module in _DOMAIN_MODULES.items():
        results: list[ComponentCaseResult] = []
        for case in module.CASES:
            try:
                passed, detail = case.run()
            except (
                Exception
            ) as exc:  # noqa: BLE001 - a case's own crash is a result, not a harness failure
                passed, detail = False, f"raised {type(exc).__name__}: {exc}"
            results.append(
                ComponentCaseResult(
                    name=case.name, description=case.description, passed=passed, detail=detail
                )
            )
        domain_reports.append(ComponentDomainReport(domain=domain_name, results=results))
    return ComponentBenchmarkReport(domains=domain_reports)
