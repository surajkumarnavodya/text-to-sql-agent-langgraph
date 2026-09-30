"""A provider-neutral `DatabaseAdapter` contract, consolidating facts and
operations that are otherwise scattered across `db/connection.py`,
`db/execution.py`, `db/query_cost.py`, and `db/schema_introspection.py`
into one typed object per configured database connection.

**Honest framing, not an overclaim**: `agent/nodes.py` already never
branches on `db_type` -- every real call site already calls generic
functions (`db.connection.get_connection`, `db.execution
.execute_readonly_sql`, `db.query_cost.estimate_query_cost`,
`db.schema_introspection.introspect_schema`) parameterized by whichever
engine/db_type the caller resolved. Adding a fifth `DB_TYPE` among
postgresql/mysql/mssql/oracle already requires zero changes to the AI
orchestration layer today -- this module doesn't change that fact, and
this docstring says so rather than claiming credit for something that
predates it.

What *is* scattered, and what this module actually fixes: provider
*capability facts* live in three independently-maintained dicts in three
different files (`db/connection.py`'s `_VERSION_QUERIES`/
`_WRITE_PRIVILEGE_QUERIES`, `db/execution.py`'s `_STATEMENT_TIMEOUT_SQL`,
`db/query_cost.py`'s `_STRATEGIES`) -- there was no single object a
caller could ask "what can this engine do." `DatabaseCapabilities` is
that single object, computed from those same dicts (via
`db.execution.supports_driver_level_statement_timeout`/
`db.query_cost.supports_cost_estimation`), never a second, parallel
source of truth.

`DatabaseAdapter` is the unifying interface a genuinely new, currently-
unsupported engine family (e.g. DuckDB, Snowflake, SQLite) would
implement, rather than adding an entry to four separate dicts across
three files. `SqlAlchemyDatabaseAdapter` is the one real implementation
today -- a pure, zero-logic wrapper over the existing, already-tested
`db.connection`/`db.execution`/`db.query_cost`/`db.schema_introspection`
functions, exactly the "additive infrastructure, wraps existing tested
functions with zero reimplementation" pattern `analytics/provider.py`/
`semantic/metrics.py` already established in this codebase (see
`02_TARGET_ARCHITECTURE.md`). **Not called by `agent/nodes.py` or any
route in this increment** -- see `03_DATABASE_ADAPTER_CONTRACT.md`'s own
"stubbed today, wired later" note.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy import Engine

from config.settings import ConfigurationError, Settings, get_settings
from db.connection import (
    SUPPORTED_DB_TYPES,
    ConnectionTestResult,
    DbConnectionLike,
    WritePrivilegeCheckResult,
    check_write_privileges,
    get_read_only_engine,
    test_connection,
)
from db.execution import execute_readonly_sql, supports_driver_level_statement_timeout
from db.query_cost import CostEstimate, estimate_query_cost, supports_cost_estimation
from db.schema_introspection import TableSchemaInfo, get_schema_fingerprint, introspect_schema


class DatabaseCapabilities:
    """What one `DB_TYPE`'s engine supports -- the single object a caller
    should consult instead of checking a provider-specific dict directly.

    A plain class, not a `pydantic.BaseModel` like most typed contracts in
    this codebase: every field is computed once from already-validated
    config (`SUPPORTED_DB_TYPES`) and already-tested module state, never
    from untrusted input crossing a real request/response boundary the
    way `db.query_cost.CostEstimate`/`db.connection.ConnectionTestResult`
    do -- there's no external boundary here for Pydantic validation to
    protect. `frozen`-equivalent via `__slots__` and no setters, matching
    the *spirit* of this codebase's `ConfigDict(frozen=True)` convention
    without paying for validation this class doesn't need.

    Attributes:
        db_type: The `DB_TYPE` this describes.
        sqlglot_dialect: The sqlglot dialect name `agent/sql_validator.py`
            uses for this engine, or `None` for an unrecognized `db_type`
            (sqlglot's generic dialect) -- sourced from
            `db.connection.SUPPORTED_DB_TYPES`, never duplicated.
        driver_package: The pip package name for this engine's driver.
        default_port: This engine's conventional default port.
        supports_cost_estimation: Whether `db.query_cost.estimate_query_cost`
            has a real strategy for this engine (`True` for all four
            `SUPPORTED_DB_TYPES` today).
        supports_driver_level_statement_timeout: Whether a simple,
            one-line session-level statement timeout exists for this
            engine (`True` for postgresql/mysql, `False` for mssql/oracle
            -- real per-engine variance; `db.execution
            ._execute_with_timeout`'s thread-based force-close still
            covers all four uniformly regardless of this flag).
        supports_write_privilege_check: Whether
            `db.connection.check_write_privileges` has a real query for
            this engine. `True` for all four `SUPPORTED_DB_TYPES` today --
            hardcoded rather than backed by its own accessor function,
            since there is currently no per-engine variance to expose.
        supports_db_version_query: Same shape as
            `supports_write_privilege_check`, for `db.connection`'s
            `_VERSION_QUERIES` -- `True` for all four today.
        supports_on_demand_cancellation: Always `False` today --
            **disclosed, not invented**: the only cancellation mechanism
            anywhere in this codebase is `db.execution
            ._execute_with_timeout`'s automatic force-close at the
            timeout deadline; there is no callable "cancel this specific
            in-flight query, right now" API for any engine.
        supports_parameter_binding: Always `True` -- SQLAlchemy's own
            `Connection.execute(statement, parameters)` bind-parameter
            mechanism works uniformly across all four drivers (already
            used internally by `db.query_cost`'s own Oracle strategy),
            even though the main LLM-generation execution path doesn't
            use it (see `db.execution.execute_readonly_sql`'s own
            docstring for why).
    """

    __slots__ = (
        "db_type",
        "sqlglot_dialect",
        "driver_package",
        "default_port",
        "supports_cost_estimation",
        "supports_driver_level_statement_timeout",
        "supports_write_privilege_check",
        "supports_db_version_query",
        "supports_on_demand_cancellation",
        "supports_parameter_binding",
    )

    def __init__(
        self,
        *,
        db_type: str,
        sqlglot_dialect: str | None,
        driver_package: str,
        default_port: int,
        supports_cost_estimation: bool,
        supports_driver_level_statement_timeout: bool,
        supports_write_privilege_check: bool = True,
        supports_db_version_query: bool = True,
        supports_on_demand_cancellation: bool = False,
        supports_parameter_binding: bool = True,
    ) -> None:
        self.db_type = db_type
        self.sqlglot_dialect = sqlglot_dialect
        self.driver_package = driver_package
        self.default_port = default_port
        self.supports_cost_estimation = supports_cost_estimation
        self.supports_driver_level_statement_timeout = supports_driver_level_statement_timeout
        self.supports_write_privilege_check = supports_write_privilege_check
        self.supports_db_version_query = supports_db_version_query
        self.supports_on_demand_cancellation = supports_on_demand_cancellation
        self.supports_parameter_binding = supports_parameter_binding

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DatabaseCapabilities):
            return NotImplemented
        return all(getattr(self, name) == getattr(other, name) for name in self.__slots__)

    def __repr__(self) -> str:
        fields = ", ".join(f"{name}={getattr(self, name)!r}" for name in self.__slots__)
        return f"DatabaseCapabilities({fields})"

    @classmethod
    def for_db_type(cls, db_type: str) -> DatabaseCapabilities:
        """Builds the capability object for `db_type` from real config
        (`SUPPORTED_DB_TYPES`) and the real, existing per-module accessor
        functions -- never a second, hand-maintained truth table.

        Raises:
            ConfigurationError: if `db_type` isn't one of
                `SUPPORTED_DB_TYPES` -- the same "fail fast on an
                unrecognized DB_TYPE" posture and exception type
                `db.connection.build_connection_url` already uses,
                deliberately matched rather than raising a bare `KeyError`.
        """
        info = SUPPORTED_DB_TYPES.get(db_type)
        if info is None:
            raise ConfigurationError(
                f"Unsupported or missing DB_TYPE={db_type!r}. Supported values: "
                f"{', '.join(sorted(SUPPORTED_DB_TYPES))}."
            )
        return cls(
            db_type=db_type,
            sqlglot_dialect=info.sqlglot_dialect,
            driver_package=info.driver_package,
            default_port=info.default_port,
            supports_cost_estimation=supports_cost_estimation(db_type),
            supports_driver_level_statement_timeout=supports_driver_level_statement_timeout(
                db_type
            ),
        )


@runtime_checkable
class DatabaseAdapter(Protocol):
    """The provider-neutral contract every database connection this
    application queries should be reachable through.

    `runtime_checkable`, matching `analytics.provider.AnalyticsProvider`'s
    established convention in this codebase -- dependency inversion via a
    structural type, not an ABC requiring explicit subclassing.
    """

    @property
    def capabilities(self) -> DatabaseCapabilities:
        """What this adapter's engine supports -- see `DatabaseCapabilities`."""
        ...

    def test_connection(self) -> ConnectionTestResult:
        """A lightweight connect + `SELECT 1` health check. Never raises."""
        ...

    def introspect_schema(self, schema: str | None = None) -> list[TableSchemaInfo]:
        """Live, metadata-only schema introspection -- see
        `db.schema_introspection.introspect_schema`'s own docstring for
        why this never touches table data."""
        ...

    def get_schema_fingerprint(self, tables: list[TableSchemaInfo]) -> str:
        """A stable hash of `tables`' shape, for Chroma cache invalidation."""
        ...

    def execute_readonly(
        self,
        sql: str,
        timeout_seconds: int,
        max_result_rows: int | None = None,
        params: dict[str, object] | None = None,
    ) -> tuple[list[str], list[tuple]]:
        """Executes already-validated, already row-limited SQL read-only,
        with a wall-clock timeout and a row cap -- see
        `db.execution.execute_readonly_sql`'s own docstring for the full
        contract (timeout enforcement, row-cap-independent-of-LIMIT,
        optional bind parameters).

        Raises:
            SQLAlchemyError: on a SQL execution error.
            TimeoutError: if execution exceeds `timeout_seconds`.
        """
        ...

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        """A non-executing cost/row-count estimate for `sql`, or `None` if
        estimation is disabled, unsupported, or failed open -- see
        `db.query_cost.estimate_query_cost`'s own docstring. Never raises."""
        ...

    def check_write_privileges(self) -> WritePrivilegeCheckResult:
        """Best-effort check for whether the connected role has any write
        privilege -- see `db.connection.check_write_privileges`'s own
        docstring. A warning-only, defense-in-depth signal, never a hard
        gate; never raises."""
        ...


class SqlAlchemyDatabaseAdapter:
    """The one real `DatabaseAdapter` implementation today -- a pure,
    zero-logic wrapper over the existing `db.connection`/`db.execution`/
    `db.query_cost`/`db.schema_introspection` functions.

    Constructed from one `DbConnectionLike` (a `Settings` or one
    `Settings.databases` entry -- the same structural type every function
    it wraps already accepts) plus the global `Settings` (for the
    cross-database tuning knobs: cost-estimation thresholds, row caps,
    pool sizing). Uses the already-cached `get_read_only_engine` under
    the hood -- constructing this adapter never creates a second engine
    or connection pool.
    """

    def __init__(self, connection: DbConnectionLike, settings: Settings | None = None) -> None:
        self._connection = connection
        self._settings = settings or get_settings()
        self._capabilities = DatabaseCapabilities.for_db_type(connection.db_type)

    @property
    def capabilities(self) -> DatabaseCapabilities:
        return self._capabilities

    def _engine(self) -> Engine:
        return get_read_only_engine(self._connection)

    def test_connection(self) -> ConnectionTestResult:
        return test_connection(self._connection)

    def introspect_schema(self, schema: str | None = None) -> list[TableSchemaInfo]:
        return introspect_schema(self._engine(), schema)

    def get_schema_fingerprint(self, tables: list[TableSchemaInfo]) -> str:
        return get_schema_fingerprint(tables)

    def execute_readonly(
        self,
        sql: str,
        timeout_seconds: int,
        max_result_rows: int | None = None,
        params: dict[str, object] | None = None,
    ) -> tuple[list[str], list[tuple]]:
        return execute_readonly_sql(
            sql, timeout_seconds, max_result_rows, engine=self._engine(), params=params
        )

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        return estimate_query_cost(self._engine(), sql, self._connection.db_type, self._settings)

    def check_write_privileges(self) -> WritePrivilegeCheckResult:
        return check_write_privileges(self._engine(), self._connection)
