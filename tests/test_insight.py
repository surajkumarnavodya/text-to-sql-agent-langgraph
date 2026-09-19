"""Unit tests for the insight summarization/grounding logic (agent/insight.py).

All pure functions, no LLM/DB mocking required -- these exercise
summarize_result's aggregate math directly and is_insight_grounded's
number-extraction/matching against hand-built ResultSummary instances.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import pstdev as _pstdev

from agent.insight import (
    ResultSummary,
    extract_numbers,
    is_insight_grounded,
    should_skip_insight,
    summarize_result,
)


class TestShouldSkipInsight:
    def test_empty_result_is_skipped(self):
        assert should_skip_insight(["CustomerCount"], []) is True

    def test_single_row_single_column_is_skipped(self):
        assert should_skip_insight(["CustomerCount"], [(1231,)]) is True

    def test_single_row_multi_column_is_not_skipped(self):
        assert should_skip_insight(["Year", "Total"], [(2013, 100.0)]) is False

    def test_multi_row_single_column_is_not_skipped(self):
        assert should_skip_insight(["Name"], [("a",), ("b",)]) is False


class TestSummarizeResult:
    def test_numeric_column_gets_min_max_total(self):
        summary = summarize_result(
            ["Region", "TotalSales"],
            [("Australia", 2300000.0), ("Southwest", 1100000.0)],
        )
        sales_stat = next(s for s in summary.column_stats if s.name == "TotalSales")
        assert sales_stat.is_numeric is True
        assert sales_stat.minimum == 1100000.0
        assert sales_stat.maximum == 2300000.0
        assert sales_stat.total == 3400000.0

    def test_non_numeric_column_gets_distinct_count_not_min_max(self):
        summary = summarize_result(
            ["Region", "TotalSales"], [("Australia", 1.0), ("Australia", 2.0)]
        )
        region_stat = next(s for s in summary.column_stats if s.name == "Region")
        assert region_stat.is_numeric is False
        assert region_stat.distinct_count == 1
        assert region_stat.minimum is None

    def test_label_plus_value_pairing_computes_top_and_share(self):
        summary = summarize_result(
            ["Region", "TotalSales"],
            [("Australia", 2300000.0), ("Southwest", 1100000.0), ("Canada", 600000.0)],
        )
        assert summary.top_label == "Australia"
        assert summary.top_value == 2300000.0
        assert summary.top_share_percent == round(100 * 2300000.0 / 4000000.0, 1)

    def test_decimal_values_are_handled(self):
        """Real SQL Server/Postgres drivers return Decimal for money columns."""
        summary = summarize_result(["Region", "TotalSales"], [("Australia", Decimal("2300000.00"))])
        sales_stat = next(s for s in summary.column_stats if s.name == "TotalSales")
        assert sales_stat.total == 2300000.0

    def test_numeric_vs_numeric_picks_last_column_as_value(self):
        """'SELECT year, SUM(amount)' -- the dimension (year) is numeric too,
        so the *last* numeric column must be treated as the metric, not the
        first, or "top year" claims get attributed to the wrong column."""
        summary = summarize_result(
            ["CalendarYear", "TotalSales"],
            [(2012, 9000000.0), (2013, 16000000.0)],
        )
        assert summary.top_value_column == "TotalSales"
        assert summary.top_label_column == "CalendarYear"
        assert summary.top_label == "2013"
        assert summary.top_value == 16000000.0

    def test_no_numeric_column_produces_no_top_pairing(self):
        summary = summarize_result(["Name", "Category"], [("a", "x"), ("b", "y")])
        assert summary.top_label is None
        assert summary.top_value is None
        assert summary.top_share_percent is None

    def test_row_count_matches_input(self):
        summary = summarize_result(["x"], [(1,), (2,), (3,)])
        assert summary.row_count == 3


class TestVarianceStats:
    """The "AI Data Analyst depth" pass -- deterministic stddev/coefficient
    of variation alongside the existing min/max/total, computed the same
    Python-does-the-arithmetic way as top_share_percent."""

    def test_numeric_column_gets_a_population_stddev(self):
        summary = summarize_result(["Region", "Sales"], [("A", 10.0), ("B", 20.0), ("C", 30.0)])
        sales_stat = next(s for s in summary.column_stats if s.name == "Sales")
        assert sales_stat.stddev == round(_pstdev([10.0, 20.0, 30.0]), 4)

    def test_coefficient_of_variation_is_stddev_over_mean(self):
        summary = summarize_result(["Region", "Sales"], [("A", 10.0), ("B", 20.0), ("C", 30.0)])
        sales_stat = next(s for s in summary.column_stats if s.name == "Sales")
        expected = round(_pstdev([10.0, 20.0, 30.0]) / 20.0, 4)
        assert sales_stat.coefficient_of_variation == expected

    def test_single_row_has_no_stddev_rather_than_a_misleading_zero(self):
        summary = summarize_result(["Region", "Sales"], [("A", 10.0)])
        sales_stat = next(s for s in summary.column_stats if s.name == "Sales")
        assert sales_stat.stddev is None
        assert sales_stat.coefficient_of_variation is None

    def test_zero_mean_gives_no_coefficient_of_variation(self):
        summary = summarize_result(["Region", "Delta"], [("A", -5.0), ("B", 5.0)])
        delta_stat = next(s for s in summary.column_stats if s.name == "Delta")
        assert delta_stat.stddev is not None
        assert delta_stat.coefficient_of_variation is None


class TestTrend:
    def test_two_periods_computes_change_percent_and_direction(self):
        summary = summarize_result(["Year", "Revenue"], [(2023, 100.0), (2024, 150.0)])
        assert summary.trend is not None
        assert summary.trend.first_period == "2023"
        assert summary.trend.last_period == "2024"
        assert summary.trend.change_percent == 50.0
        assert summary.trend.direction == "up"

    def test_a_decline_is_direction_down_with_a_negative_change_percent(self):
        summary = summarize_result(["Year", "Revenue"], [(2023, 200.0), (2024, 100.0)])
        assert summary.trend is not None
        assert summary.trend.change_percent == -50.0
        assert summary.trend.direction == "down"

    def test_no_change_is_direction_flat(self):
        summary = summarize_result(["Year", "Revenue"], [(2023, 100.0), (2024, 100.0)])
        assert summary.trend is not None
        assert summary.trend.direction == "flat"
        assert summary.trend.change_percent == 0.0

    def test_uses_first_and_last_period_across_more_than_two_rows(self):
        summary = summarize_result(
            ["Year", "Revenue"], [(2021, 100.0), (2022, 120.0), (2023, 140.0)]
        )
        assert summary.trend is not None
        assert summary.trend.first_period == "2021"
        assert summary.trend.last_period == "2023"
        assert summary.trend.change_percent == 40.0

    def test_a_single_row_has_no_trend_nothing_to_compare(self):
        summary = summarize_result(["Year", "Revenue"], [(2023, 100.0)])
        assert summary.trend is None

    def test_a_zero_first_value_has_no_trend_percent_change_is_undefined(self):
        summary = summarize_result(["Year", "Revenue"], [(2023, 0.0), (2024, 100.0)])
        assert summary.trend is None

    def test_trend_values_are_reflected_in_allowed_values_and_percents(self):
        """The grounding gate (`is_insight_grounded`) must be able to
        verify a claim about the trend, not just the pre-existing
        top-label/share numbers."""
        summary = summarize_result(["Year", "Revenue"], [(2023, 100.0), (2024, 150.0)])
        assert 100.0 in summary.allowed_values()
        assert 150.0 in summary.allowed_values()
        assert 50.0 in summary.allowed_percents()


class TestOutliers:
    def test_flags_a_value_far_from_the_mean(self):
        # 5 tightly-clustered values plus one far outlier -- with only a
        # handful of points, population stddev is sensitive to how many
        # "normal" points anchor the mean, so this uses enough of them
        # that the one real outlier clears the 2-stddev threshold without
        # dragging the mean/stddev up to swallow its own signal.
        summary = summarize_result(
            ["Region", "Sales"],
            [
                ("A", 100.0),
                ("B", 100.0),
                ("C", 100.0),
                ("D", 100.0),
                ("E", 100.0),
                ("F", 1000.0),
            ],
        )
        outlier_labels = {o.label for o in summary.outliers}
        assert outlier_labels == {"F"}

    def test_no_outliers_when_values_are_close_together(self):
        summary = summarize_result(
            ["Region", "Sales"], [("A", 100.0), ("B", 102.0), ("C", 99.0), ("D", 101.0)]
        )
        assert summary.outliers == ()

    def test_fewer_than_three_labels_never_reports_outliers(self):
        """Two points are always symmetric around their own mean --
        nothing meaningful to flag."""
        summary = summarize_result(["Region", "Sales"], [("A", 1.0), ("B", 1000.0)])
        assert summary.outliers == ()

    def test_identical_values_have_zero_stddev_and_no_outliers(self):
        summary = summarize_result(["Region", "Sales"], [("A", 50.0), ("B", 50.0), ("C", 50.0)])
        assert summary.outliers == ()

    def test_outlier_values_are_groundable(self):
        summary = summarize_result(
            ["Region", "Sales"],
            [
                ("A", 100.0),
                ("B", 100.0),
                ("C", 100.0),
                ("D", 100.0),
                ("E", 100.0),
                ("F", 1000.0),
            ],
        )
        assert len(summary.outliers) == 1
        assert summary.outliers[0].value in summary.allowed_values()


class TestExtractNumbers:
    def test_plain_integer(self):
        assert extract_numbers("42 customers") == [(42.0, False)]

    def test_comma_grouped_number_not_split(self):
        """Regression: a naive comma-group regex can truncate-match a plain
        ungrouped number's first few digits and leave the rest as a bogus
        second match."""
        assert extract_numbers("2,300,000.00") == [(2300000.0, False)]

    def test_plain_ungrouped_large_number_not_split(self):
        assert extract_numbers("2300000.0") == [(2300000.0, False)]

    def test_dollar_prefix_stripped(self):
        assert extract_numbers("$1,359,861.90") == [(1359861.90, False)]

    def test_percent_flagged(self):
        assert extract_numbers("57.5%") == [(57.5, True)]

    def test_k_m_b_suffixes_expanded(self):
        assert extract_numbers("2.3M") == [(2300000.0, False)]
        assert extract_numbers("42K") == [(42000.0, False)]
        assert extract_numbers("1.5B") == [(1500000000.0, False)]

    def test_multiple_numbers_in_one_sentence(self):
        result = extract_numbers("Sales ranged from $100 to $200, a 50% increase.")
        assert (100.0, False) in result
        assert (200.0, False) in result
        assert (50.0, True) in result


class TestIsInsightGrounded:
    def _summary(self) -> ResultSummary:
        return summarize_result(
            ["Region", "TotalSales"],
            [("Australia", 2300000.0), ("Southwest", 1100000.0), ("Canada", 600000.0)],
        )

    def test_grounded_sentence_passes(self):
        summary = self._summary()
        text = "Australia had the highest sales at 2300000.0, about 57.5% of the total."
        assert is_insight_grounded(text, summary) is True

    def test_fabricated_percent_fails(self):
        summary = self._summary()
        text = "Australia led with sales up 42% year-over-year."
        assert is_insight_grounded(text, summary) is False

    def test_fabricated_value_fails(self):
        summary = self._summary()
        text = "Total sales across all regions reached 9999999."
        assert is_insight_grounded(text, summary) is False

    def test_row_count_is_a_grounded_number(self):
        summary = self._summary()
        text = "There are 3 regions shown."
        assert is_insight_grounded(text, summary) is True

    def test_number_absent_from_question_and_sql_is_ungrounded(self):
        summary = summarize_result(["TotalSales"], [(500000.0,), (500000.0,)])
        text = "In 2013, total sales were 500000.0."
        assert is_insight_grounded(text, summary) is False

    def test_literal_value_from_question_is_allowed(self):
        """A filter value the user themselves typed (e.g. a year) is not a
        fabricated claim even though it isn't part of the result data."""
        summary = summarize_result(["TotalSales"], [(500000.0,), (500000.0,)])
        text = "In 2013, total sales were 500000.0."
        assert is_insight_grounded(text, summary, question="What were sales in 2013?") is True

    def test_literal_value_from_sql_is_allowed(self):
        summary = summarize_result(["TotalSales"], [(500000.0,)])
        text = "For 2013, total sales were 500000.0."
        sql = "SELECT SUM(SalesAmount) AS TotalSales FROM Fact WHERE Year = 2013"
        assert is_insight_grounded(text, summary, sql=sql) is True

    def test_small_rounding_difference_is_tolerated(self):
        summary = self._summary()
        text = "Sales totaled about 2300000.4."  # off by 0.4, within tolerance
        assert is_insight_grounded(text, summary) is True

    def test_empty_text_is_trivially_grounded(self):
        assert is_insight_grounded("", self._summary()) is True
