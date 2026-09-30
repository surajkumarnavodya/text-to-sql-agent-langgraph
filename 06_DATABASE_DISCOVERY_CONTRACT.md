# 06 — Automated SQL Server Database Discovery

Prompt 06 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`05_SQL_SERVER_DIALECT_VALIDATION_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/
TEST/REVIEW/FIX/DOCUMENT** — extends `db/schema_introspection.py` and
`embeddings/schema_indexer.py` (both reused, not duplicated), adds one
new module (`db/row_count_estimate.py`), and additively extends
`POST /schema/refresh`'s response. No existing route, node, or
`Settings` field changed behavior for a caller that doesn't use the new,
optional capabilities.

## 1. Inspection: what already existed vs. the real gaps

`db.schema_introspection.introspect_schema()` was already live,
metadata-only, engine-agnostic (via `sqlalchemy.inspect`), and already
the sole source of schema truth (per CLAUDE.md's own framing) — no
hardcoded/bundled schema file anywhere. `embeddings.schema_indexer` was
already idempotent at the whole-database level (a fingerprint file
skipped re-embedding when nothing changed) and already isolated
per-database failures (`refresh_all_schema_indexes` logs and continues
past one bad connection). Metadata discovery was already separated from
expensive profiling — `db/value_sampling.py` is a distinct, opt-in step.
**None of this needed rebuilding.**

Real gaps, found by reading the actual code (not assumed):

1. **No views** — only `get_table_names`, never `get_view_names`.
2. **No length/precision/scale as structured fields** — baked into a
   display string (`str(col["type"])`) only.
3. **No defaults, no computed/identity flags** — `ColumnInfo` didn't
   capture the `default`/`computed`/`identity` keys SQLAlchemy's
   reflected column dict already provides.
4. **No row-count metadata at all.**
5. **Incrementality was whole-database, not per-table** — any single
   table changing triggered a full `delete_collection` + rebuild-from-
   scratch of *every* table's chunk.
6. **No change/version history** — the fingerprint file stored only the
   current hash, nothing about what changed since the last run.
7. **No per-table failure isolation** — one table's `get_columns()`/
   `get_pk_constraint()` raising killed introspection for the entire
   database, not just that table.
8. **"Tenant-aware"**: per this repo's own already-disclosed state
   (`02_TARGET_ARCHITECTURE.md`), there is no real multi-tenant data
   model for the SQL path. "Tenant-aware" here means "correctly isolated
   per configured database" (already true, `db_name`-keyed collections/
   manifests never cross-contaminate) — stated plainly rather than
   implying broader tenant isolation exists.
9. The acceptance criterion ("a new database can be inventoried without
   manual table-by-table config") **already held** before this prompt —
   configure one connection, run the refresh, every table is discovered
   automatically. Not claimed as new.

## 2. What was built

### `db/schema_introspection.py`

- `ColumnInfo` gained `default`, `is_computed`, `is_identity`, `length`,
  `precision`, `scale` — all sourced from data `Inspector.get_columns()`
  already returns per column (the reflected `type` object's own
  `.length`/`.precision`/`.scale` attributes, and the dict's own
  `default`/`computed`/`identity` keys). No second query. Each defaults
  to `None`/`False` when a dialect doesn't populate it.
- `TableSchemaInfo` gained `is_view` (views are discovered via
  `inspector.get_view_names()`, tagged, and rendered as `CREATE VIEW` —
  `render_ddl`'s new `is_view` parameter) and `row_count_estimate` (see
  below).
- `introspect_schema` gained `include_row_counts: bool = False`
  (opt-in, default off — every existing caller's per-refresh cost is
  unchanged unless it explicitly asks for the extra round trip) and now
  isolates each table/view's own introspection in a `try`/`except`: one
  bad object is logged and skipped, not fatal to the whole database. A
  dialect with no view-reflection support (`NotImplementedError`) is
  treated as "no views," never a reason table discovery fails.

### `db/row_count_estimate.py` (new)

A catalog-only, approximate row count per table/view, per `DB_TYPE`
(mirrors `db/query_cost.py::_STRATEGIES`'s established per-engine-
dict pattern): `pg_stat_user_tables.n_live_tup` (postgresql),
`information_schema.tables.TABLE_ROWS` (mysql),
`sys.dm_db_partition_stats` summed across the heap/clustered-index
partitions (mssql), `ALL_TABLES.NUM_ROWS` (oracle). **Deliberately never
a `SELECT COUNT(*)`** — that's exactly the "expensive profiling" this
prompt's own requirements separate from metadata discovery. Fails open
to `None` on any error, missing stats, or unsupported `db_type`.
Deliberately simpler than `db/query_cost.py`'s thread-based timeout
wrapper — a catalog-only single-row lookup against an already-maintained
statistics view has no realistic pathological-hang scenario the way
compiling a query *plan* does.

Queries `information_schema`/catalog objects directly via hardcoded,
backend-authored SQL — never through `agent.sql_validator.validate_sql`'s
LLM-output allowlist, and not in tension with that validator's own
system-catalog denial: that denial stops an *LLM-generated* query from
digging through catalog metadata in response to a business question;
this is the application's own trusted administrative introspection code,
the same distinction `db/connection.py`'s version/write-privilege queries
already rely on.

### `embeddings/schema_indexer.py`

The old bare-hash cache file is replaced by a small JSON manifest
(`.schema_manifest__<db_name>.json`) that also stores a **per-table**
fingerprint (`get_schema_fingerprint([table])`, reusing the existing
function rather than a second hashing implementation). When the overall
fingerprint changes but a previous manifest exists, `build_index` now
diffs old vs. new per-table fingerprints and does a targeted
`collection.upsert()` for just the added/changed tables plus
`collection.delete()` for removed ones, instead of deleting and
rebuilding the entire collection. A forced re-embed or a genuine first
build (no manifest yet) still takes the simple, always-correct full-
collection path.

`SchemaDiscoveryDiff` (new, frozen dataclass) + `get_last_discovery_diff
(db_name, settings)` read the manifest back for a caller that wants to
report what changed — **without changing `build_index`/
`refresh_schema_index`/`refresh_all_schema_indexes`'s own existing
signatures or return types**. On a genuine first build, every discovered
table is correctly reported as "added" (there's no prior state to diff
against) — the useful signal for an onboarding UI ("discovered 47
tables"), not a quirk suppressed to look tidier.

**No migration step needed**: an old `.schema_hash__<db_name>` file is
simply never read again by the new code (a different filename); the
next build finds no manifest, treats it as a first build (the existing,
always-correct full-rebuild path), and starts writing the new format
from then on.

### `api/schemas.py` / `api/main.py`

`SchemaRefreshResult` gained `view_count`, `added_tables`,
`removed_tables`, `changed_tables`, `last_discovered_at` — all additive,
all defaulting to "nothing to report," so an older client sees no
behavior change. `POST /schema/refresh` populates them from
`get_last_discovery_diff` per database, after the existing
`refresh_all_schema_indexes` call — a database that was skipped (schema
unchanged) correctly reports an empty diff from its last real build.

## 3. Explicitly out of scope

- **A runtime API to register a brand-new database connection** without
  editing `.env`/restarting. `Settings.databases` is loaded once at
  process startup by design (`pydantic_settings.BaseSettings`); making
  that dynamic is a materially larger architectural change than this
  prompt's acceptance criterion requires — flagged as a candidate future
  prompt rather than half-built here.
- **Real tenant-policy enforcement.** Unchanged from
  `05_SQL_SERVER_DIALECT_VALIDATION_CONTRACT.md`'s own disclosure — no
  data model exists to check against.
- **CTE-to-CTE-level or column-level change diffing.** The diff is
  table-granular (matching the chunk granularity `embeddings
  .schema_indexer` already embeds at) — a column added to an otherwise-
  unchanged table correctly shows up as that table being "changed," not
  as a separate column-level entry.
- **Wiring row-count estimates into the LLM prompt.** `row_count_estimate`
  is discovery/onboarding metadata only, never embedded into `ddl` — a
  deliberate boundary, matching `db/value_sampling.py`'s own "sampled
  values are the only data-derived input to `render_ddl`" contract.

## 4. Testing

- `tests/test_schema_introspection.py`: +10 tests — views discovered and
  tagged, PK/FK reflection correctly skipped for views,
  `NotImplementedError` from a dialect with no view support treated as
  "no views," length/precision/scale extraction from real SQLAlchemy type
  objects, default/computed/identity extraction (including the DDL
  rendering the new annotations), per-object failure isolation (one bad
  table doesn't block the rest; every object failing returns `[]`, not a
  raise), and `include_row_counts`'s opt-in wiring (off by default,
  threads the engine's own dialect name through when enabled).
- `tests/test_row_count_estimate.py` (new, 21 tests): all four dialect
  strategies against a mocked `Engine.connect()`, a missing/NULL catalog
  value resolving to `None` (not `0` — "known to be empty" would be a
  false claim), Oracle's table/schema upper-casing, an unsupported
  `db_type` short-circuiting without querying, and `SQLAlchemyError`
  failing open.
- `tests/test_schema_indexer.py`: +11 tests — `build_index`'s **first
  direct unit tests** (previously only exercised indirectly through
  `refresh_schema_index`'s mocked-at-a-higher-level tests): first build,
  skip-when-unchanged, incremental upsert-only-what-changed, delete-of-
  removed-tables, proof the incremental path never calls
  `delete_collection`, forced full rebuild, `get_last_discovery_diff`'s
  None/corrupt-manifest cases, and two-database manifest/collection
  isolation.
- `tests/test_api_schema.py`: existing test's mocks updated to carry
  `is_view` (a real field now); +1 test for the diff fields actually
  reaching the HTTP response.

Full suite: **2479 tests pass (2436 pre-existing + 43 new), zero
regressions** (`pytest -q`, 119.91s). `ruff check` / `black --check` /
`mypy` on every touched/new file: clean (one pre-existing, unrelated
`ruff` finding at `api/main.py:647` — not on a line this prompt touched —
matches CLAUDE.md's already-disclosed baseline).

## 5. Security / tenant-isolation / performance review

- **Security**: `db/row_count_estimate.py` executes hardcoded,
  backend-authored SQL against catalog/statistics views only — never
  interpolates `table_name`/`schema` into a SQL string (bind parameters
  throughout); never executes LLM-generated or user-supplied text. This
  is backend administrative code, structurally separate from
  `agent.sql_validator`'s LLM-output gate, not a bypass of it (§2's own
  framing). No new write path anywhere.
- **Tenant isolation**: every new file/manifest/collection reference is
  `db_name`-keyed, exactly like the pre-existing pattern
  (`test_two_databases_never_share_manifests_or_collections` proves one
  database's change never touches another's manifest or Chroma
  collection).
- **Performance**: the incremental upsert/delete path is the real win
  here — a one-table change on a database with hundreds of tables now
  costs one upsert call instead of a full collection delete + re-embed
  of everything. `include_row_counts` is strictly opt-in (default off)
  specifically because it adds one extra catalog query per table; no
  existing caller's cost changed.

## 6. Known limitations / remaining risks

- No live SQL Server instance with real views/computed/identity columns
  was used to re-verify end-to-end — verification is against real
  SQLAlchemy type objects and a mocked `Inspector`/Chroma layer (same
  level as this series' other prompts' own disclosed limitation).
- Oracle's `ALL_TABLES.NUM_ROWS` is `NULL` until `DBMS_STATS` has run for
  that table — correctly surfaces as `None`, but a freshly-created Oracle
  table with no stats gathered yet will report no row-count estimate at
  all, not a stale/wrong one.
- The discovery diff is computed and stored once per `build_index` call;
  a caller that never calls `POST /schema/refresh` (e.g. only ever hits
  the process-startup refresh) still gets a manifest written, but
  nothing surfaces the diff to a human unless that endpoint (or a future
  caller of `get_last_discovery_diff`) is actually used.

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — unchanged recommendation
from `04_SQL_SERVER_ADAPTER_CONTRACT.md`/
`05_SQL_SERVER_DIALECT_VALIDATION_CONTRACT.md`, still pending explicit
sign-off since it changes a live response shape.
