"""Proactive query cost estimation via each engine's non-executing plan mechanism.

Catches an expensive query *before* running it -- an earlier, additional
layer in front of the existing timeout-based protection
(`db.execution._execute_with_timeout`), not a replacement for it. The
timeout still catches anything this layer misses (a plan that
under-estimates, a dialect this module doesn't support, ...); this layer
exists purely to catch the common, avoidable case (a missing filter, an
accidental cross join) before spending any real execution time on it at
all.

Per-DB_TYPE strategy:
  - **postgresql**: `EXPLAIN (FORMAT JSON) <query>` -- a plain, side-effect-
    free SELECT-like call; the query is never actually run.
  - **mysql**: `EXPLAIN FORMAT=JSON <query>` -- same idea, deeper JSON
    nesting (walked recursively for the largest scan estimate anywhere in
    the plan).
  - **mssql**: `SET SHOWPLAN_XML ON` -- MSSQL has no single-statement
    EXPLAIN; toggling this session option makes the *next* statement return
    its estimated plan as XML instead of executing, and it must be toggled
    back off afterward. See `_run_mssql_showplan`'s docstring for the
    connection-pooling safety detail this requires.
  - **oracle**: `EXPLAIN PLAN FOR <query>` populates `PLAN_TABLE`, then a
    follow-up query reads the root row back out. Requires INSERT privilege
    on `PLAN_TABLE`, which a strictly read-only role may not have --
    expected to fail open for some read-only setups; that's fine, it's the
    same fail-open path as any other estimation failure.

Every one of these fails open: `estimate_query_cost()` returns None (not an
exception) on any error, timeout, disabled setting, or unsupported
`DB_TYPE`, and the caller (`agent.nodes.estimate_query_cost_node`) treats
None exactly like "proceed normally" -- a bug or an unusual environment
here must never be the reason a legitimate query can't run.

Severity is classified on **estimated row count alone**, not cost: cost
units aren't comparable across these four engines in any principled way,
while an estimated row count is a reasonably portable signal everywhere.
Cost is still captured and logged for visibility, just never used in the
threshold decision.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, text

from config.settings import Settings

logger = logging.getLogger(__name__)

Severity = Literal["low", "moderate", "high"]

_MSSQL_SHOWPLAN_NS = {"sp": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}

# User-facing text for a "moderate" query -- shown before/during execution,
# never a reason to block it. Kept short; the point is just "don't worry,
# this is expected to take a bit."
MODERATE_COST_NOTICE = "This query scans a large amount of data and may take a moment to run."


@dataclass(frozen=True)
class CostEstimate:
    """A non-executing plan estimate for one candidate query.

    Attributes:
        estimated_rows: Estimated row count the plan's root/largest scan
            would produce, or None if the engine's plan didn't yield one.
        estimated_cost: Engine-native cost units (never compared across
            engines) -- captured for logging/visibility only.
        severity: "low" (proceed silently), "moderate" (proceed, show a
            notice), or "high" (don't run -- feed back as a retryable
            error). Classified from `estimated_rows` alone; see module
            docstring.
        plan_summary: Short human-readable description of the plan's
            top/largest operation (e.g. "Clustered Index Scan on
            FactInternetSales"), for logging and the moderate-cost notice.
    """

    estimated_rows: float | None
    estimated_cost: float | None
    severity: Severity
    plan_summary: str


def high_cost_error_message(estimate: CostEstimate) -> str:
    """User-facing text for a "high" severity estimate -- fed back as a
    retryable error, same wording style as the existing timeout message."""
    rows_text = (
        f"~{int(estimate.estimated_rows):,} rows"
        if estimate.estimated_rows
        else "a very large amount of data"
    )
    return (
        f"This query would scan an unusually large amount of data ({rows_text}, "
        f"estimated) -- try adding a filter or narrowing the date range."
    )


@dataclass(frozen=True)
class _RawPlanInfo:
    """Engine-native numbers pulled out of one plan, before severity classification."""

    estimated_rows: float | None
    estimated_cost: float | None
    plan_summary: str


def _classify_severity(rows: float | None, settings: Settings) -> Severity:
    if rows is None:
        return "low"  # unknown -- fail toward "don't block" (see module docstring)
    if rows >= settings.cost_high_row_threshold:
        return "high"
    if rows >= settings.cost_moderate_row_threshold:
        return "moderate"
    return "low"


def _run_postgresql_explain(engine: Engine, sql: str) -> _RawPlanInfo | None:
    with engine.connect() as connection:
        row = connection.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).fetchone()
    if row is None:
        return None
    raw = row[0]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    plan = (parsed[0] if parsed else {}).get("Plan", {})
    return _RawPlanInfo(
        estimated_rows=float(plan["Plan Rows"]) if "Plan Rows" in plan else None,
        estimated_cost=float(plan["Total Cost"]) if "Total Cost" in plan else None,
        plan_summary=str(plan.get("Node Type", "unknown")),
    )


def _max_mysql_scan_rows(node: object) -> float | None:
    """Recursively finds the largest row-estimate anywhere in a MySQL JSON plan.

    MySQL's `EXPLAIN FORMAT=JSON` nests table/join info arbitrarily deeply
    (nested_loop, materialized subqueries, ...) rather than exposing one
    top-level row count the way Postgres/MSSQL do -- the largest individual
    scan anywhere in the tree is a reasonable, simple proxy for "how much
    data could this touch," matching this module's goal (catch runaway
    scans) without needing a fully faithful cost model.
    """
    best: float | None = None
    if isinstance(node, dict):
        for key in ("rows_examined_per_scan", "rows_produced_per_join"):
            value = node.get(key)
            if isinstance(value, int | float):
                best = value if best is None else max(best, float(value))
        for value in node.values():
            candidate = _max_mysql_scan_rows(value)
            if candidate is not None:
                best = candidate if best is None else max(best, candidate)
    elif isinstance(node, list):
        for item in node:
            candidate = _max_mysql_scan_rows(item)
            if candidate is not None:
                best = candidate if best is None else max(best, candidate)
    return best


def _run_mysql_explain(engine: Engine, sql: str) -> _RawPlanInfo | None:
    with engine.connect() as connection:
        row = connection.execute(text(f"EXPLAIN FORMAT=JSON {sql}")).fetchone()
    if row is None:
        return None
    raw = row[0]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    query_block = parsed.get("query_block", {})
    cost_info = query_block.get("cost_info", {})
    cost_raw = cost_info.get("query_cost")
    return _RawPlanInfo(
        estimated_rows=_max_mysql_scan_rows(query_block),
        estimated_cost=float(cost_raw) if cost_raw is not None else None,
        plan_summary="MySQL query plan",
    )


def _run_mssql_showplan(engine: Engine, sql: str) -> _RawPlanInfo | None:
    """Toggles `SET SHOWPLAN_XML` around `sql` on one pooled connection.

    Safety detail: `SET SHOWPLAN_XML ON` changes *session* state on a
    physical, pooled connection. If anything between ON and OFF raises,
    that connection must never go back into the pool still in "return a
    plan instead of executing" mode -- the next real query on it would
    silently get a plan back instead of running. `connection.invalidate()`
    forces the pool to discard (not reuse) the connection whenever the OFF
    didn't provably run.
    """
    with engine.connect() as connection:
        showplan_disabled = False
        try:
            connection.execute(text("SET SHOWPLAN_XML ON"))
            row = connection.execute(text(sql)).fetchone()
            connection.execute(text("SET SHOWPLAN_XML OFF"))
            showplan_disabled = True
        finally:
            if not showplan_disabled:
                connection.invalidate()

    if row is None:
        return None
    xml_text = row[0]
    root = ET.fromstring(xml_text)
    stmt = root.find(".//sp:StmtSimple", _MSSQL_SHOWPLAN_NS)
    rows = None
    cost = None
    if stmt is not None:
        rows_attr = stmt.get("StatementEstRows")
        cost_attr = stmt.get("StatementSubTreeCost")
        rows = float(rows_attr) if rows_attr else None
        cost = float(cost_attr) if cost_attr else None
    relop = root.find(".//sp:RelOp", _MSSQL_SHOWPLAN_NS)
    op = relop.get("PhysicalOp") if relop is not None else "unknown"
    return _RawPlanInfo(estimated_rows=rows, estimated_cost=cost, plan_summary=str(op))


_ORACLE_STATEMENT_ID_RE = re.compile(r"[^a-zA-Z0-9_]")


def _run_oracle_explain(engine: Engine, sql: str) -> _RawPlanInfo | None:
    """Uses `EXPLAIN PLAN FOR` + a `PLAN_TABLE` read-back.

    Requires INSERT privilege on `PLAN_TABLE`, which a strictly read-only
    role may not have -- expected to fail open (this function raises, the
    caller catches broadly) in that case, same as any other estimation
    failure. A per-call statement_id scopes rows to this call, and they're
    cleaned up afterward regardless of outcome.
    """
    statement_id = f"tsql_{id(sql):x}_{threading.get_ident():x}"
    with engine.connect() as connection:
        try:
            connection.execute(text(f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {sql}"))
            row = connection.execute(
                text(
                    "SELECT cost, cardinality FROM plan_table "
                    "WHERE statement_id = :sid AND id = 0"
                ),
                {"sid": statement_id},
            ).fetchone()
        finally:
            connection.execute(
                text("DELETE FROM plan_table WHERE statement_id = :sid"), {"sid": statement_id}
            )
            connection.commit()

    if row is None:
        return None
    cost, cardinality = row[0], row[1]
    return _RawPlanInfo(
        estimated_rows=float(cardinality) if cardinality is not None else None,
        estimated_cost=float(cost) if cost is not None else None,
        plan_summary="Oracle query plan",
    )


_STRATEGIES = {
    "postgresql": _run_postgresql_explain,
    "mysql": _run_mysql_explain,
    "mssql": _run_mssql_showplan,
    "oracle": _run_oracle_explain,
}


def _run_with_timeout(strategy, engine: Engine, sql: str, timeout_seconds: float):
    """Runs `strategy(engine, sql)` on a worker thread, raising `TimeoutError` past the deadline.

    A plan-only compile is normally fast (milliseconds), so this is a pure
    safety net for a pathological edge case, not the primary defense --
    unlike `db.execution._execute_with_timeout`, an abandoned worker thread
    here is simply left to finish and clean up its own connection on its
    own schedule (daemon thread; never blocks process exit). That's an
    acceptable simplification for a rare-timeout, estimation-only path
    where correctness doesn't depend on the abandoned call's outcome.
    """
    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["value"] = strategy(engine, sql)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        raise TimeoutError(f"Cost estimation exceeded {timeout_seconds}s")
    if "error" in error:
        raise error["error"]
    return result.get("value")


def estimate_query_cost(engine: Engine, sql: str, settings: Settings) -> CostEstimate | None:
    """Estimates `sql`'s cost/row count without executing it, failing open on any problem.

    Args:
        engine: A read-only SQLAlchemy engine.
        sql: Already-validated, row-limited SQL (see `agent.sql_validator`).
        settings: For `db_type` (selects the per-engine strategy),
            `cost_estimation_enabled`, `cost_estimation_timeout_seconds`,
            and the two row thresholds.

    Returns:
        A `CostEstimate`, or None if estimation is disabled, unsupported
        for `settings.db_type`, or failed/timed out for any reason (logged
        at debug level either way -- never raises). The exception catch
        here is deliberately broad (`Exception`, not a specific list): this
        function's entire contract is "never the reason a legitimate query
        can't run" (see module docstring), so an unexpected driver-specific
        error must fail open exactly the same way an anticipated one
        (SQLAlchemyError, a timeout, malformed plan JSON/XML) does -- an
        unusual environment or a bug in a rarely-exercised code path (e.g.
        the Oracle/MySQL strategies, which this project can't verify
        against a live database) must not crash the whole agent run over
        what is, by design, an optional optimization.
    """
    if not settings.cost_estimation_enabled:
        return None

    strategy = _STRATEGIES.get(settings.db_type)
    if strategy is None:
        logger.debug(
            "[query_cost] no cost-estimation strategy for db_type=%r, skipping", settings.db_type
        )
        return None

    try:
        raw = _run_with_timeout(strategy, engine, sql, settings.cost_estimation_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring above
        logger.debug("[query_cost] estimation failed, failing open: %s", exc)
        return None

    if raw is None:
        logger.debug("[query_cost] plan produced no usable estimate, failing open")
        return None

    severity = _classify_severity(raw.estimated_rows, settings)
    estimate = CostEstimate(
        estimated_rows=raw.estimated_rows,
        estimated_cost=raw.estimated_cost,
        severity=severity,
        plan_summary=raw.plan_summary,
    )
    logger.debug(
        "[query_cost] estimated_rows=%s estimated_cost=%s severity=%s plan=%r sql=%r",
        estimate.estimated_rows,
        estimate.estimated_cost,
        estimate.severity,
        estimate.plan_summary,
        sql,
    )
    return estimate
