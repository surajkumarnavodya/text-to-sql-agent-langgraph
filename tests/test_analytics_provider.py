"""Wiring test for analytics/provider.py's default `AnalyticsProvider`:
proves the adapter forwards real, already-computed `agent.insight
.ResultSummary` data unmodified into typed `AnalyticsFinding`s, rather
than recomputing (or silently dropping) any of it.

Mirrors tests/test_tools_definitions.py's own "assert the wrapper forwards
real data correctly" wiring-test shape.
"""

from __future__ import annotations

from analytics.models import AnalyticsFindingKind
from analytics.provider import AnalyticsProvider, ResultSummaryAnalyticsProvider

from agent.insight import summarize_result
from agent.provenance import DataTruthLevel

# One clear outlier (2012, ~2.24 population-stddevs from the mean -- 6
# points so a single outlier can actually clear OUTLIER_STDDEV_THRESHOLD;
# with only 5 points a single outlier's deviation asymptotically approaches
# but never exceeds sqrt(5-1) == 2.0 as its value grows, so it would never
# be flagged), a real up-trend from first to last period, and enough
# spread for a non-degenerate stddev -- exercises all three finding kinds
# from a single, real `summarize_result` call.
_COLUMNS = ["Year", "SalesAmount"]
_ROWS = [
    ("2010", 100.0),
    ("2011", 105.0),
    ("2012", 5000.0),
    ("2013", 110.0),
    ("2014", 108.0),
    ("2015", 120.0),
]


class TestResultSummaryAnalyticsProviderSatisfiesProtocol:
    def test_isinstance_check(self):
        assert isinstance(ResultSummaryAnalyticsProvider(), AnalyticsProvider)


class TestResultSummaryAnalyticsProviderAdapter:
    def test_row_count_forwarded_unmodified(self):
        summary = summarize_result(_COLUMNS, _ROWS)
        result = ResultSummaryAnalyticsProvider().analyze(summary)
        assert result.row_count == summary.row_count == len(_ROWS)

    def test_trend_finding_matches_the_real_computed_trend(self):
        summary = summarize_result(_COLUMNS, _ROWS)
        assert summary.trend is not None  # sanity: this dataset does produce a trend
        result = ResultSummaryAnalyticsProvider().analyze(summary)
        trend_findings = [f for f in result.findings if f.kind == AnalyticsFindingKind.TREND]
        assert len(trend_findings) == 1
        finding = trend_findings[0]
        assert finding.trend == summary.trend
        assert finding.claim.level == DataTruthLevel.DATABASE_FACT
        # The rendered claim must actually state the real, computed numbers
        # -- not a placeholder -- so a caller reading only `claim.value`
        # (without the typed `trend` field) still sees the real finding.
        assert summary.trend.first_period in finding.claim.value
        assert summary.trend.last_period in finding.claim.value
        assert f"{abs(summary.trend.change_percent):.1f}" in finding.claim.value

    def test_outlier_finding_matches_a_real_computed_outlier(self):
        summary = summarize_result(_COLUMNS, _ROWS)
        assert len(summary.outliers) >= 1  # sanity: 1000.0 is a real outlier here
        result = ResultSummaryAnalyticsProvider().analyze(summary)
        outlier_findings = [f for f in result.findings if f.kind == AnalyticsFindingKind.OUTLIER]
        assert len(outlier_findings) == len(summary.outliers)
        forwarded_outliers = {f.outlier for f in outlier_findings}
        assert forwarded_outliers == set(summary.outliers)

    def test_variance_finding_matches_the_real_computed_stddev(self):
        summary = summarize_result(_COLUMNS, _ROWS)
        sales_stat = next(s for s in summary.column_stats if s.name == "SalesAmount")
        assert sales_stat.stddev is not None  # sanity: 5 rows, real spread
        result = ResultSummaryAnalyticsProvider().analyze(summary)
        variance_findings = [f for f in result.findings if f.kind == AnalyticsFindingKind.VARIANCE]
        assert len(variance_findings) == 1
        finding = variance_findings[0]
        assert finding.variance_column == "SalesAmount"
        assert f"{sales_stat.stddev:.2f}" in finding.claim.value

    def test_no_variance_finding_for_a_column_with_no_stddev(self):
        # A single-row result: stddev is deliberately None (degenerate),
        # not a misleading 0.0 -- see agent/insight.py's ColumnStat docstring.
        summary = summarize_result(["CustomerCount"], [(1231,)])
        result = ResultSummaryAnalyticsProvider().analyze(summary)
        assert result.findings == ()

    def test_zero_new_computation_findings_are_a_pure_subset_of_summary_data(self):
        """No finding this adapter produces states a number that isn't
        already present somewhere in `summary` -- proving this is a pure
        rendering, not new statistics."""
        summary = summarize_result(_COLUMNS, _ROWS)
        result = ResultSummaryAnalyticsProvider().analyze(summary)
        allowed = summary.allowed_values() | {round(p, 1) for p in summary.allowed_percents()}
        for finding in result.findings:
            if finding.kind == AnalyticsFindingKind.TREND:
                assert finding.trend is not None
                assert round(finding.trend.change_percent, 1) in {
                    round(p, 1) for p in summary.allowed_percents()
                }
            elif finding.kind == AnalyticsFindingKind.OUTLIER:
                assert finding.outlier is not None
                assert round(finding.outlier.value, 2) in allowed
