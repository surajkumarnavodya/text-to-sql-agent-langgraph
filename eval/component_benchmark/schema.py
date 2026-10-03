"""Dataset/result schema for the component benchmark.

Deliberately simpler than `eval.schema`'s `BenchmarkCase`/`BenchmarkReport`:
a component case is a plain `(name, description, run) -> bool` triple --
`run` calls a real production function directly and returns whether its
output matched expectations, not a graded accuracy score. There's no
equivalent of `eval.schema`'s gold-SQL-execution machinery here because
every one of the four domains this package covers is a pure function with
one correct, directly-assertable answer per case -- the thing being
measured is "did this stay correct," not "how close was it."
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ComponentCase:
    """One named, hand-authored scenario for one domain.

    Attributes:
        name: Stable, unique (within its domain) identifier -- this is
            the key `ComponentRegressionResult` flips are reported
            against, so renaming a case is a breaking change to the
            baseline the same way renaming a SQL benchmark case's `id`
            would be.
        description: One sentence of what this case is checking and why,
            for a human reading a failure report.
        run: Takes nothing, returns `(passed, detail)` -- `detail` is a
            short human-readable explanation always included in the
            report, not just on failure, so a passing run's report is
            still informative about what was actually checked.
    """

    name: str
    description: str
    run: Callable[[], tuple[bool, str]]


@dataclass
class ComponentCaseResult:
    """One case's outcome from one run."""

    name: str
    description: str
    passed: bool
    detail: str


@dataclass
class ComponentDomainReport:
    """One domain's (e.g. "analytics") full set of case results."""

    domain: str
    results: list[ComponentCaseResult] = field(default_factory=list)

    @property
    def cases_run(self) -> int:
        return len(self.results)

    @property
    def cases_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)


@dataclass
class ComponentBenchmarkReport:
    """The full run's report, across every domain."""

    domains: list[ComponentDomainReport] = field(default_factory=list)

    @property
    def total_cases(self) -> int:
        return sum(d.cases_run for d in self.domains)

    @property
    def total_passed(self) -> int:
        return sum(d.cases_passed for d in self.domains)

    @property
    def all_passed(self) -> bool:
        return all(d.all_passed for d in self.domains)

    def failures(self) -> list[tuple[str, ComponentCaseResult]]:
        """Every `(domain, result)` pair where `result.passed` is False,
        across all domains, in domain order."""
        return [(d.domain, r) for d in self.domains for r in d.results if not r.passed]
