# 05 — SQL Server Dialect, Validation & Safe Execution

Prompt 05 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`04_SQL_SERVER_ADAPTER_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — two narrow, real bug fixes inside
`agent/sql_validator.py`, found by actually running every T-SQL
construct in the prompt's own requirements list through the real
functions, not by reading the code and assuming correctness.

## 1. The honest starting point: almost nothing needed to change

`agent/sql_validator.py` is allowlist-**on-shape**, not allowlist-on-
keyword: `validate_sql` only asks (1) is the parsed root node type in
`{Select, Union, Except, Intersect}`, and (2) does the full tree contain
a disallowed node (write/DDL, a dangerous function, a system-catalog
reference, a nested aggregate, an unsafe query option)? It never asks
"does this use `TOP`?" or "does this use `STRING_AGG`?" — those are
ordinary nodes inside an `exp.Select` tree to sqlglot's own `tsql`
grammar. Every construct this prompt names — `TOP`, `OFFSET`/`FETCH`,
CTEs, window functions, `DATEADD`/`DATEDIFF`/`DATEPART`/`DATENAME`,
`EOMONTH`, `STRING_AGG`, `TRY_CAST`/`TRY_CONVERT`, `PERCENTILE_CONT
... WITHIN GROUP ... OVER (...)`, `PIVOT`/`UNPIVOT`, bracket
identifiers — was verified (by actually calling `validate_sql(sql,
dialect="tsql")` against real T-SQL text for each one) to already parse
and pass validation, because none of them change the root type or
introduce a disallowed nested type. Every mssql-specific malicious/
obfuscated input tried (standalone `EXEC`, `EXEC xp_cmdshell`, stacked
`sp_executesql`, comment-hidden statement stacking, bracket-quoted
`[sys].[database_principals]`, a cross-database `[master].[sys]
.[databases]` reference, case-mixed `SyS.database_principals`,
`OPENQUERY`) was also already correctly rejected.

Authentication → authorization already happens before any SQL is parsed
(`Depends(require_permission(Permission.ASK))`/`Permission.EXECUTE_SQL`
on `/ask`/`/execute`, resolved by FastAPI before the route body runs).
Cost estimation, execution timeout, and row-cap enforcement are already
covered by `tests/test_query_cost.py`/`tests/test_db_execution.py`.
**Tenant policy is honestly not enforced for the SQL path** — per
`02_TARGET_ARCHITECTURE.md` (Prompt 02), `tenant_id` is plumbed through
`AgentState` but never checked, because there is no real multi-tenant
SQL data model in this codebase to check against. This prompt does not
invent one; that stays a disclosed, pre-existing limitation.

## 2. The two real bugs found

Both were found the same way: by actually running the SQL through the
real function, not by reading the code.

### 2.1 `qualify_table_schema` breaks every CTE query on a schema-qualified connection

sqlglot represents a CTE *reference* (`FROM RecentOrders`) with exactly
the same `exp.Table` node type as a real table reference — it does no
semantic name resolution. `qualify_table_schema` walked every
`exp.Table` node and schema-qualified any without a `db` arg, so:

```sql
WITH RecentOrders AS (SELECT SalesOrderNumber, OrderDate FROM FactInternetSales)
SELECT * FROM RecentOrders
```

became (with `db_schema="employee"`, this project's own real
`HrAutomationDb` case):

```sql
WITH RecentOrders AS (SELECT ... FROM employee.FactInternetSales)
SELECT TOP 1000 * FROM employee.RecentOrders   -- doesn't exist!
```

Every CTE-using query would have failed execution with "Invalid object
name" on that connection — the exact bug class CLAUDE.md's own "Python
3.14 gotchas" section documents as already found and fixed once for
bare table references; CTEs reintroduced it. The same blind spot made
`find_unexpected_table_references` flag the CTE name as "unexpected"
(log noise) and `references_multiple_tables` report `True` for a
single-real-table CTE query (a spurious low-confidence signal on an
otherwise normal result).

**Fix**: `_cte_reference_table_ids(statement)`, one shared helper, used
by all four affected functions (`qualify_table_schema`,
`find_unexpected_table_references`, `references_multiple_tables`,
`find_restricted_column_references`). It returns the `id()` of every
`exp.Table` node that is a pure CTE reference rather than a real table —
processing CTEs in definition order so a later CTE referencing an
earlier one (`WITH a AS (...), b AS (SELECT * FROM a) SELECT * FROM b`)
is also correctly handled, not just the single-CTE case. A CTE alias
that happens to collide with a real table's name is deliberately treated
as a real table when the reference sits *inside* a CTE's own defining
body — the conservative direction (a false "real table" costs nothing;
a false "CTE reference" could hide a genuine restricted-table access).

### 2.2 `enforce_row_limit` doesn't recognize an existing `FETCH NEXT n ROWS ONLY`

`OFFSET n ROWS FETCH NEXT m ROWS ONLY` (T-SQL/ANSI SQL:2008 pagination)
is stored by sqlglot under the same `"limit"` arg key as an ordinary
`LIMIT`/`TOP` clause, but as an `exp.Fetch` node whose row count lives in
`.args["count"]`, not `.expression`. The existing code only ever checked
`.expression`, so an explicit `FETCH NEXT 20 ROWS ONLY` was never
recognized as an existing cap — `current_limit` stayed `None`, and
`enforce_row_limit` silently widened it to `max_rows` (e.g. 20 → 1000)
on every single call. **Not a security issue** (the row cap ceiling was
never exceeded) — a correctness regression: the query returns more rows
than the caller's own explicit `FETCH NEXT` asked for.

**Fix**: when `statement.args.get("limit")` is an `exp.Fetch`, read its
`count` arg instead of `.expression`. `TOP`/`LIMIT` handling (the
`exp.Limit` branch) is completely unchanged — verified by a dedicated
regression test.

## 3. What was built

- `agent/sql_validator.py`: `_cte_reference_table_ids` (new), applied in
  `qualify_table_schema`, `find_unexpected_table_references`,
  `references_multiple_tables`, `find_restricted_column_references`;
  `enforce_row_limit`'s `exp.Fetch`-aware limit detection.
- `tests/test_sql_validator_mssql_dialect.py` (new, 34 tests): every
  construct from §1 locked in as a permanent regression suite, every
  malicious/obfuscated mssql input re-confirmed rejected, and dedicated
  tests for both bugs above (including the chained-CTE case and a
  no-CTE-at-all no-op guard).

## 4. Explicitly out of scope

- Building real tenant-policy enforcement for the SQL path — no data
  model exists to check against (§1); inventing one wasn't asked for and
  would be a materially larger, separate architectural change.
- Any change to the authentication/authorization ordering — already
  correct, verified by inspection, not by assumption.
- Any change to cost estimation, execution timeout, or row-cap mechanics
  beyond the one `exp.Fetch` detection fix (§2.2) — already tested
  elsewhere and unrelated to this prompt's two real findings.

## 5. Testing

Full suite: **2436 tests pass (2402 pre-existing + 34 new), zero
regressions** (`pytest -q`, 125.45s). `ruff check` / `black --check` /
`mypy` on both touched files: clean.

## 6. Security / tenant-isolation / performance review

- **Security**: no change to the validation boundary itself — both
  fixes operate strictly *after* `validate_sql` has already accepted the
  statement (schema-qualification and row-limiting are execution-prep
  steps, not gates). Neither fix loosens any existing check: the CTE fix
  only *removes* a spurious match (a CTE alias being counted as a table
  reference), never adds a way to reach an unvalidated table; the
  restricted-column test (`test_restricted_column_check_still_catches_
  the_real_table_inside_the_cte`) explicitly proves detection of the
  real underlying table is unaffected.
- **Tenant isolation**: unchanged from before this prompt — see §1's
  honest disclosure.
- **Performance**: `_cte_reference_table_ids` is a linear walk over the
  same AST already being walked (no new parse, no new DB round-trip),
  and is a strict no-op (returns immediately) for the overwhelming
  common case of a statement with no `WITH` clause at all.

## 7. Known limitations / remaining risks

- The CTE fix's "inside a CTE's own body" rule doesn't extend to a
  recursive CTE's own self-reference (`WITH RECURSIVE cte AS (anchor
  UNION ALL SELECT ... FROM cte ...)`) beyond what a normal read-only
  treatment already provides — recursive CTEs are rare in this
  project's actual analytical question set and weren't specifically
  exercised.
- No live SQL Server instance with a genuinely non-default schema was
  used to re-verify end-to-end (mocked/unit-level verification only, same
  level as Prompt 04's own disclosed limitation) — verification is
  against the real `sqlglot`/`agent.sql_validator` functions directly,
  not a live database round-trip.

## Recommended next prompt

Prompt 06 (Automated SQL Server Database Discovery) — already underway
in parallel with this one; see `06_DATABASE_DISCOVERY_CONTRACT.md`.
