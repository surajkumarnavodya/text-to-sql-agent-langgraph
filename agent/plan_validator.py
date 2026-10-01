"""Deterministically re-verifies a structured `AnalyticalPlan` -- Prompt 12
(`12_ANALYTICAL_PLANNING_CONTRACT.md`).

Mirrors `agent/sql_validator.py`'s own posture toward generated SQL: an
LLM's structured output is never trusted at face value just because it
parsed cleanly. `agent.llm_client.generate_analytical_plan_from_llm`
produces an `AnalyticalPlan` that *claims* to reference real tables/
columns, respect sensitive-column policy, and be joinable/supported by
the target database -- this module is the deterministic code that
actually checks each of those claims against the real, already-retrieved
schema, before the plan is ever shown to `generate_sql`.

**Fail-closed on a genuine violation, fail-open on the plan's own
existence.** A plan with any violation is never partially trusted --
`validate_plan` returns every violation it finds (not just the first),
and `agent.nodes.build_analytical_plan_node` discards the whole plan
(falls back to the existing free-text `plan_query_node`/direct-generation
path) the moment `is_valid` is `False`. This is *not* a security bypass:
the real, fail-closed security gate for generated SQL
(`agent.sql_validator.validate_sql`'s safety-violation check,
`agent.nodes.validate_sql_node`'s restricted-column gate) remains
completely unmodified and runs regardless of whether a plan was ever
built at all -- this module is strictly an earlier, additional layer,
never a replacement (master-contract rule 5: never allow an LLM to
bypass deterministic security controls).
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from agent.analytical_plan import AnalyticalPlan
from agent.authz import Permission, has_role_permission
from config.sensitive_columns import load_sensitive_columns
from db.relationship_inference import type_family
from db.schema_introspection import extract_ddl_column_names, extract_ddl_foreign_key_targets

if TYPE_CHECKING:
    from agent.state import TableSchema

# Small, honestly-scoped per-`DB_TYPE` support table for the handful of
# `AnalyticalPlan.required_operations` tags this validator actually knows
# how to check -- **not** an attempt to catalog every SQL feature. A tag
# not listed here is never blocked (fail open on ignorance); a tag listed
# here with `False` for this `db_type` is a *known* incompatibility (fail
# closed). See each tag's own comment for the real-world reason.
_OPERATION_SUPPORT: dict[str, frozenset[str]] = {
    # ROW_NUMBER/RANK/LAG/LEAD -- standard ANSI SQL, all four engines.
    "window_function": frozenset({"mssql", "postgresql", "mysql", "oracle"}),
    "lag_lead": frozenset({"mssql", "postgresql", "mysql", "oracle"}),
    # WITH ... AS (...) -- all four engines support CTEs (MySQL since 8.0).
    "cte": frozenset({"mssql", "postgresql", "mysql", "oracle"}),
    # PERCENTILE_CONT(...) WITHIN GROUP (...) -- MySQL has no equivalent
    # window/aggregate function at all (verified against Prompt 05's own
    # T-SQL dialect review; MySQL only has PERCENT_RANK/CUME_DIST, a
    # different calculation).
    "percentile_cont": frozenset({"mssql", "postgresql", "oracle"}),
    # STRING_AGG(...) under that exact name -- MySQL's equivalent is
    # GROUP_CONCAT(...) and Oracle's is LISTAGG(...), different function
    # names this app's generation prompt would need to pick per-dialect;
    # flagged unsupported under this generic tag rather than silently
    # assuming the model knows to substitute the right one.
    "string_agg": frozenset({"mssql", "postgresql"}),
    "rollup": frozenset({"mssql", "postgresql", "mysql", "oracle"}),
}


class PlanViolation(BaseModel):
    """One deterministic reason a plan was rejected."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class PlanValidationResult(BaseModel):
    """The outcome of `validate_plan` -- `violations` is always complete
    (every check runs; a plan isn't short-circuited on its first
    violation), so a caller logging/surfacing the result sees the whole
    picture, not just whichever check happened to run first."""

    model_config = ConfigDict(frozen=True)

    is_valid: bool
    violations: tuple[PlanViolation, ...] = ()


def _column_type_family(ddl: str, column_name: str) -> str | None:
    """Looks up `column_name`'s rendered type token in `ddl` and classifies
    it via `db.relationship_inference.type_family` (reused, not
    duplicated) -- `None` if the column isn't found at all (existence is
    checked separately; this is purely a type lookup)."""
    for line in ddl.splitlines():
        stripped = line.strip().rstrip(",")
        tokens = stripped.split()
        if len(tokens) < 2 or tokens[0] != column_name:
            continue
        return type_family(tokens[1])
    return None


def _table_columns(schema_tables: list[TableSchema]) -> dict[str, set[str]]:
    """Table name -> the set of column names its own DDL declares."""
    return {
        table["table_name"]: set(extract_ddl_column_names(table["ddl"])) for table in schema_tables
    }


def _fk_adjacency(schema_tables: list[TableSchema]) -> dict[str, set[str]]:
    """Builds a table-name adjacency map from FK edges declared in the
    already-retrieved `schema_tables`' own DDL text -- a disclosed, bounded
    scope: only FK edges visible in the DDL already shown to the LLM for
    this question are considered (see `db.schema_introspection
    .extract_ddl_foreign_key_targets`'s own docstring), never a full
    cross-schema graph search and never an inferred (non-FK) candidate
    relationship. Undirected -- a path is usable regardless of which side
    declared the FK.
    """
    known_tables = {table["table_name"] for table in schema_tables}
    adjacency: dict[str, set[str]] = {name: set() for name in known_tables}
    for table in schema_tables:
        source = table["table_name"]
        for target in extract_ddl_foreign_key_targets(table["ddl"]):
            if target in known_tables:
                adjacency[source].add(target)
                adjacency[target].add(source)
    return adjacency


def _is_connected(start: str, end: str, adjacency: dict[str, set[str]]) -> bool:
    if start == end:
        return True
    visited = {start}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor == end:
                return True
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return False


def _referenced_table_columns(plan: AnalyticalPlan) -> list[tuple[str, str]]:
    """Every (table, column) pair the plan directly names -- metrics
    (excluding a `governed_metric_key`'d one, which has no table/column of
    its own to check), dimensions, filters, and the time range."""
    pairs: list[tuple[str, str]] = []
    for metric in plan.metrics:
        if metric.governed_metric_key:
            continue
        if metric.table and metric.column:
            pairs.append((metric.table, metric.column))
    for dimension in plan.dimensions:
        pairs.append((dimension.table, dimension.column))
    for filt in plan.filters:
        pairs.append((filt.table, filt.column))
    if plan.time_range:
        pairs.append((plan.time_range.table, plan.time_range.column))
    return pairs


def validate_plan(
    plan: AnalyticalPlan,
    schema_tables: list[TableSchema],
    governing_metrics: list[dict] | None,
    caller_roles: tuple[str, ...],
    db_type: str,
) -> PlanValidationResult:
    """Deterministically re-verifies every claim `plan` makes.

    Args:
        plan: The LLM-proposed structured plan (see `agent.analytical_plan
            .AnalyticalPlan`) -- untrusted input, exactly like generated
            SQL text is to `agent.sql_validator.validate_sql`.
        schema_tables: `AgentState["schema_tables"]` -- the already-
            retrieved top-k relevant tables for this question (the same
            set `generate_sql`'s own prompt is scoped to). A plan
            referencing a table/column outside this set is rejected, the
            same "only the tables shown to you exist" boundary the
            generation system prompt already enforces on the model.
        governing_metrics: `AgentState["governing_metrics"]` -- published,
            `CONFIRMED_BUSINESS_TRUTH` metric definitions for this
            question (see `retrieval.retriever.extract_governing_metrics`).
            A metric's `governed_metric_key` must match one of these
            entries' `business_name` to be trusted without its own
            table/column.
        caller_roles: `AgentState["caller_roles"]` -- checked against
            `config.sensitive_columns`' "restricted" classification via
            `agent.authz.has_role_permission`, the identical check
            `agent.nodes.validate_sql_node` already applies to generated
            SQL (this is an earlier, additional layer, not a replacement).
        db_type: The target database's `DB_TYPE` (`AgentState
            ["selected_database"]`'s own `db_type`, resolved by the
            caller) -- checked against `_OPERATION_SUPPORT` for each tag
            in `plan.required_operations`.

    Returns:
        A `PlanValidationResult` listing every violation found (empty,
        `is_valid=True`, if none).
    """
    violations: list[PlanViolation] = []
    table_columns = _table_columns(schema_tables)
    governing_names = {
        metric.get("business_name")
        for metric in (governing_metrics or [])
        if metric.get("business_name")
    }
    caller_can_view_restricted = has_role_permission(
        caller_roles, Permission.VIEW_RESTRICTED_COLUMNS
    )
    restricted_pairs = {
        pair for pair, tier in load_sensitive_columns().items() if tier == "restricted"
    }

    if not plan.metrics:
        violations.append(
            PlanViolation(code="no_metrics", message="A plan with no metrics computes nothing.")
        )

    for metric in plan.metrics:
        if metric.governed_metric_key:
            if metric.governed_metric_key not in governing_names:
                violations.append(
                    PlanViolation(
                        code="unknown_governed_metric",
                        message=(
                            f"Metric '{metric.name}' claims governed metric "
                            f"'{metric.governed_metric_key}', which is not among this "
                            "question's published governing metrics."
                        ),
                    )
                )
            continue
        if not metric.table or not metric.column:
            violations.append(
                PlanViolation(
                    code="incomplete_metric",
                    message=(
                        f"Metric '{metric.name}' has no table/column and no "
                        "governed_metric_key -- nothing to compute it from."
                    ),
                )
            )

    # Existence: every (table, column) pair the plan names must be a real
    # table in schema_tables and a real column of that table's own DDL.
    for table_name, column_name in _referenced_table_columns(plan):
        if table_name not in table_columns:
            violations.append(
                PlanViolation(
                    code="unknown_table",
                    message=f"Table '{table_name}' is not among the tables retrieved for this question.",
                )
            )
            continue
        if column_name not in table_columns[table_name]:
            violations.append(
                PlanViolation(
                    code="unknown_column",
                    message=f"Column '{table_name}.{column_name}' does not exist on that table.",
                )
            )

    # Relationship paths: every distinct table the plan touches must be
    # reachable from every other via the FK graph declared in the
    # already-retrieved schema DDL (see `_fk_adjacency`'s own docstring for
    # the disclosed scope boundary).
    distinct_tables = sorted(
        {
            table_name
            for table_name, _ in _referenced_table_columns(plan)
            if table_name in table_columns
        }
    )
    if len(distinct_tables) > 1:
        adjacency = _fk_adjacency(schema_tables)
        anchor = distinct_tables[0]
        for other in distinct_tables[1:]:
            if not _is_connected(anchor, other, adjacency):
                violations.append(
                    PlanViolation(
                        code="no_relationship_path",
                        message=(
                            f"No declared foreign-key path connects '{anchor}' and '{other}' "
                            "within the tables retrieved for this question."
                        ),
                    )
                )

    # Filter/sensitive-field policy: any referenced column classified
    # "restricted" is rejected unless the caller holds
    # VIEW_RESTRICTED_COLUMNS -- the same gate validate_sql_node already
    # applies post-generation, applied here as an earlier layer.
    if restricted_pairs and not caller_can_view_restricted:
        for table_name, column_name in _referenced_table_columns(plan):
            if (table_name, column_name) in restricted_pairs:
                violations.append(
                    PlanViolation(
                        code="restricted_column",
                        message=(
                            f"Column '{table_name}.{column_name}' is classified 'restricted' "
                            "and this caller may not view it."
                        ),
                    )
                )

    # Time feasibility: the time-range column, if the plan has one and it
    # exists, must actually be a date/time-typed column.
    if plan.time_range is not None:
        table_name, column_name = plan.time_range.table, plan.time_range.column
        ddl = next((t["ddl"] for t in schema_tables if t["table_name"] == table_name), None)
        if ddl is not None and column_name in table_columns.get(table_name, set()):
            family = _column_type_family(ddl, column_name)
            if family != "datetime":
                violations.append(
                    PlanViolation(
                        code="infeasible_time_range",
                        message=(
                            f"Time range column '{table_name}.{column_name}' is not a "
                            "date/time-typed column."
                        ),
                    )
                )

    # Database capabilities: a *known* incompatibility between a tag the
    # plan requires and the target db_type is a hard violation; an
    # unrecognized tag is silently ignored (fail open on ignorance).
    for operation in plan.required_operations:
        supported_for = _OPERATION_SUPPORT.get(operation)
        if supported_for is not None and db_type not in supported_for:
            violations.append(
                PlanViolation(
                    code="unsupported_operation",
                    message=f"'{operation}' is not supported for DB_TYPE={db_type!r}.",
                )
            )

    return PlanValidationResult(is_valid=not violations, violations=tuple(violations))
