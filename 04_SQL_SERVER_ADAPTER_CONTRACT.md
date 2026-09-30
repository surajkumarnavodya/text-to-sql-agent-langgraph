# 04 — SQL Server Adapter Correctness Fix

Prompt 04 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`03_DATABASE_ADAPTER_CONTRACT.md`'s generic `DatabaseAdapter` core.
**INSPECT/PLAN/IMPLEMENT/TEST/REVIEW/FIX/DOCUMENT** — a narrow, additive
correctness fix inside `db/adapter.py`'s own `SqlAlchemyDatabaseAdapter`.
No existing route, node, or `Settings` field changed behavior; neither
real production call site (`agent.nodes.execute_sql_node`, `api/main.py`'s
`POST /execute`) uses this adapter, so nothing they do changed either.

## 1. Inspection: what Prompt 03 actually claimed vs. what was true

Prompt 03's `db/adapter.py` docstring calls `SqlAlchemyDatabaseAdapter` "a
correct, tested, but optional second way to reach the exact same
functions every existing call site already calls directly." Inspecting
the two real call sites against the adapter's own `execute_readonly`
showed that claim wasn't quite true:

| | `agent.nodes.execute_sql_node` | `api/main.py` `POST /execute` | `SqlAlchemyDatabaseAdapter.execute_readonly` (pre-fix) |
|---|---|---|---|
| Calls `execute_readonly_sql` | Yes | Yes | Yes |
| Applies `agent.sql_validator.qualify_table_schema` first | **Yes** (`agent/nodes.py:1312`) | **Yes** (`api/main.py:1307`) | **No** |

`qualify_table_schema` exists precisely because an engine resolves an
unqualified table name against the *connecting user's own default
schema*, not `DatabaseConnectionConfig.db_schema`
(`DB_<NAME>_SCHEMA`) — CLAUDE.md's own "Python 3.14 gotchas" section
documents this as a real, already-reproduced bug for this project's own
`HrAutomationDb` configuration (tables under the `employee` schema, the
connecting user's default schema `dbo`), fixed at both real call sites.
The adapter — meant to be a drop-in replacement for calling those
functions directly — skipped this step, so any future caller that
switched from calling `execute_readonly_sql` directly to going through
`SqlAlchemyDatabaseAdapter` against a schema-qualified connection would
have silently reintroduced the exact "Invalid object name" failure mode
that bug report already closed once.

**Why "SQL Server" and not a generic title**: `qualify_table_schema`
itself is dialect-parameterized, not MSSQL-specific — the fix below
applies uniformly to all four `SUPPORTED_DB_TYPES`. But this project's own
real, currently-configured non-default-schema case is exclusively the
SQL Server one (`HrAutomationDb`); no Postgres/MySQL/Oracle connection in
this codebase's own examples relies on this path today. This prompt is
named for the concrete, currently-relevant bug it closes, not for a new
MSSQL-only code branch — there isn't one.

## 2. What was built

### `db/adapter.py`

`SqlAlchemyDatabaseAdapter.execute_readonly` now builds an
execution-only, schema-qualified copy of `sql` via
`agent.sql_validator.qualify_table_schema(sql, self._connection.db_schema,
dialect=self._capabilities.sqlglot_dialect)` before calling
`execute_readonly_sql` — the identical pattern `execute_sql_node` already
uses (`execution_sql` as a local variable; the caller's own `sql`
argument is never mutated, matching that node's own "`state["sql"]` stays
exactly what the model produced" contract). `self._capabilities
.sqlglot_dialect` (already resolved by `DatabaseCapabilities.for_db_type`
in Prompt 03) is reused rather than re-deriving the dialect a second way.

Zero new dicts, zero new capability flags, zero new `Settings` fields —
`db_schema` was already on `DbConnectionLike` (Prompt 03's own structural
type), and `qualify_table_schema` was already a public, tested function
in `agent/sql_validator.py`. This is a pure consumer of both, not a
reimplementation of either.

**Verified no import cycle**: `agent/sql_validator.py` imports only
`sqlglot`/`pydantic`/stdlib — nothing from `db/` — so `db/adapter.py`
importing `agent.sql_validator.qualify_table_schema` is a one-directional
dependency, confirmed by a clean `import db.adapter` in this project's
own venv.

**No-op for the common case**: a connection with no configured
`db_schema` (every `SUPPORTED_DB_TYPES` example in this codebase except
the one real MSSQL case above) sends `execute_readonly_sql` the exact
original SQL text, unchanged — `qualify_table_schema` already guarantees
this (`if not schema: return sql`), and the existing
`test_forwards_all_arguments_including_engine` regression test (Prompt
03, unmodified) still passes unchanged, proving it.

## 3. Explicitly out of scope

- **Wiring `DatabaseAdapter` into `agent/nodes.py`/`agent/graph.py`/
  `agent/orchestrator/`/any route** — unchanged from Prompt 03 §5; this
  fix makes the adapter *correct* for a future caller, it doesn't create
  one.
- **`rag/store.py`'s native SQL Server `VECTOR` storage.** A real,
  disclosed boundary, not a silent gap: that module opens its own
  `create_engine` against `RAG_STORE_CONNECTION_STRING` directly and was
  never modeled through `db.connection`/`db.adapter` at all (see that
  module's own docstring for why it's deliberately a separate connection
  from `DB_CONNECTIONS`). Unifying it under `DatabaseAdapter`/
  `DatabaseCapabilities` would mean inventing a new, hand-maintained
  capability fact (e.g. "supports native vector storage") with no
  existing per-module accessor function backing it — exactly the
  parallel-truth-table anti-pattern Prompt 03's own design avoided. Left
  alone.
- **A new `Settings`/`DatabaseConnectionConfig` field, or a new
  `DatabaseCapabilities` attribute.** `db_schema`/`db_odbc_driver` are
  already per-connection config fields, not per-engine capability facts —
  there was nothing to add.
- **Adding a 5th database engine.**
- **Real on-demand query cancellation.** Unchanged from Prompt 03 §5.

## 4. Testing

`tests/test_db_adapter_sqlalchemy.py` gained
`TestExecuteReadonlyAppliesSchemaQualification` (5 new tests, mirroring
the file's existing monkeypatch-and-assert style):

- **No-op when no schema is configured** — the exact SQL text reaches
  `execute_readonly_sql` unchanged.
- **Qualifies an unqualified table** for an `mssql` connection with
  `db_schema="employee"` (`SELECT * FROM Employee` →
  `SELECT * FROM employee.Employee"`) — the real bug this prompt closes,
  reproduced and proven fixed.
- **Leaves an already-qualified table alone** (`dbo.AlreadyQualified`) —
  `qualify_table_schema` only fills in a missing schema, never overrides
  one; this adapter must preserve that.
- **Dialect-genericity**: the same behavior against a `postgresql`
  connection with `db_schema="reporting"`, proving the adapter reads
  `self._capabilities.sqlglot_dialect` rather than hardcoding `"tsql"` —
  the regression this test exists to prevent is a future edit that
  special-cases MSSQL instead of staying dialect-generic.
- **The caller's own `sql` string is never mutated** — documents the
  "execution-only copy" contract explicitly, even though it's moot for an
  immutable `str`.

Full suite: **2402 tests pass (2397 pre-existing + 5 new), zero
regressions** (`.venv/Scripts/python.exe -m pytest -q`, 123.97s).
`ruff check db/adapter.py tests/test_db_adapter_sqlalchemy.py`,
`black --check` (after one reformat of the new test file, re-verified),
and `mypy db/adapter.py` are all clean.

## 5. Security / tenant-isolation / performance review

- **Security**: no new SQL construction path — `qualify_table_schema`
  operates on already-validated, already row-limited SQL text
  (`agent/sql_validator.py`'s own AST allowlist runs upstream of this
  adapter in every real path today, and would still run upstream of it in
  any future wired-in path, exactly as it does for the two real call
  sites). This fix closes a *correctness* gap (wrong/failing queries
  against a schema-qualified connection), not a security gap — there was
  no way to reach unvalidated SQL through the missing step.
- **Tenant isolation**: `db_schema` is per-connection config
  (`Settings.databases[i].db_schema`), read the same way
  `execute_sql_node` already reads it — no new tenant-crossing surface;
  this fix, if anything, makes per-connection schema scoping *more*
  reliably enforced through the adapter path, not less.
- **Performance**: `qualify_table_schema` is a single `sqlglot` parse +
  AST walk + re-render, the same cost `execute_sql_node`/`POST /execute`
  already pay on every real execution today — no new cost class
  introduced, and a strict no-op (skips parsing entirely) when
  `db_schema` is `None`.

## 6. Known limitations / remaining risks

- Still **not wired into any live route** — this closes a latent
  correctness gap in code nothing calls yet, per Prompt 03's own
  "stubbed today, wired later" posture. The day something *does* call
  `SqlAlchemyDatabaseAdapter.execute_readonly` in production, this fix is
  what keeps it from being a silent regression for this project's real
  SQL Server configuration.
- `rag/store.py`'s SQL-Server-only native `VECTOR` storage remains
  entirely outside `DatabaseAdapter`/`DatabaseCapabilities` — a
  deliberate, disclosed scope boundary (§3), not something this prompt
  attempted and fell short of.
- No behavioral change was possible to verify against a **real** SQL
  Server instance with a genuinely non-default schema in this session —
  verification is at the same level as Prompt 03's own (mocked
  `db.connection`/`db.execution` functions), not a live re-run of the
  original bug report's own manual reproduction.

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — the next boundary marked
"Extend" with a real, already-computed data source
(`agent.insight.ResultSummary.trend`/`.outliers`/`stddev`) and a
documented trigger (`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md`'s own
P1 "Data analysis" roadmap item). Unlike this prompt and Prompts 02-03,
that one *does* change a live response shape (a new, additive
`AskResponse` field) and therefore needs explicit user sign-off on the
API/UI contract change before implementation, not just an INSPECT/PLAN
pass.
