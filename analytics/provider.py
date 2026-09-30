"""The `AnalyticsProvider` interface, plus one real default implementation
that adapts `agent.insight.ResultSummary` -- computed once per successful
execution, already tested, already folded into
`agent.insight.is_insight_grounded`'s allowed-value sets, but never
rendered anywhere today (see
`01_BASELINE_ARCHITECTURE_AND_REUSE_INVENTORY.md` §2's own finding) --
into this module's typed `AnalyticsResult`.

Zero new statistics are computed here. This is purely an adapter: it
renders `ResultSummary.trend`, `.outliers`, and each numeric
`ColumnStat.stddev`/`.coefficient_of_variation` into typed
`AnalyticsFinding`s. Not called by `agent.nodes.generate_insight_node` or
any route in this increment -- see `02_TARGET_ARCHITECTURE.md`'s
"stubbed today, wired later" table.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.insight import OutlierStat, ResultSummary, TrendStat
from agent.provenance import DataTruthLevel, ProvenancedClaim
from analytics.models import AnalyticsFinding, AnalyticsFindingKind, AnalyticsResult


@runtime_checkable
class AnalyticsProvider(Protocol):
    """A source of analytics findings for one query result.

    `runtime_checkable` so a test (or a future caller) can assert an object
    satisfies this shape with `isinstance(obj, AnalyticsProvider)`, the same
    convention `retrieval.vector_store`'s own `VectorStore` Protocol
    already establishes in this codebase -- dependency inversion via a
    structural type, not an ABC requiring explicit subclassing.
    """

    def analyze(self, summary: ResultSummary) -> AnalyticsResult:
        """Returns every analytics finding derivable from `summary`."""
        ...


class ResultSummaryAnalyticsProvider:
    """The default, and today the only, `AnalyticsProvider` -- a pure
    adapter over `agent.insight.ResultSummary`, computing nothing new.
    """

    def analyze(self, summary: ResultSummary) -> AnalyticsResult:
        findings: list[AnalyticsFinding] = []

        if summary.trend is not None:
            findings.append(_trend_finding(summary.trend))

        for outlier in summary.outliers:
            findings.append(_outlier_finding(outlier))

        for stat in summary.column_stats:
            if stat.is_numeric and stat.stddev is not None:
                findings.append(
                    _variance_finding(stat.name, stat.stddev, stat.coefficient_of_variation)
                )

        return AnalyticsResult(row_count=summary.row_count, findings=tuple(findings))


def _trend_finding(trend: TrendStat) -> AnalyticsFinding:
    direction_word = {"up": "rose", "down": "fell", "flat": "stayed flat"}[trend.direction]
    text = (
        f"{trend.value_column} {direction_word} {abs(trend.change_percent):.1f}% "
        f"from {trend.first_period} to {trend.last_period}."
    )
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.TREND,
        claim=ProvenancedClaim(
            value=text,
            level=DataTruthLevel.DATABASE_FACT,
            source="agent.insight.ResultSummary.trend",
        ),
        trend=trend,
    )


def _outlier_finding(outlier: OutlierStat) -> AnalyticsFinding:
    text = (
        f"{outlier.label} is an outlier on {outlier.column}: {outlier.value:.2f} "
        f"({outlier.deviations_from_mean:+.2f} standard deviations from the mean)."
    )
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.OUTLIER,
        claim=ProvenancedClaim(
            value=text,
            level=DataTruthLevel.DATABASE_FACT,
            source="agent.insight.ResultSummary.outliers",
        ),
        outlier=outlier,
    )


def _variance_finding(
    column_name: str, stddev: float, coefficient_of_variation: float | None
) -> AnalyticsFinding:
    text = f"{column_name} has a standard deviation of {stddev:.2f}"
    if coefficient_of_variation is not None:
        text += f" (coefficient of variation {coefficient_of_variation:.2f})"
    text += "."
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.VARIANCE,
        claim=ProvenancedClaim(
            value=text,
            level=DataTruthLevel.DATABASE_FACT,
            source="agent.insight.ResultSummary.column_stats",
        ),
        variance_column=column_name,
    )
