"""Unit tests for `agent/analytical_plan.py` -- Prompt 12
(`12_ANALYTICAL_PLANNING_CONTRACT.md`)'s typed contract module.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.analytical_plan import (
    AnalyticalPlan,
    PlanAggregation,
    PlanComparison,
    PlanDimension,
    PlanFilter,
    PlanMetric,
    PlanRanking,
    PlanSort,
    PlanSortDirection,
    PlanTimeRange,
    render_plan_as_steps,
)
from agent.provenance import DataTruthLevel


class TestAnalyticalPlanDefaults:
    def test_empty_plan_has_all_empty_defaults(self):
        plan = AnalyticalPlan()
        assert plan.metrics == ()
        assert plan.dimensions == ()
        assert plan.filters == ()
        assert plan.time_range is None
        assert plan.grain is None
        assert plan.comparison is None
        assert plan.ranking is None
        assert plan.sort == ()
        assert plan.limit is None
        assert plan.required_operations == ()
        assert plan.truth_level == DataTruthLevel.AI_INFERENCE

    def test_plan_is_frozen(self):
        plan = AnalyticalPlan()
        with pytest.raises(ValidationError):
            plan.grain = "month"

    def test_metric_defaults_aggregation_to_none(self):
        metric = PlanMetric(name="revenue", table="Sales", column="Amount")
        assert metric.aggregation == PlanAggregation.NONE
        assert metric.governed_metric_key is None

    def test_metric_may_carry_only_a_governed_key(self):
        metric = PlanMetric(name="revenue", governed_metric_key="Revenue")
        assert metric.table is None
        assert metric.column is None

    def test_ranking_defaults_direction_desc_and_empty_per_group(self):
        ranking = PlanRanking(order_by="revenue")
        assert ranking.direction == PlanSortDirection.DESC
        assert ranking.top_n is None
        assert ranking.per_group == ()

    def test_unrecognized_aggregation_value_is_rejected(self):
        with pytest.raises(ValidationError):
            PlanMetric(name="x", table="T", column="C", aggregation="median")  # type: ignore[arg-type]

    def test_full_plan_round_trips_through_model_dump_and_validate(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="region", table="Customer", column="Region"),),
            filters=(PlanFilter(table="Sales", column="Status", operator="=", value="Completed"),),
            time_range=PlanTimeRange(
                table="Sales", column="OrderDate", description="last 6 months"
            ),
            grain="month",
            comparison=PlanComparison(kind="period_over_period", description="this month vs last"),
            ranking=PlanRanking(order_by="revenue", top_n=3, per_group=("Region",)),
            sort=(PlanSort(field="revenue", direction=PlanSortDirection.DESC),),
            limit=10,
            required_operations=("window_function",),
        )
        dumped = plan.model_dump(mode="json")
        rebuilt = AnalyticalPlan.model_validate(dumped)
        assert rebuilt == plan


class TestRenderPlanAsSteps:
    def test_empty_plan_renders_no_steps(self):
        assert render_plan_as_steps(AnalyticalPlan()) == []

    def test_metric_only_plan(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            )
        )
        steps = render_plan_as_steps(plan)
        assert len(steps) == 1
        assert "SUM" in steps[0]
        assert "Sales.Amount" in steps[0]

    def test_governed_metric_step_names_the_governed_key_not_a_column(self):
        plan = AnalyticalPlan(metrics=(PlanMetric(name="revenue", governed_metric_key="Revenue"),))
        steps = render_plan_as_steps(plan)
        assert len(steps) == 1
        assert "governed definition" in steps[0]
        assert "Revenue" in steps[0]

    def test_lookup_metric_with_no_aggregation(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="name", table="Customer", column="Name", aggregation=PlanAggregation.NONE
                ),
            )
        )
        steps = render_plan_as_steps(plan)
        assert "no aggregation" in steps[0]

    def test_full_plan_renders_one_step_per_populated_field_group(self):
        plan = AnalyticalPlan(
            metrics=(
                PlanMetric(
                    name="revenue", table="Sales", column="Amount", aggregation=PlanAggregation.SUM
                ),
            ),
            dimensions=(PlanDimension(name="region", table="Customer", column="Region"),),
            filters=(PlanFilter(table="Sales", column="Status", operator="=", value="Completed"),),
            time_range=PlanTimeRange(
                table="Sales", column="OrderDate", description="last 6 months"
            ),
            grain="month",
            comparison=PlanComparison(kind="period_over_period", description="this month vs last"),
            ranking=PlanRanking(order_by="revenue", top_n=3, per_group=("Region",)),
            sort=(PlanSort(field="revenue", direction=PlanSortDirection.DESC),),
            limit=10,
        )
        steps = render_plan_as_steps(plan)
        joined = "\n".join(steps)
        assert "SUM(Sales.Amount)" in joined
        assert "Customer.Region" in joined
        assert "Filter Sales.Status" in joined
        assert "last 6 months" in joined
        assert "month" in joined
        assert "period_over_period" in joined
        assert "top 3" in joined and "per group" in joined
        assert "Sort by revenue desc" in joined
        assert "Limit the result to 10 row(s)" in joined
