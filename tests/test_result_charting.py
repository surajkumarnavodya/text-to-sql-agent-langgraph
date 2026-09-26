"""Unit tests for agent/result_charting.py -- column-type classification and
the starting-point chart recommendation. Actual chart validity/rendering is
a frontend concern (frontend/src/lib/chartEngine.ts); this module only ever
emits metadata, never a chart, so these tests stay narrow to that."""

from __future__ import annotations

import pandas as pd

from agent.result_charting import classify_columns, recommend_chart


class TestClassifyColumns:
    def test_numeric_column(self):
        df = pd.DataFrame({"revenue": [100, 200, 300]})
        assert classify_columns(df) == {"revenue": "numeric"}

    def test_date_like_column_by_name(self):
        df = pd.DataFrame({"order_date": ["not-a-real-date-but-named-date"]})
        assert classify_columns(df)["order_date"] == "date"

    def test_date_like_column_by_value(self):
        df = pd.DataFrame({"period": ["2024-01-01", "2024-02-01", "2024-03-01"]})
        assert classify_columns(df)["period"] == "date"

    def test_plain_text_column(self):
        df = pd.DataFrame({"region": ["East", "West", "North"]})
        assert classify_columns(df) == {"region": "text"}

    def test_mixed_columns(self):
        df = pd.DataFrame({"region": ["East", "West"], "revenue": [100, 200]})
        assert classify_columns(df) == {"region": "text", "revenue": "numeric"}


class TestRecommendChart:
    def test_empty_dataframe_has_no_recommendation(self):
        df = pd.DataFrame()
        assert recommend_chart(df, {}) is None

    def test_category_plus_numeric_recommends_bar(self):
        df = pd.DataFrame({"region": ["East", "West"], "revenue": [100, 200]})
        types = classify_columns(df)
        result = recommend_chart(df, types)
        assert result["chart_type"] == "bar"
        assert result["x_column"] == "region"
        assert result["y_column"] == "revenue"
        assert result["reason"]

    def test_date_plus_numeric_recommends_line(self):
        df = pd.DataFrame({"order_date": ["2024-01-01", "2024-02-01"], "total": [10, 20]})
        types = classify_columns(df)
        result = recommend_chart(df, types)
        assert result["chart_type"] == "line"
        assert result["x_column"] == "order_date"

    def test_single_row_single_numeric_recommends_kpi(self):
        df = pd.DataFrame({"total_revenue": [12345]})
        types = classify_columns(df)
        result = recommend_chart(df, types)
        assert result["chart_type"] == "kpi"
        assert result["y_column"] == "total_revenue"
        assert result["x_column"] is None

    def test_two_numeric_columns_recommends_scatter(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        types = classify_columns(df)
        result = recommend_chart(df, types)
        assert result["chart_type"] == "scatter"

    def test_all_text_has_no_recommendation(self):
        df = pd.DataFrame({"name": ["Alice", "Bob"]})
        types = classify_columns(df)
        assert recommend_chart(df, types) is None

    def test_no_numeric_column_at_all_has_no_recommendation(self):
        df = pd.DataFrame({"region": ["East"], "note": ["hello"]})
        types = classify_columns(df)
        assert recommend_chart(df, types) is None

    def test_multi_row_with_label_and_numeric_prefers_bar_over_kpi(self):
        """A single numeric column with >1 row (and a label column) is a
        comparison, not a single aggregate -- must not be mistaken for the
        one-row KPI case."""
        df = pd.DataFrame({"region": ["East", "West", "North"], "revenue": [1, 2, 3]})
        types = classify_columns(df)
        result = recommend_chart(df, types)
        assert result["chart_type"] == "bar"
