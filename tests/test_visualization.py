"""Unit tests for `analytics/visualization.py` -- Prompt 15
(`15_VISUALIZATION_ENGINE_CONTRACT.md`)'s deterministic visualization
specification builder.

One class per result shape (kpi/line/bar/scatter/histogram), plus
dedicated classes for geography detection (map on/off), box-plot gating,
empty/no-shape results, downsampling, title/format/aggregation inference,
accessibility-text reuse, and engine-version stamping -- matching the
prompt's own explicit testing requirements.
"""

from __future__ import annotations

from analytics.engine import compute_analytics_result
from analytics.visualization import (
    VISUALIZATION_ENGINE_VERSION,
    ChartType,
    FieldRole,
    build_chart_spec,
    detect_geography_columns,
)

from config.settings import Settings


def _column_types(
    columns: list[str], numeric: set[str] = frozenset(), date: set[str] = frozenset()
):
    types = {}
    for c in columns:
        if c in numeric:
            types[c] = "numeric"
        elif c in date:
            types[c] = "date"
        else:
            types[c] = "text"
    return types


class TestEmptyAndNoShape:
    def test_no_rows_returns_none(self):
        spec = build_chart_spec(["a", "b"], [], {"a": "text", "b": "numeric"})
        assert spec is None

    def test_no_columns_returns_none(self):
        spec = build_chart_spec([], [], {})
        assert spec is None

    def test_no_numeric_column_returns_none(self):
        columns = ["Category", "Region"]
        rows = [("Bikes", "West"), ("Clothing", "East")]
        spec = build_chart_spec(columns, rows, _column_types(columns))
        assert spec is None

    def test_three_numeric_raw_table_returns_none(self):
        columns = ["a", "b", "c"]
        rows = [(1, 2, 3), (4, 5, 6)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"a", "b", "c"}))
        assert spec is None

    def test_single_numeric_too_few_rows_returns_none(self):
        columns = ["Value"]
        rows = [(1.0,), (2.0,), (3.0,)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"Value"}))
        assert spec is None


class TestKpiShape:
    def test_single_row_single_numeric_is_kpi(self):
        columns = ["TotalRevenue"]
        rows = [(1000.0,)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalRevenue"}))
        assert spec is not None
        assert spec.chart_type == ChartType.KPI
        assert spec.fields[0].role == FieldRole.Y
        assert spec.title == "Total Revenue"


class TestLineShape:
    def test_date_plus_numeric_is_line(self):
        columns = ["OrderYear", "TotalSales"]
        rows = [("2021", 100.0), ("2022", 120.0), ("2023", 90.0)]
        spec = build_chart_spec(
            columns, rows, _column_types(columns, numeric={"TotalSales"}, date={"OrderYear"})
        )
        assert spec is not None
        assert spec.chart_type == ChartType.LINE
        assert spec.sort is not None
        assert spec.sort.column == "OrderYear"
        assert spec.sort.direction == "asc"
        assert "by Order Year" in spec.title


class TestBarShape:
    def test_text_plus_numeric_is_bar(self):
        columns = ["Category", "TotalSales"]
        rows = [("Bikes", 100.0), ("Accessories", 50.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalSales"}))
        assert spec is not None
        assert spec.chart_type == ChartType.BAR
        assert spec.sort is not None
        assert spec.sort.direction == "desc"
        assert spec.title == "Total Sales by Category"


class TestScatterShape:
    def test_two_numeric_is_scatter(self):
        columns = ["Quantity", "Revenue"]
        rows = [(1.0, 10.0), (2.0, 25.0), (3.0, 40.0)]
        spec = build_chart_spec(
            columns, rows, _column_types(columns, numeric={"Quantity", "Revenue"})
        )
        assert spec is not None
        assert spec.chart_type == ChartType.SCATTER
        assert spec.title == "Revenue vs Quantity"


class TestHistogramShape:
    def test_single_numeric_enough_rows_is_histogram(self):
        columns = ["OrderValue"]
        rows = [(v,) for v in [10.0, 20.0, 15.0, 30.0, 25.0, 18.0]]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"OrderValue"}))
        assert spec is not None
        assert spec.chart_type == ChartType.HISTOGRAM
        assert spec.title == "Distribution of Order Value"
        assert spec.fields[0].role == FieldRole.Y

    def test_histogram_falls_back_to_box_when_enabled(self):
        columns = ["OrderValue"]
        rows = [(v,) for v in [10.0, 20.0, 15.0, 30.0, 25.0, 18.0]]
        settings = Settings(enable_box_plot_charts=True)
        spec = build_chart_spec(
            columns, rows, _column_types(columns, numeric={"OrderValue"}), settings=settings
        )
        assert spec is not None
        assert spec.chart_type == ChartType.BOX


class TestGeographyDetection:
    def test_detects_region_latitude_longitude(self):
        roles = detect_geography_columns(
            ["SalesTerritoryRegion", "StoreLatitude", "StoreLongitude", "Revenue"]
        )
        assert roles["SalesTerritoryRegion"] == FieldRole.GEO_REGION
        assert roles["StoreLatitude"] == FieldRole.GEO_LATITUDE
        assert roles["StoreLongitude"] == FieldRole.GEO_LONGITUDE
        assert "Revenue" not in roles

    def test_unmatched_name_is_not_tagged(self):
        roles = detect_geography_columns(["ProductName", "UnitPrice"])
        assert roles == {}

    def test_geo_region_falls_back_to_bar_when_map_charts_disabled(self):
        columns = ["SalesTerritoryRegion", "TotalSales"]
        rows = [("North America", 100.0), ("Europe", 50.0)]
        settings = Settings(enable_map_charts=False)
        spec = build_chart_spec(
            columns, rows, _column_types(columns, numeric={"TotalSales"}), settings=settings
        )
        assert spec is not None
        assert spec.chart_type == ChartType.BAR

    def test_geo_region_becomes_map_when_map_charts_enabled(self):
        columns = ["SalesTerritoryRegion", "TotalSales"]
        rows = [("North America", 100.0), ("Europe", 50.0)]
        settings = Settings(enable_map_charts=True)
        spec = build_chart_spec(
            columns, rows, _column_types(columns, numeric={"TotalSales"}), settings=settings
        )
        assert spec is not None
        assert spec.chart_type == ChartType.MAP

    def test_lat_lon_pair_becomes_map_when_enabled(self):
        columns = ["StoreLatitude", "StoreLongitude"]
        rows = [(47.6, -122.3), (40.7, -74.0)]
        settings = Settings(enable_map_charts=True)
        spec = build_chart_spec(
            columns,
            rows,
            _column_types(columns, numeric={"StoreLatitude", "StoreLongitude"}),
            settings=settings,
        )
        assert spec is not None
        assert spec.chart_type == ChartType.MAP

    def test_lat_lon_pair_falls_back_to_scatter_when_disabled(self):
        columns = ["StoreLatitude", "StoreLongitude"]
        rows = [(47.6, -122.3), (40.7, -74.0)]
        settings = Settings(enable_map_charts=False)
        spec = build_chart_spec(
            columns,
            rows,
            _column_types(columns, numeric={"StoreLatitude", "StoreLongitude"}),
            settings=settings,
        )
        assert spec is not None
        assert spec.chart_type == ChartType.SCATTER


class TestDownsampling:
    def test_large_bar_result_is_downsampled_with_notice(self):
        columns = ["Category", "TotalSales"]
        rows = [(f"Cat{i}", float(i)) for i in range(50)]
        settings = Settings(visualization_default_top_n=20)
        spec = build_chart_spec(
            columns, rows, _column_types(columns, numeric={"TotalSales"}), settings=settings
        )
        assert spec is not None
        assert spec.is_downsampled is True
        assert spec.limit == 20
        assert any("top 20 of 50" in n for n in spec.notices)

    def test_small_bar_result_is_not_downsampled(self):
        columns = ["Category", "TotalSales"]
        rows = [("Bikes", 100.0), ("Accessories", 50.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalSales"}))
        assert spec is not None
        assert spec.is_downsampled is False
        assert spec.notices == ()


class TestFormatAndAggregationInference:
    def test_currency_format_from_name(self):
        columns = ["Category", "TotalRevenue"]
        rows = [("Bikes", 100.0), ("Accessories", 50.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalRevenue"}))
        assert spec is not None
        y_field = next(f for f in spec.fields if f.role == FieldRole.Y)
        assert y_field.format == "currency"
        assert y_field.aggregation == "sum"

    def test_percent_format_from_name(self):
        columns = ["Category", "GrowthRate"]
        rows = [("Bikes", 10.5), ("Accessories", -3.2)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"GrowthRate"}))
        assert spec is not None
        y_field = next(f for f in spec.fields if f.role == FieldRole.Y)
        assert y_field.format == "percent"

    def test_count_aggregation_has_no_prefix_collision(self):
        columns = ["Category", "OrderCount"]
        rows = [("Bikes", 10.0), ("Accessories", 5.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"OrderCount"}))
        assert spec is not None
        y_field = next(f for f in spec.fields if f.role == FieldRole.Y)
        assert y_field.aggregation == "count"
        # "Order Count by Category" -- no double "Count Count".
        assert "Count Count" not in spec.title

    def test_title_does_not_double_prefix_already_stated_aggregation(self):
        columns = ["Category", "TotalSales"]
        rows = [("Bikes", 100.0), ("Accessories", 50.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalSales"}))
        assert spec is not None
        assert "Total Total" not in spec.title
        assert spec.title == "Total Sales by Category"


class TestAccessibility:
    def test_falls_back_to_generic_description_without_analytics_result(self):
        columns = ["Category", "TotalSales"]
        rows = [("Bikes", 100.0), ("Accessories", 50.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalSales"}))
        assert spec is not None
        assert spec.accessibility.alt_text
        assert spec.accessibility.summary
        assert "2 data point" in spec.accessibility.alt_text

    def test_reuses_real_analytics_result_ranking_claim(self):
        columns = ["Category", "Revenue"]
        rows = [("Bikes", 1000.0), ("Accessories", 150.0), ("Clothing", 50.0)]
        column_types = _column_types(columns, numeric={"Revenue"})
        analytics_result = compute_analytics_result(columns, rows)
        spec = build_chart_spec(columns, rows, column_types, analytics_result=analytics_result)
        assert spec is not None
        ranking_finding = next(f for f in analytics_result.findings if f.kind.value == "ranking")
        assert ranking_finding.claim.value in spec.accessibility.alt_text
        assert spec.accessibility.summary == ranking_finding.claim.value

    def test_accessibility_never_empty(self):
        columns = ["Value"]
        rows = [(1.0,)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"Value"}))
        assert spec is not None
        assert len(spec.accessibility.alt_text) > 0
        assert len(spec.accessibility.summary) > 0


class TestEngineVersion:
    def test_every_spec_is_stamped_with_current_version(self):
        columns = ["Category", "TotalSales"]
        rows = [("Bikes", 100.0), ("Accessories", 50.0)]
        spec = build_chart_spec(columns, rows, _column_types(columns, numeric={"TotalSales"}))
        assert spec is not None
        assert spec.engine_version == VISUALIZATION_ENGINE_VERSION
