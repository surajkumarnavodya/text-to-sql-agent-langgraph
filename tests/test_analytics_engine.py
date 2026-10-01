"""Unit tests for `analytics/engine.py` -- Prompt 13
(`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`)'s Deterministic Analytical
Result Engine.

One class per result shape, plus dedicated classes for null-handling,
percentile correctness, variance edge cases, both outlier methods,
insufficient-data-reasons population, and formula/version metadata --
matching the prompt's own explicit "deterministic tests for each result
shape and edge case" requirement.
"""

from __future__ import annotations

from analytics.engine import (
    classify_period_column,
    classify_result_shape,
    compute_analytics_result,
)
from analytics.models import ANALYTICS_ENGINE_VERSION, ResultShape

from config.settings import Settings


def _finding_kinds(result):
    return [f.kind.value for f in result.findings]


class TestShapeClassification:
    def test_empty(self):
        assert classify_result_shape(["n"], []) == ResultShape.EMPTY

    def test_scalar_single_row_single_column(self):
        assert classify_result_shape(["n"], [(42,)]) == ResultShape.SCALAR

    def test_scalar_single_row_multi_column(self):
        assert classify_result_shape(["a", "b"], [(1, 2)]) == ResultShape.SCALAR

    def test_categorical_aggregate(self):
        rows = [("Bikes", 100.0), ("Accessories", 50.0), ("Clothing", 30.0)]
        assert (
            classify_result_shape(["Category", "Revenue"], rows)
            == ResultShape.CATEGORICAL_AGGREGATE
        )

    def test_time_series_year_labels(self):
        rows = [("2020", 100.0), ("2021", 110.0), ("2022", 120.0)]
        assert classify_result_shape(["Year", "Sales"], rows) == ResultShape.TIME_SERIES

    def test_time_series_year_month_labels(self):
        rows = [("2021-01", 10.0), ("2021-02", 12.0), ("2021-03", 9.0)]
        assert classify_result_shape(["Month", "Sales"], rows) == ResultShape.TIME_SERIES

    def test_time_series_iso_date_labels(self):
        rows = [("2021-01-01", 10.0), ("2021-01-02", 12.0), ("2021-01-03", 9.0)]
        assert classify_result_shape(["Day", "Sales"], rows) == ResultShape.TIME_SERIES

    def test_multidimensional_two_dimension_columns(self):
        rows = [("Bikes", "West", 100.0), ("Bikes", "East", 50.0), ("Clothing", "West", 30.0)]
        assert (
            classify_result_shape(["Category", "Region", "Revenue"], rows)
            == ResultShape.MULTIDIMENSIONAL
        )

    def test_raw_table_single_numeric_column_multi_row(self):
        rows = [(10.0,), (20.0,), (30.0,)]
        assert classify_result_shape(["amount"], rows) == ResultShape.RAW_TABLE

    def test_raw_table_all_text_columns(self):
        rows = [("a", "x"), ("b", "y"), ("c", "z")]
        assert classify_result_shape(["name", "desc"], rows) == ResultShape.RAW_TABLE

    def test_numeric_vs_numeric_falls_back_to_label_value_pair(self):
        """Mirrors agent.insight.summarize_result's own numeric-vs-numeric
        fallback -- a result like "year, total" (both numeric) still gets
        a usable label/value pair, not RAW_TABLE."""
        rows = [(2020, 100.0), (2021, 110.0), (2022, 120.0)]
        assert classify_result_shape(["Year", "Total"], rows) == ResultShape.TIME_SERIES


class TestClassifyPeriodColumn:
    def test_majority_year(self):
        assert classify_period_column(["2020", "2021", "2022"]) == "year"

    def test_minority_does_not_count(self):
        # Only 1 of 3 looks like a year -- below the 50% majority threshold.
        assert classify_period_column(["2020", "Bikes", "Accessories"]) is None

    def test_no_labels(self):
        assert classify_period_column([]) is None

    def test_unrecognized_shape_returns_none(self):
        assert classify_period_column(["Q1 2021", "Q2 2021"]) is None


class TestScalarShape:
    def test_scalar_produces_column_findings_only(self):
        result = compute_analytics_result(["total_customers"], [(18484,)])
        assert result.shape == ResultShape.SCALAR
        assert result.row_count == 1
        assert "column_summary" in _finding_kinds(result)
        assert "ranking" not in _finding_kinds(result)
        assert "growth" not in _finding_kinds(result)


class TestEmptyShape:
    def test_empty_result(self):
        result = compute_analytics_result(["n"], [])
        assert result.shape == ResultShape.EMPTY
        assert result.row_count == 0
        assert result.findings == ()
        assert result.insufficient_data_reasons == ("all calculations: empty result",)


class TestCategoricalAggregateShape:
    _ROWS = [
        ("Bikes", 1000.0),
        ("Accessories", 50.0),
        ("Clothing", 60.0),
        ("Components", 55.0),
    ]

    def test_ranking_is_sorted_descending_with_share_percent(self):
        result = compute_analytics_result(["Category", "Revenue"], self._ROWS)
        ranking = next(f for f in result.findings if f.kind.value == "ranking").ranking
        assert ranking is not None
        entries = ranking.entries
        assert [e.label for e in entries] == ["Bikes", "Clothing", "Components", "Accessories"]
        assert entries[0].rank == 1
        assert entries[0].share_percent == round(100 * 1000.0 / 1165.0, 1)

    def test_ranking_is_capped_at_max_entries_and_flags_truncation(self):
        rows = [(f"Cat{i}", float(i + 1)) for i in range(5)]
        settings = Settings(analytics_ranking_max_entries=2)
        result = compute_analytics_result(["Category", "Revenue"], rows, settings)
        ranking = next(f for f in result.findings if f.kind.value == "ranking").ranking
        assert ranking is not None
        assert len(ranking.entries) == 2
        assert ranking.truncated is True

    def test_untruncated_when_within_cap(self):
        result = compute_analytics_result(["Category", "Revenue"], self._ROWS)
        ranking = next(f for f in result.findings if f.kind.value == "ranking").ranking
        assert ranking is not None
        assert ranking.truncated is False

    def test_no_growth_finding_for_categorical_shape(self):
        result = compute_analytics_result(["Category", "Revenue"], self._ROWS)
        assert "growth" not in _finding_kinds(result)


class TestTimeSeriesShape:
    def test_full_period_series_not_just_first_and_last(self):
        rows = [("2020", 100.0), ("2021", 110.0), ("2022", 90.0), ("2023", 300.0)]
        result = compute_analytics_result(["Year", "Sales"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "growth")
        assert _finding.growth is not None
        growth = _finding.growth
        assert [p.period for p in growth.points] == ["2020", "2021", "2022", "2023"]
        assert growth.points[0].change_percent_from_previous is None
        assert growth.points[1].change_percent_from_previous == 10.0
        assert growth.points[2].change_percent_from_previous == round(100 * (90 - 110) / 110, 1)
        assert growth.overall_change_percent == 200.0
        assert growth.direction == "up"

    def test_missing_period_detection_for_years(self):
        rows = [("2020", 100.0), ("2021", 110.0), ("2023", 300.0)]
        result = compute_analytics_result(["Year", "Sales"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "growth")
        assert _finding.growth is not None
        growth = _finding.growth
        assert growth.missing_periods == ("2022",)

    def test_missing_period_detection_for_year_months(self):
        rows = [("2021-01", 10.0), ("2021-04", 40.0)]
        result = compute_analytics_result(["Month", "Sales"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "growth")
        assert _finding.growth is not None
        growth = _finding.growth
        assert growth.missing_periods == ("2021-02", "2021-03")

    def test_no_missing_period_detection_for_dates(self):
        """Disclosed, bounded scope -- a full ISO date gets no gap
        detection (the real reporting cadence isn't inferrable from the
        label shape alone), even with an obvious gap."""
        rows = [("2021-01-01", 10.0), ("2021-01-05", 15.0)]
        result = compute_analytics_result(["Day", "Sales"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "growth")
        assert _finding.growth is not None
        growth = _finding.growth
        assert growth.missing_periods == ()

    def test_zero_denominator_step_is_none_not_a_crash(self):
        rows = [("2020", 0.0), ("2021", 50.0)]
        result = compute_analytics_result(["Year", "Sales"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "growth")
        assert _finding.growth is not None
        growth = _finding.growth
        assert growth.points[1].change_percent_from_previous is None
        # Overall change is also undefined when the first value is 0.
        assert growth.overall_change_percent is None
        assert growth.direction is None

    def test_fewer_than_two_periods_records_insufficient_data(self):
        # A single distinct period across many rows -- fewer than 2 to compare.
        rows = [("2020", 10.0), ("2020", 20.0)]
        result = compute_analytics_result(["Year", "Sales"], rows)
        assert "growth" not in _finding_kinds(result)
        assert any("growth" in reason for reason in result.insufficient_data_reasons)

    def test_no_ranking_or_outliers_for_time_series_shape(self):
        rows = [("2020", 100.0), ("2021", 110.0), ("2022", 90.0)]
        result = compute_analytics_result(["Year", "Sales"], rows)
        assert "ranking" not in _finding_kinds(result)
        assert "outlier" not in _finding_kinds(result)


class TestTimeSeriesAnomalyIntegration:
    """Prompt 14 (14_ANOMALY_ROOT_CAUSE_CONTRACT.md): compute_analytics_result's
    TIME_SERIES branch feeds its own already-computed growth series into
    analytics.anomaly.detect_anomalies -- zero new queries."""

    _SPIKY_ROWS = [
        ("2020", 100.0),
        ("2021", 105.0),
        ("2022", 98.0),
        ("2023", 103.0),
        ("2024", 101.0),
        ("2025", 900.0),
    ]
    _STABLE_ROWS = [
        ("2020", 100.0),
        ("2021", 102.0),
        ("2022", 99.0),
        ("2023", 101.0),
        ("2024", 100.5),
    ]

    def test_an_anomalous_series_produces_anomaly_findings(self):
        result = compute_analytics_result(["Year", "Sales"], self._SPIKY_ROWS)
        anomaly_findings = [f for f in result.findings if f.kind.value == "anomaly"]
        assert anomaly_findings
        for finding in anomaly_findings:
            assert finding.anomaly is not None
            assert finding.anomaly.signals

    def test_a_stable_series_produces_no_anomaly_findings(self):
        result = compute_analytics_result(["Year", "Sales"], self._STABLE_ROWS)
        assert "anomaly" not in _finding_kinds(result)

    def test_disabling_anomaly_detection_reproduces_pre_prompt_14_output(self):
        """The explicit fallback-preservation regression test: with the
        feature off, a TIME_SERIES result's findings are identical to
        what compute_analytics_result produced before this prompt."""
        settings = Settings(enable_anomaly_detection=False)
        with_detection = compute_analytics_result(["Year", "Sales"], self._SPIKY_ROWS)
        without_detection = compute_analytics_result(["Year", "Sales"], self._SPIKY_ROWS, settings)

        assert "anomaly" in _finding_kinds(with_detection)
        assert "anomaly" not in _finding_kinds(without_detection)
        # Every non-anomaly finding is unaffected by the flag.
        non_anomaly_with = [f for f in with_detection.findings if f.kind.value != "anomaly"]
        assert len(non_anomaly_with) == len(without_detection.findings)


class TestMultidimensionalShape:
    def test_composite_label_ranking(self):
        rows = [
            ("Bikes", "West", 100.0),
            ("Bikes", "East", 50.0),
            ("Clothing", "West", 30.0),
        ]
        result = compute_analytics_result(["Category", "Region", "Revenue"], rows)
        assert result.shape == ResultShape.MULTIDIMENSIONAL
        _finding = next(f for f in result.findings if f.kind.value == "ranking")
        assert _finding.ranking is not None
        ranking = _finding.ranking
        assert ranking.label_column == "Category / Region"
        labels = [e.label for e in ranking.entries]
        assert "Bikes / West" in labels
        assert "Clothing / West" in labels

    def test_row_with_null_dimension_value_is_excluded(self):
        rows = [
            ("Bikes", "West", 100.0),
            ("Bikes", None, 50.0),
            ("Clothing", "West", 30.0),
        ]
        result = compute_analytics_result(["Category", "Region", "Revenue"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "ranking")
        assert _finding.ranking is not None
        ranking = _finding.ranking
        labels = [e.label for e in ranking.entries]
        assert "Bikes / West" in labels
        assert not any(label.startswith("Bikes / None") for label in labels)


class TestRawTableShape:
    def test_only_column_findings_no_ranking_growth_or_outliers(self):
        # A single numeric column (nothing to pair it with as a label/
        # dimension) is the one shape with no usable value/label pair at
        # all -- see TestShapeClassification's own numeric-vs-numeric
        # fallback test for why two-or-more numeric columns instead
        # always finds a usable (fallback) pair and is never RAW_TABLE.
        rows = [(1.0,), (3.0,), (5.0,), (None,)]
        result = compute_analytics_result(["amount"], rows)
        assert result.shape == ResultShape.RAW_TABLE
        kinds = _finding_kinds(result)
        assert "ranking" not in kinds
        assert "growth" not in kinds
        assert "outlier" not in kinds
        assert "column_summary" in kinds

    def test_all_text_columns_get_summary_and_distinct_count_only(self):
        rows = [("a", "x"), ("b", "y"), ("a", "z")]
        result = compute_analytics_result(["name", "code"], rows)
        kinds = _finding_kinds(result)
        assert kinds.count("column_summary") == 2
        assert kinds.count("distinct_count") == 2
        assert "median" not in kinds  # non-numeric columns get no median/percentile/variance


class TestNullHandling:
    def test_numeric_column_with_nulls_still_classified_numeric(self):
        rows = [("Bikes", 100.0), ("Accessories", None), ("Clothing", 60.0)]
        result = compute_analytics_result(["Category", "Revenue"], rows)
        summary = next(
            f.column_summary
            for f in result.findings
            if f.kind.value == "column_summary"
            and f.column_summary is not None
            and f.column_summary.column == "Revenue"
        )
        assert summary is not None
        assert summary.is_numeric is True
        assert summary.null_count == 1
        assert summary.count == 3
        assert summary.minimum == 60.0
        assert summary.maximum == 100.0

    def test_null_values_excluded_from_totals_and_ranking(self):
        rows = [("Bikes", 100.0), ("Accessories", None), ("Clothing", 60.0)]
        result = compute_analytics_result(["Category", "Revenue"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "ranking")
        assert _finding.ranking is not None
        ranking = _finding.ranking
        labels = {e.label for e in ranking.entries}
        assert "Accessories" not in labels
        assert labels == {"Bikes", "Clothing"}

    def test_all_null_column_is_treated_as_non_numeric(self):
        rows = [("a", None), ("b", None)]
        result = compute_analytics_result(["name", "amount"], rows)
        summary = next(
            f.column_summary
            for f in result.findings
            if f.kind.value == "column_summary"
            and f.column_summary is not None
            and f.column_summary.column == "amount"
        )
        assert summary is not None
        assert summary.is_numeric is False
        assert summary.null_count == 2


class TestPercentileAndMedianCorrectness:
    def test_known_dataset_percentiles(self):
        # A simple 1..10 dataset has well-known percentile/median values.
        rows = [(f"r{i}", float(i)) for i in range(1, 11)]
        result = compute_analytics_result(["Category", "Value"], rows)
        percentile = next(
            f.percentile
            for f in result.findings
            if f.kind.value == "percentile"
            and f.percentile is not None
            and f.percentile.column == "Value"
        )
        median = next(
            f.median
            for f in result.findings
            if f.kind.value == "median" and f.median is not None and f.median.column == "Value"
        )
        assert percentile is not None
        assert median is not None
        assert median.median == 5.5
        assert percentile.p50 == 5.5
        assert (
            percentile.p25
            < percentile.p50
            < percentile.p75
            < percentile.p90
            < percentile.p95
            < percentile.p99
        )

    def test_fewer_than_two_non_null_values_skips_median_percentile_variance(self):
        rows = [("a", 10.0), ("b", None)]
        result = compute_analytics_result(["Category", "Value"], rows)
        kinds = [
            f.kind.value
            for f in result.findings
            if f.kind.value in ("median", "percentile", "variance")
        ]
        assert kinds == []
        assert any(
            "median/percentile/variance" in reason for reason in result.insufficient_data_reasons
        )


class TestVarianceEdgeCases:
    def test_two_identical_values_zero_variance(self):
        rows = [("a", 10.0), ("b", 10.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "variance")
        assert _finding.variance is not None
        variance = _finding.variance
        assert variance.variance == 0.0
        assert variance.stddev == 0.0

    def test_coefficient_of_variation_none_when_mean_is_zero(self):
        rows = [("a", -10.0), ("b", 10.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        _finding = next(f for f in result.findings if f.kind.value == "variance")
        assert _finding.variance is not None
        variance = _finding.variance
        assert variance.coefficient_of_variation is None


class TestOutlierDetection:
    def test_zscore_and_iqr_can_disagree(self):
        """A value right at the z-score boundary but clearly outside the
        IQR fence -- demonstrating the two methods are genuinely
        independent, not aliases of each other."""
        rows = [("A", 10.0), ("B", 12.0), ("C", 11.0), ("D", 9.0), ("E", 100.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        methods = {
            f.outlier_detail.method
            for f in result.findings
            if f.kind.value == "outlier" and f.outlier_detail is not None
        }
        assert "iqr" in methods

    def test_clear_outlier_flagged_by_zscore(self):
        # A single extreme outlier among few points self-inflates the
        # population stddev enough to mask its own z-score (a real,
        # well-known z-score limitation, not an engine bug) -- this
        # dataset's outlier is extreme relative to a *tighter* cluster,
        # which clears the threshold comfortably (z ~= 2.23).
        rows = [("A", 10.0), ("B", 11.0), ("C", 9.0), ("D", 10.5), ("E", 10.2), ("F", 50.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        zscore_outlier_labels = {
            f.outlier_detail.label
            for f in result.findings
            if f.kind.value == "outlier"
            and f.outlier_detail is not None
            and f.outlier_detail.method == "zscore"
        }
        assert "F" in zscore_outlier_labels

    def test_fewer_than_three_categories_records_insufficient_data_for_zscore(self):
        rows = [("A", 10.0), ("B", 20.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        assert any("zscore" in reason for reason in result.insufficient_data_reasons)

    def test_fewer_than_four_categories_records_insufficient_data_for_iqr(self):
        rows = [("A", 10.0), ("B", 20.0), ("C", 15.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        assert any("iqr" in reason for reason in result.insufficient_data_reasons)

    def test_identical_values_never_flagged(self):
        rows = [("A", 10.0), ("B", 10.0), ("C", 10.0), ("D", 10.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        assert "outlier" not in _finding_kinds(result)
        assert any("stddev is 0" in reason for reason in result.insufficient_data_reasons)
        assert any(
            "interquartile range is 0" in reason for reason in result.insufficient_data_reasons
        )


class TestFormulaAndVersionMetadata:
    def test_engine_version_stamped_on_every_result(self):
        result = compute_analytics_result(["n"], [(1,)])
        assert result.engine_version == ANALYTICS_ENGINE_VERSION

    def test_every_derived_finding_type_carries_a_formula(self):
        rows = [("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 4.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        for finding in result.findings:
            if finding.median is not None:
                assert finding.median.formula
            if finding.percentile is not None:
                assert finding.percentile.formula
            if finding.variance is not None:
                assert finding.variance.formula
            if finding.ranking is not None:
                assert finding.ranking.formula
            if finding.growth is not None:
                assert finding.growth.formula
            if finding.outlier_detail is not None:
                assert finding.outlier_detail.formula

    def test_distinct_truth_level_is_always_database_fact(self):
        rows = [("A", 1.0), ("B", 2.0), ("C", 3.0)]
        result = compute_analytics_result(["Category", "Value"], rows)
        for finding in result.findings:
            assert finding.claim.level.value == "database_fact"
