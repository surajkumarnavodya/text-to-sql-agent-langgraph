"""SQL Server Query Store performance intelligence -- Prompt 19
(`19_QUERY_STORE_PERFORMANCE_CONTRACT.md`).

**Additive, SQL-Server-only, and fully optional** -- the same posture
`db/query_cost.py`'s own per-engine `_STRATEGIES` dict establishes for
proactive cost estimation, applied here to a feature that genuinely has
no equivalent on postgresql/mysql/oracle: SQL Server's Query Store is a
server-side feature (`ALTER DATABASE ... SET QUERY_STORE = ON`) with no
analogue this codebase's other three supported engines expose through a
comparable DMV surface. Every public function here checks `db_type ==
"mssql"` (or is only ever called after a caller already has) before
doing anything database-specific, and `check_query_store_availability`
is the single, mandatory first call every other function in this module
depends on -- a non-MSSQL database, a disabled feature flag, Query Store
not enabled on the target database, or insufficient permission to read
its DMVs all resolve to the identical "unavailable" outcome, never an
exception, mirroring `db.connection.check_write_privileges`'s own
`checked: bool`-and-fail-open contract exactly.

**Least privilege**: reading Query Store's catalog views/DMVs
(`sys.database_query_store_options`, `sys.query_store_query`,
`sys.query_store_query_text`, `sys.query_store_plan`,
`sys.query_store_runtime_stats`, `sys.query_store_runtime_stats_interval`)
requires the connected principal to hold `VIEW DATABASE STATE` (SQL
Server 2016-2019) or `VIEW DATABASE PERFORMANCE STATE` (SQL Server
2022+) on the target database -- a database-scoped permission, not
`VIEW SERVER STATE`/sysadmin. This is an *optional* grant on top of this
project's existing read-only `DB_USER` role (see `SECURITY.md`'s
least-privilege guidance) -- a `DB_USER` without it simply never sees
Query Store insights (`checked=False`), the connection and every other
feature keep working exactly as before.

**Literals are never surfaced.** Query Store's own `query_sql_text` can
contain literal values (customer names, emails, account numbers --
whatever a query actually filtered on) when forced parameterization
isn't enabled on the target database. `_mask_literals` parses every
query's text with `sqlglot` and replaces every `exp.Literal` node with a
bare placeholder before it is ever truncated into a
`normalized_sql_preview` -- the *only* text form this module ever
returns. A query this module's own parser can't handle degrades to a
fixed, generic placeholder string, never a silent pass-through of the
raw text (fail closed on the one thing that matters: never leaking a
literal). Statistics (execution count, durations, CPU, logical reads,
the `query_hash` fingerprint) are the only other data this module
surfaces -- all numeric/structural, nothing content-bearing.

**Zero new execution path.** Every Query Store DMV read goes through
`db.execution.execute_readonly_sql` -- the exact same timeout-enforced,
row-capped executor `agent.nodes.execute_sql_node`/`POST /execute`
already use for everything else in this application, not a second,
parallel query-execution mechanism.
"""

from __future__ import annotations

import logging
import threading
import time

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

from config.settings import Settings, get_settings
from db.execution import execute_readonly_sql
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)

_MSSQL_DIALECT = "tsql"
_UNPARSEABLE_PREVIEW = "<query text could not be safely parsed; preview omitted>"


class QueryStoreAvailability(BaseModel):
    """Whether Query Store insights can be read at all for one
    connection -- the one and only result every caller must check before
    trusting `QueryStoreFindings.top_queries`/`.regressions`.

    Attributes:
        available: `True` only when `db_type == "mssql"`,
            `Settings.enable_query_store_insights` is on, the target
            database actually has Query Store enabled
            (`sys.database_query_store_options.actual_state_desc ==
            "READ_WRITE"`), and reading that catalog view itself
            succeeded.
        reason: Always set -- either "available" or a short, human-
            readable explanation (never a raw driver error string; see
            `check_query_store_availability`'s own docstring).
    """

    model_config = ConfigDict(frozen=True)

    available: bool
    reason: str


class QueryStoreQueryStats(BaseModel):
    """One high-cost/repeated query's own aggregated Query Store
    statistics over the lookback window.

    Attributes:
        query_id: Query Store's own internal identifier -- stable across
            plan changes for the same query shape, unlike `query_hash`
            alone (which identifies the normalized query text, not a
            specific plan). Carried for traceability only, never shown
            as if it were meaningful to an end user.
        query_fingerprint: Hex of `sys.query_store_query.query_hash` --
            a stable identifier for this normalized query shape, safe to
            surface since it is a hash, never the text itself.
        normalized_sql_preview: `_mask_literals`'s output, truncated to
            `Settings.query_store_preview_max_chars` -- the only form of
            this query's text this module ever returns. See this
            module's own docstring.
        execution_count: Total executions across the lookback window.
        avg_duration_ms: Average wall-clock duration per execution.
        avg_cpu_ms: Average CPU time per execution.
        avg_logical_reads: Average logical page reads per execution.
        plan_count: How many distinct plans Query Store has recorded for
            this query -- more than one can itself indicate plan
            instability (parameter sniffing), surfaced as a plain fact,
            never auto-diagnosed as the cause.
        has_forced_plan: Whether an administrator has pinned
            (`sp_query_store_force_plan`) a specific plan for this query.
    """

    model_config = ConfigDict(frozen=True)

    query_id: int
    query_fingerprint: str
    normalized_sql_preview: str
    execution_count: int
    avg_duration_ms: float
    avg_cpu_ms: float
    avg_logical_reads: float
    plan_count: int
    has_forced_plan: bool


class QueryStoreRegression(BaseModel):
    """One query whose recent average duration has regressed against its
    own historical baseline within the same lookback window -- entirely
    self-referential (this query vs. its own past), never compared
    against any other query.

    Attributes:
        regression_factor: `recent_avg_duration_ms / baseline_avg_duration_ms`
            -- always `>= Settings.query_store_regression_factor` for a
            constructed instance (the rule's own validation bar; see
            `get_regressions`).
    """

    model_config = ConfigDict(frozen=True)

    query_id: int
    query_fingerprint: str
    normalized_sql_preview: str
    baseline_avg_duration_ms: float
    recent_avg_duration_ms: float
    regression_factor: float
    recent_execution_count: int
    baseline_execution_count: int


class QueryStoreFindings(BaseModel):
    """`get_query_store_findings`'s full result -- always constructible,
    never raises; `top_queries`/`regressions` are empty (not a sentinel)
    whenever `availability.available` is `False` or nothing cleared the
    configured thresholds."""

    model_config = ConfigDict(frozen=True)

    availability: QueryStoreAvailability
    top_queries: tuple[QueryStoreQueryStats, ...] = ()
    regressions: tuple[QueryStoreRegression, ...] = ()
    lookback_hours: float


def _mask_literals(sql: str | None, settings: Settings) -> str:
    """Replaces every literal value in `sql` with a bare placeholder and
    truncates the result -- the one function standing between a Query
    Store DMV's own stored query text and anything this module ever
    returns. See this module's own docstring for why this exists.

    Never raises: a `None`/empty input, or one `sqlglot` can't parse,
    returns a fixed, generic placeholder string -- never the original
    text, fail-closed on the one thing that matters here.
    """
    if not sql:
        return _UNPARSEABLE_PREVIEW
    try:
        parsed = sqlglot.parse_one(sql, read=_MSSQL_DIALECT)
        masked = parsed.transform(
            lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node
        )
        rendered = masked.sql(dialect=_MSSQL_DIALECT)
    except Exception:  # noqa: BLE001 - never leak raw text on any failure, whatever the cause
        return _UNPARSEABLE_PREVIEW
    # Defense in depth: redact_secrets targets known secret *shapes*
    # (connection strings, API keys) that a masked query text should
    # never contain anyway, but costs nothing to apply here too.
    rendered = redact_secrets(rendered, settings)
    limit = settings.query_store_preview_max_chars
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "…"


def check_query_store_availability(
    engine: Engine, db_type: str, settings: Settings | None = None
) -> QueryStoreAvailability:
    """The mandatory first call -- see this module's own docstring.

    Never raises: an unrecognized/non-"mssql" `db_type`, a disabled
    feature flag, a permission error reading
    `sys.database_query_store_options`, or Query Store genuinely not
    being enabled on the target database all resolve to
    `available=False` with a distinct, honest `reason` -- never
    conflated with each other, but all equally "nothing more to do
    here," never an exception a caller must handle.
    """
    settings = settings or get_settings()
    if db_type != "mssql":
        return QueryStoreAvailability(
            available=False,
            reason=f"Query Store is a SQL Server-only feature (db_type={db_type!r}).",
        )
    if not settings.enable_query_store_insights:
        return QueryStoreAvailability(
            available=False,
            reason="Query Store insights are disabled (enable_query_store_insights=False).",
        )

    try:
        _columns, rows = execute_readonly_sql(
            "SELECT actual_state_desc FROM sys.database_query_store_options",
            settings.query_store_timeout_seconds,
            max_result_rows=1,
            engine=engine,
        )
    except (SQLAlchemyError, TimeoutError) as exc:
        safe_detail = redact_secrets(str(exc), settings)
        logger.debug("[query_store] availability check failed, failing open: %s", safe_detail)
        return QueryStoreAvailability(
            available=False,
            reason="Could not read Query Store status (insufficient permission, or unreachable).",
        )

    if not rows:
        return QueryStoreAvailability(
            available=False, reason="Query Store has never been configured for this database."
        )
    actual_state = str(rows[0][0] or "").upper()
    if actual_state != "READ_WRITE":
        return QueryStoreAvailability(
            available=False,
            reason=f"Query Store is not actively collecting (state={actual_state!r}).",
        )
    return QueryStoreAvailability(available=True, reason="available")


_TOP_QUERIES_SQL = """
SELECT TOP (:top_n)
    q.query_id,
    q.query_hash,
    qt.query_sql_text,
    SUM(rs.count_executions) AS execution_count,
    AVG(rs.avg_duration_us) / 1000.0 AS avg_duration_ms,
    AVG(rs.avg_cpu_time_us) / 1000.0 AS avg_cpu_ms,
    AVG(rs.avg_logical_io_reads) AS avg_logical_reads,
    COUNT(DISTINCT p.plan_id) AS plan_count,
    MAX(CASE WHEN p.is_forced_plan = 1 THEN 1 ELSE 0 END) AS has_forced_plan
FROM sys.query_store_query AS q
JOIN sys.query_store_query_text AS qt ON q.query_text_id = qt.query_text_id
JOIN sys.query_store_plan AS p ON p.query_id = q.query_id
JOIN sys.query_store_runtime_stats AS rs ON rs.plan_id = p.plan_id
JOIN sys.query_store_runtime_stats_interval AS rsi
    ON rsi.runtime_stats_interval_id = rs.runtime_stats_interval_id
WHERE rsi.start_time >= DATEADD(HOUR, -:lookback_hours, SYSUTCDATETIME())
GROUP BY q.query_id, q.query_hash
HAVING SUM(rs.count_executions) >= :min_execution_count
ORDER BY AVG(rs.avg_duration_us) * SUM(rs.count_executions) DESC
"""


def get_top_queries(
    engine: Engine, settings: Settings | None = None
) -> tuple[QueryStoreQueryStats, ...]:
    """The top `Settings.query_store_top_n_queries` queries by total
    (avg duration × execution count) over `Settings
    .query_store_lookback_hours` -- "high-cost/repeated patterns," the
    literal requirement. Caller must already know
    `check_query_store_availability` returned `available=True` -- this
    function does not re-check (a cheap, already-answered question one
    layer up), but still fails open (returns `()`) on any execution
    error, never raising.
    """
    settings = settings or get_settings()
    try:
        columns, rows = execute_readonly_sql(
            _TOP_QUERIES_SQL,
            settings.query_store_timeout_seconds,
            max_result_rows=settings.query_store_top_n_queries,
            engine=engine,
            params={
                "top_n": settings.query_store_top_n_queries,
                "lookback_hours": settings.query_store_lookback_hours,
                "min_execution_count": settings.query_store_min_execution_count,
            },
        )
    except (SQLAlchemyError, TimeoutError) as exc:
        safe_detail = redact_secrets(str(exc), settings)
        logger.debug("[query_store] top-queries read failed, failing open: %s", safe_detail)
        return ()

    results: list[QueryStoreQueryStats] = []
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        results.append(
            QueryStoreQueryStats(
                query_id=int(record["query_id"]),
                query_fingerprint=(
                    (record["query_hash"] or b"").hex()
                    if isinstance(record["query_hash"], bytes)
                    else str(record["query_hash"] or "")
                ),
                normalized_sql_preview=_mask_literals(record.get("query_sql_text"), settings),
                execution_count=int(record["execution_count"] or 0),
                avg_duration_ms=float(record["avg_duration_ms"] or 0.0),
                avg_cpu_ms=float(record["avg_cpu_ms"] or 0.0),
                avg_logical_reads=float(record["avg_logical_reads"] or 0.0),
                plan_count=int(record["plan_count"] or 0),
                has_forced_plan=bool(record["has_forced_plan"]),
            )
        )
    return tuple(results)


_REGRESSIONS_SQL = """
WITH recent AS (
    SELECT rs.plan_id,
           SUM(rs.count_executions) AS execution_count,
           SUM(rs.avg_duration_us * rs.count_executions) / SUM(rs.count_executions) AS avg_duration_us
    FROM sys.query_store_runtime_stats AS rs
    JOIN sys.query_store_runtime_stats_interval AS rsi
        ON rsi.runtime_stats_interval_id = rs.runtime_stats_interval_id
    WHERE rsi.start_time >= DATEADD(HOUR, -:recent_hours, SYSUTCDATETIME())
    GROUP BY rs.plan_id
),
baseline AS (
    SELECT rs.plan_id,
           SUM(rs.count_executions) AS execution_count,
           SUM(rs.avg_duration_us * rs.count_executions) / SUM(rs.count_executions) AS avg_duration_us
    FROM sys.query_store_runtime_stats AS rs
    JOIN sys.query_store_runtime_stats_interval AS rsi
        ON rsi.runtime_stats_interval_id = rs.runtime_stats_interval_id
    WHERE rsi.start_time >= DATEADD(HOUR, -:lookback_hours, SYSUTCDATETIME())
      AND rsi.start_time < DATEADD(HOUR, -:recent_hours, SYSUTCDATETIME())
    GROUP BY rs.plan_id
)
SELECT TOP (:top_n)
    q.query_id,
    q.query_hash,
    qt.query_sql_text,
    baseline.avg_duration_us / 1000.0 AS baseline_avg_duration_ms,
    recent.avg_duration_us / 1000.0 AS recent_avg_duration_ms,
    recent.execution_count AS recent_execution_count,
    baseline.execution_count AS baseline_execution_count
FROM recent
JOIN baseline ON baseline.plan_id = recent.plan_id
JOIN sys.query_store_plan AS p ON p.plan_id = recent.plan_id
JOIN sys.query_store_query AS q ON q.query_id = p.query_id
JOIN sys.query_store_query_text AS qt ON qt.query_text_id = q.query_text_id
WHERE recent.execution_count >= :min_execution_count
  AND baseline.execution_count >= :min_execution_count
  AND baseline.avg_duration_us > 0
  AND recent.avg_duration_us >= baseline.avg_duration_us * :regression_factor
ORDER BY (recent.avg_duration_us * 1.0 / baseline.avg_duration_us) DESC
"""


def get_regressions(
    engine: Engine, settings: Settings | None = None
) -> tuple[QueryStoreRegression, ...]:
    """Queries whose average duration in the most recent `Settings
    .query_store_regression_recent_hours` window is at least
    `Settings.query_store_regression_factor`x slower than their own
    baseline (the rest of the lookback window) -- "regressed patterns,"
    entirely self-referential, never comparing one query against
    another. Fails open (returns `()`) on any execution error.
    """
    settings = settings or get_settings()
    try:
        columns, rows = execute_readonly_sql(
            _REGRESSIONS_SQL,
            settings.query_store_timeout_seconds,
            max_result_rows=settings.query_store_top_n_queries,
            engine=engine,
            params={
                "top_n": settings.query_store_top_n_queries,
                "lookback_hours": settings.query_store_lookback_hours,
                "recent_hours": settings.query_store_regression_recent_hours,
                "min_execution_count": settings.query_store_min_execution_count,
                "regression_factor": settings.query_store_regression_factor,
            },
        )
    except (SQLAlchemyError, TimeoutError) as exc:
        safe_detail = redact_secrets(str(exc), settings)
        logger.debug("[query_store] regression read failed, failing open: %s", safe_detail)
        return ()

    results: list[QueryStoreRegression] = []
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        baseline_ms = float(record["baseline_avg_duration_ms"] or 0.0)
        recent_ms = float(record["recent_avg_duration_ms"] or 0.0)
        results.append(
            QueryStoreRegression(
                query_id=int(record["query_id"]),
                query_fingerprint=(
                    (record["query_hash"] or b"").hex()
                    if isinstance(record["query_hash"], bytes)
                    else str(record["query_hash"] or "")
                ),
                normalized_sql_preview=_mask_literals(record.get("query_sql_text"), settings),
                baseline_avg_duration_ms=baseline_ms,
                recent_avg_duration_ms=recent_ms,
                regression_factor=round(recent_ms / baseline_ms, 2) if baseline_ms else 0.0,
                recent_execution_count=int(record["recent_execution_count"] or 0),
                baseline_execution_count=int(record["baseline_execution_count"] or 0),
            )
        )
    return tuple(results)


def get_query_store_findings(
    engine: Engine, db_type: str, settings: Settings | None = None
) -> QueryStoreFindings:
    """The single entry point: checks availability first, and only ever
    runs the two DMV reads above when it's `True` -- an unavailable
    Query Store costs exactly one cheap catalog-view read, never two
    failed DMV joins. Never raises.
    """
    settings = settings or get_settings()
    availability = check_query_store_availability(engine, db_type, settings)
    if not availability.available:
        return QueryStoreFindings(
            availability=availability,
            lookback_hours=settings.query_store_lookback_hours,
        )

    top_queries = get_top_queries(engine, settings)
    regressions = get_regressions(engine, settings)
    return QueryStoreFindings(
        availability=availability,
        top_queries=top_queries,
        regressions=regressions,
        lookback_hours=settings.query_store_lookback_hours,
    )


# ---------------------------------------------------------------------------
# A small, in-process TTL cache -- keeps the live `/ask` pipeline's added
# cost "usually free." Query Store reflects server-wide historical
# activity, not anything specific to one question, so re-running both DMV
# joins on every single request would be pure waste; this mirrors
# `observability.metrics.PerformanceMetrics`'s own "cheap, lock-protected,
# single-process, resets on restart" posture, adapted with a wall-clock
# TTL since this module's own data genuinely lives in the database, not
# in already-collected in-process state.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_findings_cache: dict[str, tuple[float, QueryStoreFindings]] = {}


def get_cached_query_store_findings(
    engine: Engine, db_type: str, cache_key: str, settings: Settings | None = None
) -> QueryStoreFindings:
    """Same contract as `get_query_store_findings`, but reuses a result
    already computed for `cache_key` within `Settings
    .query_store_refresh_interval_seconds` instead of re-querying. `cache_key`
    is typically the configured database's own name (`Settings.databases
    [i].name`) -- one cache slot per configured database, never shared
    across them.
    """
    settings = settings or get_settings()
    now = time.monotonic()
    with _cache_lock:
        cached = _findings_cache.get(cache_key)
        if cached is not None:
            cached_at, findings = cached
            if now - cached_at < settings.query_store_refresh_interval_seconds:
                return findings

    findings = get_query_store_findings(engine, db_type, settings)
    with _cache_lock:
        _findings_cache[cache_key] = (now, findings)
    return findings


def clear_query_store_cache() -> None:
    """Clears every cached entry -- test isolation only (mirrors
    `tests/conftest.py::_clear_process_singleton_caches`'s own purpose
    for this module's one piece of process-lifetime state)."""
    with _cache_lock:
        _findings_cache.clear()
