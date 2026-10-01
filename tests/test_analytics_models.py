"""Isolated contract tests for analytics/models.py -- no dependency on the
real adapter in analytics/provider.py (see tests/test_analytics_provider.py
for that), or on the new engine in analytics/engine.py (see
tests/test_analytics_engine.py for that).
"""

from __future__ import annotations

from analytics.models import (
    ANALYTICS_ENGINE_VERSION,
    AnalyticsFinding,
    AnalyticsFindingKind,
    AnalyticsResult,
    AnomalyDetectionResult,
    AnomalyMethod,
    AnomalyPoint,
    AnomalySignal,
    ColumnSummaryStat,
    ContributorStat,
    DistinctCountStat,
    GrowthPoint,
    GrowthStat,
    MedianStat,
    OutlierFinding,
    PercentileStat,
    RankingEntry,
    RankingStat,
    ResultShape,
    RootCauseResult,
    VarianceStat,
)

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


class TestPreExistingShapeIsUnchanged:
    """Prompt 13 (13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md) extended this
    module additively -- these regression tests pin down that every
    pre-Prompt-13 construction call and field still validates exactly as
    it did before, independent of tests/test_analytics_provider.py (which
    exercises the real adapter, not bare model construction)."""

    def test_analytics_result_construction_with_only_the_original_fields_still_works(self):
        result = AnalyticsResult(row_count=5, findings=())
        assert result.shape is None
        assert result.engine_version == ANALYTICS_ENGINE_VERSION
        assert result.insufficient_data_reasons == ()

    def test_new_analytics_finding_fields_default_to_none(self):
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.VARIANCE,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            variance_column="SalesAmount",
        )
        assert finding.column_summary is None
        assert finding.variance is None
        assert finding.median is None
        assert finding.percentile is None
        assert finding.distinct_count is None
        assert finding.growth is None
        assert finding.ranking is None
        assert finding.outlier_detail is None


class TestResultShape:
    def test_six_values(self):
        assert {s.value for s in ResultShape} == {
            "scalar",
            "time_series",
            "categorical_aggregate",
            "multidimensional",
            "raw_table",
            "empty",
        }


class TestNewFindingKindsAndTypedStats:
    def test_column_summary_stat(self):
        stat = ColumnSummaryStat(
            column="Amount",
            count=10,
            null_count=1,
            is_numeric=True,
            minimum=1.0,
            maximum=9.0,
            mean=5.0,
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.COLUMN_SUMMARY,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            column_summary=stat,
        )
        assert finding.column_summary == stat

    def test_variance_stat_is_typed_unlike_the_legacy_variance_column_field(self):
        stat = VarianceStat(column="Amount", variance=4.0, stddev=2.0, coefficient_of_variation=0.5)
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.VARIANCE,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            variance=stat,
        )
        assert finding.variance is not None
        assert finding.variance.stddev == 2.0
        assert finding.variance_column is None

    def test_median_stat(self):
        stat = MedianStat(column="Amount", median=42.0)
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.MEDIAN,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            median=stat,
        )
        assert finding.median is not None
        assert finding.median.median == 42.0
        assert "median" in stat.formula

    def test_percentile_stat(self):
        stat = PercentileStat(column="Amount", p25=1.0, p50=2.0, p75=3.0, p90=4.0, p95=5.0, p99=6.0)
        assert stat.p50 == 2.0

    def test_distinct_count_stat(self):
        stat = DistinctCountStat(column="Region", distinct_count=4)
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.DISTINCT_COUNT,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            distinct_count=stat,
        )
        assert finding.distinct_count is not None
        assert finding.distinct_count.distinct_count == 4

    def test_growth_stat_with_points_and_missing_periods(self):
        points = (
            GrowthPoint(period="2020", value=100.0),
            GrowthPoint(period="2021", value=110.0, change_percent_from_previous=10.0),
        )
        stat = GrowthStat(
            label_column="Year",
            value_column="Sales",
            points=points,
            overall_change_percent=10.0,
            direction="up",
            missing_periods=("2022",),
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.GROWTH,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            growth=stat,
        )
        assert finding.growth is not None
        assert len(finding.growth.points) == 2
        assert finding.growth.missing_periods == ("2022",)

    def test_ranking_stat_with_entries_and_truncation_flag(self):
        entries = (
            RankingEntry(label="Bikes", value=1000.0, rank=1, share_percent=80.0),
            RankingEntry(label="Accessories", value=250.0, rank=2, share_percent=20.0),
        )
        stat = RankingStat(
            label_column="Category", value_column="Revenue", entries=entries, truncated=True
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.RANKING,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            ranking=stat,
        )
        assert finding.ranking is not None
        assert finding.ranking.truncated is True
        assert finding.ranking.entries[0].rank == 1

    def test_outlier_finding_distinct_from_legacy_outlier_stat(self):
        detail = OutlierFinding(
            label="Bikes",
            value=1000.0,
            column="Revenue",
            method="zscore",
            deviations_from_mean=2.5,
            formula="z = (value - mean) / population_stddev",
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.OUTLIER,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            outlier_detail=detail,
        )
        assert finding.outlier_detail is not None
        assert finding.outlier_detail.method == "zscore"
        assert finding.outlier is None  # the legacy field is untouched


class TestAnomalyModels:
    """Prompt 14 (14_ANOMALY_ROOT_CAUSE_CONTRACT.md)."""

    def test_anomaly_method_has_five_values(self):
        assert {m.value for m in AnomalyMethod} == {
            "threshold",
            "percent_change",
            "rolling_zscore",
            "iqr",
            "seasonal",
        }

    def test_anomaly_point_carries_its_signals(self):
        signal = AnomalySignal(
            method=AnomalyMethod.IQR,
            baseline_value=10.0,
            actual_value=100.0,
            deviation=5.0,
            threshold_used=1.5,
            formula="flagged when outside the IQR fence",
        )
        point = AnomalyPoint(period="2023", value=100.0, signals=(signal,))
        assert point.signals[0].method == AnomalyMethod.IQR

    def test_anomaly_finding_kind_on_analytics_finding(self):
        point = AnomalyPoint(
            period="2023",
            value=100.0,
            signals=(
                AnomalySignal(
                    method=AnomalyMethod.THRESHOLD,
                    baseline_value=10.0,
                    actual_value=100.0,
                    deviation=90.0,
                    threshold_used=10.0,
                    formula="x",
                ),
            ),
        )
        finding = AnalyticsFinding(
            kind=AnalyticsFindingKind.ANOMALY,
            claim=ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT),
            anomaly=point,
        )
        assert finding.anomaly is not None
        assert finding.anomaly.period == "2023"

    def test_anomaly_detection_result_defaults(self):
        result = AnomalyDetectionResult()
        assert result.anomalies == ()
        assert result.methods_evaluated == ()
        assert result.insufficient_data_reasons == ()
        assert result.engine_version == ANALYTICS_ENGINE_VERSION


class TestRootCauseModels:
    """Prompt 14 (14_ANOMALY_ROOT_CAUSE_CONTRACT.md)."""

    def test_contributor_stat_fields(self):
        stat = ContributorStat(
            label="West",
            baseline_value=100.0,
            current_value=500.0,
            contribution=400.0,
            contribution_percent=99.3,
            rank=1,
        )
        assert stat.rank == 1

    def test_root_cause_result_insufficient_evidence_defaults(self):
        result = RootCauseResult(has_sufficient_evidence=False)
        assert result.contributors == ()
        assert result.confidence is None
        assert result.formula  # always present, even without evidence
        assert result.engine_version == ANALYTICS_ENGINE_VERSION

    def test_root_cause_result_with_contributors(self):
        contributor = ContributorStat(
            label="West",
            baseline_value=100.0,
            current_value=500.0,
            contribution=400.0,
            contribution_percent=99.3,
            rank=1,
        )
        result = RootCauseResult(
            has_sufficient_evidence=True,
            magnitude=403.0,
            baseline_total=300.0,
            current_total=703.0,
            contributors=(contributor,),
            confidence=0.99,
        )
        assert result.contributors[0].label == "West"
        assert result.confidence == 0.99
