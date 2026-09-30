"""Isolated contract tests for analytics/models.py -- no dependency on the
real adapter in analytics/provider.py (see tests/test_analytics_provider.py
for that).
"""

from __future__ import annotations

from analytics.models import AnalyticsFinding, AnalyticsFindingKind, AnalyticsResult

from agent.insight import OutlierStat, TrendStat
from agent.provenance import DataTruthLevel, ProvenancedClaim


class TestAnalyticsResult:
    def test_empty_findings_is_a_real_valid_state(self):
        result = AnalyticsResult(row_count=1)
        assert result.findings == ()

    def test_findings_tuple_is_iterable_without_none_check(self):
        result = AnalyticsResult(row_count=3, findings=())
        assert list(result.findings) == []


class TestAnalyticsFinding:
    def test_trend_finding_carries_the_typed_trend_stat(self):
        trend = TrendStat(
            label_column="Year",
            value_column="SalesAmount",
            first_period="2011",
            first_value=100.0,
            last_period="2015",
            last_value=120.0,
            change_percent=20.0,
            direction="up",
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.TREND,
            claim=ProvenancedClaim(value="Sales rose 20%.", level=DataTruthLevel.DATABASE_FACT),
            trend=trend,
        )
        assert finding.trend == trend
        assert finding.outlier is None
        assert finding.claim.level == DataTruthLevel.DATABASE_FACT

    def test_outlier_finding_carries_the_typed_outlier_stat(self):
        outlier = OutlierStat(
            label="2014", value=1000.0, column="SalesAmount", deviations_from_mean=2.5
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.OUTLIER,
            claim=ProvenancedClaim(value="2014 is an outlier.", level=DataTruthLevel.DATABASE_FACT),
            outlier=outlier,
        )
        assert finding.outlier == outlier
        assert finding.trend is None

    def test_variance_finding_carries_the_column_name(self):
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.VARIANCE,
            claim=ProvenancedClaim(value="High variance.", level=DataTruthLevel.DATABASE_FACT),
            variance_column="SalesAmount",
        )
        assert finding.variance_column == "SalesAmount"
