"""Structured analytical-plan contract -- Prompt 12
(`12_ANALYTICAL_PLANNING_CONTRACT.md`): a typed, resolvable-to-real-schema
representation of what the eventual SQL must compute, sitting between
`agent.intent`'s *classification* (what kind of question is this) and
`generate_sql`'s free-form SQL text.

`agent.nodes.plan_query_node`'s existing `query_plan: list[str]` is
free-text, only ever judged by a *second LLM call*
(`review_sql_against_plan_from_llm`) -- there is no deterministic
verification that a plan's own metric/dimension/filter/time references
are real, authorized, joinable, or supported by the target database. This
module is that structure's typed contract only (no LLM call lives here --
`agent.llm_client.generate_analytical_plan_from_llm` makes the call,
`agent.plan_validator.validate_plan` deterministically re-verifies it,
`agent.nodes.build_analytical_plan_node` wires both into the graph),
mirroring `agent/intent.py`'s identical "typed contract module, separate
from the LLM-calling module and the validation module" split.

**Deliberately additive to, never a replacement for, the existing
free-text `query_plan`/`review_sql_node` pair.** See
`agent.nodes.build_analytical_plan_node`'s own docstring for the full
three-tier fallback (validated structured plan -> free-text plan -> direct
generation) this module is the first tier of.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from agent.provenance import DataTruthLevel


class PlanAggregation(str, Enum):
    """How a metric's column is aggregated -- `NONE` for a plain lookup
    (no aggregation at all, e.g. a single row's raw value)."""

    SUM = "sum"
    AVG = "avg"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    MIN = "min"
    MAX = "max"
    NONE = "none"


class PlanSortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class PlanMetric(BaseModel):
    """One quantity the plan computes.

    `table`/`column` are `None` only when `governed_metric_key` is set --
    a metric that resolves entirely to a published, `CONFIRMED_BUSINESS_
    TRUTH` governed metric definition (`AgentState["governing_metrics"]`)
    carries its own approved expression already; this plan only needs to
    name *which* governed metric it means, not re-derive its table/column
    shape (see `agent.plan_validator.validate_plan`'s own handling of this
    case). A metric with neither a `governed_metric_key` nor a `column` is
    a malformed plan element -- the validator rejects it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    table: str | None = None
    column: str | None = None
    aggregation: PlanAggregation = PlanAggregation.NONE
    governed_metric_key: str | None = None


class PlanDimension(BaseModel):
    """One column the result is grouped/broken down by."""

    model_config = ConfigDict(frozen=True)

    name: str
    table: str
    column: str


class PlanFilter(BaseModel):
    """One filter condition -- `value` is free text (DATA, never an
    instruction) rendered into the generation prompt for the model to
    translate into a literal, not interpolated into any SQL directly by
    this codebase."""

    model_config = ConfigDict(frozen=True)

    table: str
    column: str
    operator: str
    value: str


class PlanTimeRange(BaseModel):
    """The time window/column the question is scoped to."""

    model_config = ConfigDict(frozen=True)

    table: str
    column: str
    description: str


class PlanComparison(BaseModel):
    """What's being compared, for a COMPARISON-shaped question (e.g.
    period-over-period, segment-vs-segment)."""

    model_config = ConfigDict(frozen=True)

    kind: str
    description: str


class PlanRanking(BaseModel):
    """A top/bottom-N ranking, optionally partitioned per group -- the
    exact "top N per group needs a window function, not TOP+GROUP BY"
    shape `agent.llm_client._QUERY_PATTERNS_BLOCK` already warns about."""

    model_config = ConfigDict(frozen=True)

    order_by: str
    direction: PlanSortDirection = PlanSortDirection.DESC
    top_n: int | None = None
    per_group: tuple[str, ...] = ()


class PlanSort(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    direction: PlanSortDirection = PlanSortDirection.DESC


class AnalyticalPlan(BaseModel):
    """One question's structured analytical plan -- always `AI_INFERENCE`
    (see `truth_level`) until `agent.plan_validator.validate_plan` has
    deterministically confirmed every reference it makes; even then, the
    plan's *existence* is never promoted to `CONFIRMED_BUSINESS_TRUTH` --
    only a `governed_metric_key`'d metric ever carries that status, and
    only because it points at an already-published catalog entry, not
    because of anything this plan itself asserts.

    Every field defaults to empty/`None` ("not needed for this question")
    -- a plan for a simple KPI lookup may populate only `metrics`, while a
    TREND/COMPARISON/RANKING-shaped plan populates most of them.

    Attributes:
        metrics: The quantities computed (at least one, for a plan to be
            meaningful -- `agent.plan_validator.validate_plan` rejects an
            empty-metrics plan).
        dimensions: Group-by/breakdown columns.
        filters: Filter conditions.
        time_range: The time window/column, or `None` if the question has
            no time scoping.
        grain: The time granularity implied (e.g. `"day"`, `"month"`,
            `"year"`), or `None`.
        comparison: What's being compared, for a COMPARISON-shaped
            question, or `None`.
        ranking: A top/bottom-N ranking, or `None`.
        sort: Explicit sort order(s), beyond whatever `ranking` already
            implies.
        limit: An explicit row limit the question asks for, or `None`.
        required_operations: Free-form capability tags this plan needs
            the target database to support (e.g. `"window_function"`,
            `"percentile_cont"`) -- only a small, known subset is ever
            checked by `agent.plan_validator.validate_plan`; an
            unrecognized tag is never itself a validation failure (fail
            open on ignorance, fail closed only on a *known*
            incompatibility -- see that function's own docstring).
        truth_level: Always `AI_INFERENCE` -- see `agent.provenance
            .DataTruthLevel`'s own docstring.
    """

    model_config = ConfigDict(frozen=True)

    metrics: tuple[PlanMetric, ...] = ()
    dimensions: tuple[PlanDimension, ...] = ()
    filters: tuple[PlanFilter, ...] = ()
    time_range: PlanTimeRange | None = None
    grain: str | None = None
    comparison: PlanComparison | None = None
    ranking: PlanRanking | None = None
    sort: tuple[PlanSort, ...] = ()
    limit: int | None = None
    required_operations: tuple[str, ...] = ()
    truth_level: DataTruthLevel = DataTruthLevel.AI_INFERENCE


def render_plan_as_steps(plan: AnalyticalPlan) -> list[str]:
    """Renders a structured `AnalyticalPlan` as the same ordered list of
    plain-English step strings `agent.nodes.plan_query_node`'s free-text
    planner already produces (`AgentState["query_plan"]`) -- reused
    verbatim by `review_sql_node`'s existing plan-conformance LLM check
    (`review_sql_against_plan_from_llm`), so a validated structured plan
    gets the identical SQL-vs-plan review the free-text planner's own
    output already gets, without a second review node (master-contract
    rule 3: never duplicate LangGraph functionality).
    """
    steps: list[str] = []
    for metric in plan.metrics:
        if metric.governed_metric_key:
            steps.append(
                f"Compute metric '{metric.name}' using the governed definition for "
                f"'{metric.governed_metric_key}'"
            )
        elif metric.aggregation == PlanAggregation.NONE:
            # Bandit's SQL-construction heuristic (B608) false-positives here
            # purely because of the words "Select"/"from" nearby -- this
            # builds a plain-English plan-review step string (never executed
            # as SQL), the identical false positive agent.llm_client
            # ._system_prompt's own nosec comment already documents.
            steps.append(
                f"Select '{metric.name}' from {metric.table}.{metric.column} (no aggregation)"  # nosec B608
            )
        else:
            steps.append(
                f"Compute '{metric.name}' as {metric.aggregation.value.upper()}"
                f"({metric.table}.{metric.column})"
            )
    if plan.dimensions:
        dims = ", ".join(f"{d.table}.{d.column}" for d in plan.dimensions)
        steps.append(f"Group/break down by: {dims}")
    for filt in plan.filters:
        steps.append(f"Filter {filt.table}.{filt.column} {filt.operator} {filt.value!r}")
    if plan.time_range:
        tr = plan.time_range
        steps.append(f"Scope to time range on {tr.table}.{tr.column}: {tr.description}")
    if plan.grain:
        steps.append(f"Use time grain: {plan.grain}")
    if plan.comparison:
        steps.append(f"Comparison ({plan.comparison.kind}): {plan.comparison.description}")
    if plan.ranking:
        rank = plan.ranking
        per_group = f" per group ({', '.join(rank.per_group)})" if rank.per_group else ""
        top_n = f"top {rank.top_n} " if rank.top_n else ""
        steps.append(
            f"Rank {top_n}by {rank.order_by} {rank.direction.value}{per_group} -- use a window "
            "function (ROW_NUMBER/RANK), never plain TOP/LIMIT combined with GROUP BY, if "
            "partitioned per group"
        )
    for sort in plan.sort:
        steps.append(f"Sort by {sort.field} {sort.direction.value}")
    if plan.limit is not None:
        steps.append(f"Limit the result to {plan.limit} row(s)")
    return steps
