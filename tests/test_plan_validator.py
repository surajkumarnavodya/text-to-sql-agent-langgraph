"""Unit tests for `agent/plan_validator.py` -- Prompt 12
(`12_ANALYTICAL_PLANNING_CONTRACT.md`)'s deterministic re-verification of
an LLM-proposed `AnalyticalPlan`.

`schema_tables` fixtures below use `db.schema_introspection.render_ddl`'s
exact text shape directly (hand-authored, not built via a live engine) --
the same convention `tests/test_schema_retriever.py` etc. already use for
DDL-text-shaped fixtures.
"""

from __future__ import annotations

from agent.analytical_plan import (
    AnalyticalPlan,
    PlanAggregation,
    PlanDimension,
    PlanFilter,
    PlanMetric,
    PlanTimeRange,
)
from agent.plan_validator import validate_plan
from agent.state import TableSchema

_SALES_DDL = (
    "CREATE TABLE Sales (\n"
    "    SalesId INT PRIMARY KEY,\n"
    "    Amount DECIMAL NOT NULL,\n"
    "    Status NVARCHAR(20) NOT NULL,\n"
    "    OrderDate DATETIME NOT NULL,\n"
    "    CustomerKey INT NOT NULL,\n"
    "    FOREIGN KEY (CustomerKey) REFERENCES Customer (CustomerKey)\n"
    ");"
)
_CUSTOMER_DDL = (
    "CREATE TABLE Customer (\n"
    "    CustomerKey INT PRIMARY KEY,\n"
    "    Region NVARCHAR(50) NOT NULL,\n"
    "    Ssn NVARCHAR(11) NOT NULL\n"
    ");"
)
# No FK edge connects this to Sales/Customer at all -- deliberately an
# isolated island for the no-relationship-path test.
_PRODUCT_DDL = (
    "CREATE TABLE Product (\n    ProductId INT PRIMARY KEY,\n    Name NVARCHAR(100) NOT NULL\n);"
)
# Bridges Sales <-> Region via two separate FKs -- a real intermediate hop.
_REGION_DDL = (
    "CREATE TABLE Region (\n    RegionId INT PRIMARY KEY,\n    Name NVARCHAR(50) NOT NULL\n);"
)
_SALES_WITH_REGION_DDL = (
    "CREATE TABLE Sales (\n"
    "    SalesId INT PRIMARY KEY,\n"
    "    Amount DECIMAL NOT NULL,\n"
    "    RegionId INT NOT NULL,\n"
    "    FOREIGN KEY (RegionId) REFERENCES Region (RegionId)\n"
    ");"
)


def _schema_tables() -> list[TableSchema]:
    return [
        {"table_name": "Sales", "ddl": _SALES_DDL, "similarity_score": 1.0},
        {"table_name": "Customer", "ddl": _CUSTOMER_DDL, "similarity_score": 1.0},
        {"table_name": "Product", "ddl": _PRODUCT_DDL, "similarity_score": 0.5},
    ]


class TestExistence:
    def test_valid_plan_passes(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="region", table="Customer", column="Region"),),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert result.is_valid
        assert result.violations == ()

    def test_unknown_table_is_rejected(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="x", table="NotATable", column="Amount", aggregation=PlanAggregation.SUM
                ),
            )
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "unknown_table" for v in result.violations)

    def test_unknown_column_is_rejected(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="x", table="Sales", column="NotAColumn", aggregation=PlanAggregation.SUM
                ),
            )
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "unknown_column" for v in result.violations)

    def test_no_metrics_is_rejected(self):
        result = validate_plan(AnalyticalPlan(), _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "no_metrics" for v in result.violations)

    def test_metric_with_neither_column_nor_governed_key_is_rejected(self):
        plan = AnalyticalPlan(metrics=(PlanMetric(name="mystery"),))
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "incomplete_metric" for v in result.violations)

    def test_governed_metric_key_not_in_governing_metrics_is_rejected(self):
        plan = AnalyticalPlan(metrics=(PlanMetric(name="revenue", governed_metric_key="Revenue"),))
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "unknown_governed_metric" for v in result.violations)

    def test_governed_metric_key_matching_a_governing_metric_passes_without_table_column(self):
        plan = AnalyticalPlan(metrics=(PlanMetric(name="revenue", governed_metric_key="Revenue"),))
        governing_metrics = [
            {"business_name": "Revenue", "approved_expression": "SUM(Sales.Amount)"}
        ]
        result = validate_plan(plan, _schema_tables(), governing_metrics, (), "mssql")
        assert result.is_valid


class TestRelationshipPaths:
    def test_single_table_plan_needs_no_path_check(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="x", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            )
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert result.is_valid

    def test_directly_fk_connected_tables_pass(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="region", table="Customer", column="Region"),),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert result.is_valid

    def test_no_declared_fk_path_is_rejected(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="product", table="Product", column="Name"),),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "no_relationship_path" for v in result.violations)

    def test_path_through_an_intermediate_fk_table_passes(self):
        schema_tables: list[TableSchema] = [
            {"table_name": "Sales", "ddl": _SALES_WITH_REGION_DDL, "similarity_score": 1.0},
            {"table_name": "Region", "ddl": _REGION_DDL, "similarity_score": 1.0},
        ]
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="region", table="Region", column="Name"),),
        )
        result = validate_plan(plan, schema_tables, [], (), "mssql")
        assert result.is_valid


class TestSensitiveFieldPolicy:
    def test_restricted_column_without_permission_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "agent.plan_validator.load_sensitive_columns",
            lambda: {("Customer", "Ssn"): "restricted"},
        )
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            filters=(
                PlanFilter(table="Customer", column="Ssn", operator="=", value="123-45-6789"),
            ),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "restricted_column" for v in result.violations)

    def test_restricted_column_with_view_permission_passes(self, monkeypatch):
        monkeypatch.setattr(
            "agent.plan_validator.load_sensitive_columns",
            lambda: {("Customer", "Ssn"): "restricted"},
        )
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            filters=(
                PlanFilter(table="Customer", column="Ssn", operator="=", value="123-45-6789"),
            ),
        )
        result = validate_plan(plan, _schema_tables(), [], ("analyst",), "mssql")
        assert result.is_valid


class TestTimeFeasibility:
    def test_datetime_column_passes(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            time_range=PlanTimeRange(
                table="Sales", column="OrderDate", description="last 6 months"
            ),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert result.is_valid

    def test_non_datetime_column_is_rejected(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            time_range=PlanTimeRange(table="Sales", column="Status", description="active orders"),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mssql")
        assert not result.is_valid
        assert any(v.code == "infeasible_time_range" for v in result.violations)


class TestDatabaseCapabilities:
    def test_percentile_cont_on_mysql_is_rejected(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            required_operations=("percentile_cont",),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mysql")
        assert not result.is_valid
        assert any(v.code == "unsupported_operation" for v in result.violations)

    def test_percentile_cont_on_postgresql_passes(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            required_operations=("percentile_cont",),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "postgresql")
        assert result.is_valid

    def test_unrecognized_operation_tag_never_blocks(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            required_operations=("some_future_feature_this_validator_has_never_heard_of",),
        )
        result = validate_plan(plan, _schema_tables(), [], (), "mysql")
        assert result.is_valid


class TestQuestionShapeCoverage:
    """One fully-valid plan per analytical-question shape named in Prompt
    12's own testing requirements (trends, comparisons, rankings,
    segmentation, KPI lookups)."""

    def test_trend_shaped_plan(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            time_range=PlanTimeRange(
                table="Sales", column="OrderDate", description="last 12 months"
            ),
            grain="month",
        )
        assert validate_plan(plan, _schema_tables(), [], (), "mssql").is_valid

    def test_comparison_shaped_plan(self):
        from agent.analytical_plan import PlanComparison

        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            comparison=PlanComparison(
                kind="period_over_period", description="this quarter vs last"
            ),
            required_operations=("lag_lead", "cte"),
        )
        assert validate_plan(plan, _schema_tables(), [], (), "mssql").is_valid

    def test_ranking_shaped_plan(self):
        from agent.analytical_plan import PlanRanking

        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="region", table="Customer", column="Region"),),
            ranking=PlanRanking(order_by="revenue", top_n=3, per_group=("region",)),
            required_operations=("window_function",),
        )
        assert validate_plan(plan, _schema_tables(), [], (), "mssql").is_valid

    def test_segmentation_shaped_plan(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(
                PlanDimension(name="region", table="Customer", column="Region"),
                PlanDimension(name="status", table="Sales", column="Status"),
            ),
        )
        assert validate_plan(plan, _schema_tables(), [], (), "mssql").is_valid

    def test_kpi_lookup_shaped_plan(self):
        governing_metrics = [
            {"business_name": "Revenue", "approved_expression": "SUM(Sales.Amount)"}
        ]
        plan = AnalyticalPlan(metrics=(PlanMetric(name="Revenue", governed_metric_key="Revenue"),))
        assert validate_plan(plan, _schema_tables(), governing_metrics, (), "mssql").is_valid
