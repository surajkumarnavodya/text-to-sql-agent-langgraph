# 03 — Generic Database Adapter Core

Prompt 03 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), building on
`02_TARGET_ARCHITECTURE.md`'s boundary map, which already labeled
`db/connection.py::SUPPORTED_DB_TYPES` as the "Database Adapter"
boundary's existing reused code. **INSPECT/PLAN/IMPLEMENT (additive
only)** — no existing route, node, or `Settings` field changed behavior;
one existing function (`db.execution.execute_readonly_sql`) gained a new,
optional, default-`None` parameter that is a provable no-op for both of
its two real callers.

## 1. The honest starting point: the acceptance criterion already holds

Prompt 03's stated acceptance criterion is *"a future PostgreSQL/MySQL/
Oracle adapter can be added without modifying the AI orchestration
layer."* A code-verified read of `agent/nodes.py` shows this is **already
true today**, and was true before this prompt: every call site that
touches the database calls a generic function parameterized by whichever
engine/`db_type` the caller resolved —

- `db.connection.get_connection(settings, state["selected_database"])` /
  `get_read_only_engine(...)`
- `db.execution.execute_readonly_sql(sql, timeout, max_rows, engine=...)`
- `db.query_cost.estimate_query_cost(engine, sql, db_type, settings)`
- `db.schema_introspection.introspect_schema(engine, schema)`

None of these branches on `db_type` inside `agent/`. Adding a 5th
`DB_TYPE` to `db.connection.SUPPORTED_DB_TYPES` (plus the matching entries
in `db/execution.py`'s `_STATEMENT_TIMEOUT_SQL` and
`db/query_cost.py`'s `_STRATEGIES`, and a driver installed) already
requires zero changes to `agent/nodes.py`, `agent/graph.py`, or
`agent/orchestrator/`. This document says that plainly rather than
claiming this prompt invented an abstraction that predates it — see
`agent/provenance.py`'s `DataTruthLevel` naming convention this
initiative already established for not overclaiming.

## 2. The real, narrow gap this prompt closes

What *was* genuinely missing: a single place to ask "what can this
engine do." Capability facts were scattered across three independently-
maintained dicts in three files, with no single object connecting them:

| Fact | Lived in | Keys today |
|---|---|---|
| dialect / driver / default port | `db/connection.py::SUPPORTED_DB_TYPES` | postgresql, mysql, mssql, oracle |
| driver-level statement timeout support | `db/execution.py::_STATEMENT_TIMEOUT_SQL` | postgresql, mysql only |
| cost-estimation strategy | `db/query_cost.py::_STRATEGIES` | postgresql, mysql, mssql, oracle |
| write-privilege check query | `db/connection.py::_WRITE_PRIVILEGE_QUERIES` | postgresql, mysql, mssql, oracle |
| DB version query | `db/connection.py::_VERSION_QUERIES` | postgresql, mysql, mssql, oracle |

A caller (or a future new engine implementer) had no way to introspect
this without reading four separate private dicts across three files.
`db/adapter.py::DatabaseCapabilities` is the single, computed-not-
duplicated answer.

## 3. What was built

### `db/adapter.py` (new)

- **`DatabaseCapabilities`** — a plain class (`__slots__`, not a
  `pydantic.BaseModel`, since every field is computed once from
  already-validated config/module state, never from an external
  boundary Pydantic needs to protect). `db_type`, `sqlglot_dialect`,
  `driver_package`, `default_port` are sourced directly from
  `SUPPORTED_DB_TYPES` — never re-typed by hand.
  `supports_cost_estimation`/`supports_driver_level_statement_timeout`
  are computed from the two new accessor functions below (§3.2), never a
  second hand-copied truth table. `supports_write_privilege_check`/
  `supports_db_version_query` are hardcoded `True` (all four engines have
  a real query today; a genuine accessor function would only ever return
  `True`, so one wasn't added — see §5 for when that would change).
  `supports_on_demand_cancellation` is always `False` — **disclosed, not
  invented**: the only cancellation mechanism anywhere in this codebase
  is `db.execution._execute_with_timeout`'s automatic force-close at the
  timeout deadline (mirrored by `db/query_cost.py::_run_with_timeout`'s
  own 2026-09-25 fix); there is no callable "cancel this in-flight query,
  right now" API for any engine. `supports_parameter_binding` is always
  `True` — SQLAlchemy's bind-parameter mechanism already works uniformly
  across all four drivers (already used internally by
  `db.query_cost.py`'s own Oracle strategy) even though the main
  LLM-generation execution path doesn't use it (see §3.3).
- **`DatabaseAdapter`** — a `@runtime_checkable typing.Protocol`,
  matching `analytics.provider.AnalyticsProvider`'s established
  convention in this codebase (structural typing, not an ABC requiring
  explicit subclassing): `capabilities` (property), `test_connection()`,
  `introspect_schema(schema=None)`, `get_schema_fingerprint(tables)`,
  `execute_readonly(sql, timeout_seconds, max_result_rows=None,
  params=None)`, `estimate_cost(sql)`, `check_write_privileges()`.
- **`SqlAlchemyDatabaseAdapter`** — the one real implementation, a pure,
  zero-logic wrapper over the already-tested `db.connection`/
  `db.execution`/`db.query_cost`/`db.schema_introspection` functions.
  Constructed from one `DbConnectionLike` (a `Settings` or one
  `Settings.databases` entry — the exact structural type every wrapped
  function already accepts) and uses the already-cached
  `get_read_only_engine()` underneath — constructing or using this
  adapter never opens a second engine or connection pool. Exactly the
  "additive infrastructure, wraps existing tested functions with zero
  reimplementation" pattern `analytics/provider.py`/`semantic/metrics.py`
  established in Prompt 02.

### Two new small accessor functions (not four)

- `db.execution.supports_driver_level_statement_timeout(db_type: str) ->
  bool` — `db_type in _STATEMENT_TIMEOUT_SQL`. Earns a real function
  (rather than a hardcoded capability) because there *is* real per-engine
  variance: `True` for postgresql/mysql, `False` for mssql/oracle.
- `db.query_cost.supports_cost_estimation(db_type: str) -> bool` —
  `db_type in _STRATEGIES`.

Neither dict changed. Neither function is called anywhere except
`db/adapter.py` and their own tests today.

### `db/execution.py`: one real, additive behavior extension

`_execute_with_timeout` and the public `execute_readonly_sql` both gained
a new trailing parameter, `params: dict[str, object] | None = None`
(SQLAlchemy's own `Connection.execute(statement, parameters)` bind
mechanism — already used internally by `db/query_cost.py`'s Oracle
strategy, just never exposed as a parameter of this function before). It
is threaded into the one real execute call as:

```python
cursor_result = (
    connection.execute(text(sql), params) if params else connection.execute(text(sql))
)
```

When `params` is falsy (its default), this reproduces the exact original
call — no second argument at all. Verified: both real callers
(`agent/nodes.py`'s `execute_sql_node`, `api/main.py`'s `POST /execute`)
pass exactly 3 positional args + `engine=` and never pass `params` —
neither call site changed behavior. This was a deliberate choice over
having the adapter re-open a connection and duplicate the timeout/
force-close/row-cap logic itself — that logic has its own documented,
benchmark-driven fix history (`db/query_cost.py::_run_with_timeout`'s
2026-09-25 connection-leak fix mirrors `_execute_with_timeout`'s
mechanic *because* a second copy of it had already drifted once before);
extending the one real implementation instead of writing a second one is
what keeps "zero reimplementation" true.

## 4. "Stubbed today, wired later" — who consumes this

**Nothing in the live request path calls `db/adapter.py` in this
increment.** `agent/nodes.py`, `agent/graph.py`, `agent/orchestrator/`,
and every route in `api/` are byte-for-byte unchanged in behavior — this
mirrors the exact disclosure `agent/tools/` (Prompt-series precedent) and
`recommendation/` (Prompt 02) already made for their own "real, tested,
functionally complete, but not yet wired into the live path" increments.

This becomes load-bearing the day a genuinely new, currently-unsupported
engine family (DuckDB, Snowflake, SQLite, ...) needs to be added: that
engine would implement `DatabaseAdapter` (or extend
`SqlAlchemyDatabaseAdapter` if it's SQLAlchemy-compatible) and register
its capability facts in `DatabaseCapabilities.for_db_type` once, instead
of touching four separate dicts across three files as today's four
engines would require. Until that day, `SqlAlchemyDatabaseAdapter` is a
correct, tested, but optional second way to reach the exact same
functions every existing call site already calls directly.

## 5. Explicitly out of scope (per the approved plan)

- Wiring `DatabaseAdapter` into `agent/nodes.py`/`agent/graph.py`/
  `agent/orchestrator/` — no live bug this would fix; the acceptance
  criterion already holds without it (§1).
- Real on-demand query cancellation — doesn't exist anywhere in this
  codebase; disclosed as a gap (`supports_on_demand_cancellation` is
  always `False`), not invented.
- Adding a 5th database engine.
- A capabilities cache, a config-driven capability override mechanism, or
  any re-export ceremony beyond what `analytics/__init__.py` already
  does minimally.
- Passing `params` from either real `execute_readonly_sql` call site —
  the LLM-generation path still embeds filter values as validated SQL
  literals, not bind parameters; `params` exists for `DatabaseAdapter`'s
  contract and a future caller, not for today's two real ones.

## 6. Testing

- `tests/test_db_adapter.py` (32 tests, isolated/contract-level): the two
  new accessor functions (including a direct-dict-membership compatibility
  assertion), `DatabaseCapabilities.for_db_type` for all four engines plus
  the `ConfigurationError` path for an unknown one, equality/repr, and a
  hand-built `_FakeDatabaseAdapter` proving the Protocol is genuinely
  satisfiable by an independent implementation (mirrors
  `tests/test_analytics_provider.py`'s Protocol-conformance test).
- `tests/test_db_adapter_sqlalchemy.py` (22 tests, wiring-level):
  `SqlAlchemyDatabaseAdapter` against monkeypatched
  `db.connection`/`db.execution`/`db.query_cost`/`db.schema_introspection`
  functions, proving every method forwards its arguments unmodified
  (mirrors `tests/test_tools_definitions.py`'s style); a
  `params`-omitted-is-a-no-op regression test and a
  `params`-threaded-through test; `TimeoutError`/`SQLAlchemyError`
  propagation tests (never swallowed); and parametrized compatibility
  tests over all 4 `SUPPORTED_DB_TYPES` asserting
  `DatabaseCapabilities` never diverges from the real
  `_STATEMENT_TIMEOUT_SQL`/`_STRATEGIES` dicts directly — the actual
  regression this prompt exists to prevent (a future 5th dict entry
  added to one module without a matching capability update would fail
  this test).

Full suite: 2397 tests pass (2343 pre-existing + 54 new), zero
regressions. `ruff check .` / `black --check .` / `mypy .` on new/touched
files: clean. Whole-repo counts confirmed unchanged from the disclosed
pre-existing baseline: 7 ruff / 11 black / 236 mypy findings, none in any
file this prompt touched (cross-referenced by file path).
