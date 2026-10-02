# 19 — SQL Server Query Store Performance Intelligence

Prompt 19 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`. One new, SQL-Server-only
module (`db/query_store.py`), additive `recommendation/engine.py` rules,
additive `agent/nodes.py` wiring (gated on the *selected* database's
`db_type`, never a global switch), and additive `config.settings`
surface. No existing node, route, response field, or non-MSSQL code path
changed shape or behavior.

## 1. Inspection: what already existed vs. the real gap

`db/adapter.py`'s `DatabaseCapabilities`/`DatabaseAdapter` already
established the "provider-neutral capability object, one real
implementation wraps existing per-engine functions" pattern this prompt
follows. `db/connection.py::check_write_privileges` already established
the exact shape a permission-gated, best-effort DB introspection check
should take in this codebase: a `checked: bool` (here, `available: bool`)
result, a per-engine dict of queries, and a broad, fail-open `except`
around the whole thing — "insufficient permission" and "feature not
applicable" are deliberately indistinguishable outcomes to every caller,
both just "nothing more to do here." `db/query_cost.py` is this
feature's closest sibling (also SQL-Server-aware among other engines,
also a non-executing, fail-open DB introspection), but is scoped to one
specific *candidate* query right before running it — Query Store's job
is the opposite direction: what has *already* been running, aggregated,
server-wide, over time. `recommendation/engine.py`'s existing
`DATABASE_PERFORMANCE` category (Prompt 17) already had exactly one
evidence source (`db.query_cost.CostEstimate`); grep confirmed zero
existing Query Store/DMV-reading code anywhere in this codebase before
this prompt.

**The one real design decision, made explicitly rather than assumed:**
Query Store's own `query_sql_text` can contain literal values (whatever
a query actually filtered on) whenever forced parameterization isn't
enabled on the target database — a real, not hypothetical, way this
feature could leak sensitive data if built naively. Resolved by never
returning that text at all: every query's text is parsed with `sqlglot`
and every literal replaced with a placeholder (`_mask_literals`) before
truncation, and that masked, bounded preview is the *only* text form
this module ever constructs or returns — not a redaction pass applied
after the fact, a type-level guarantee that raw literal text never
leaves `db/query_store.py` in the first place.

## 2. What was built

### `db/query_store.py` (new)

Four typed Pydantic results: `QueryStoreAvailability`, `QueryStoreQueryStats`,
`QueryStoreRegression`, `QueryStoreFindings` (the aggregate, always
constructible, never a sentinel).

1. **`check_query_store_availability(engine, db_type, settings)`** — the
   mandatory first call. `db_type != "mssql"`, the feature flag off, a
   permission error reading `sys.database_query_store_options`, and Query
   Store genuinely not being enabled on the target database all resolve
   to `available=False` with a distinct, honest `reason` string — never
   an exception, never conflated with each other but all equally "stop
   here."
2. **`get_top_queries`** — "high-cost/repeated patterns," the literal
   requirement: joins `sys.query_store_query`/`_query_text`/`_plan`/
   `_runtime_stats`/`_runtime_stats_interval`, grouped by `query_id`,
   filtered to `execution_count >= Settings.query_store_min_execution_count`
   over `Settings.query_store_lookback_hours`, ordered by total cost
   (avg duration × execution count), capped at
   `Settings.query_store_top_n_queries`. Surfaces execution count,
   avg duration/CPU/logical reads, plan count, and whether a plan is
   forced — plus the query's `query_hash` as a hex fingerprint and its
   literal-masked, truncated text preview.
3. **`get_regressions`** — "regressed patterns": splits the lookback
   window into a "recent" window (`Settings
   .query_store_regression_recent_hours`) and everything before it (the
   baseline), aggregates each independently by `plan_id`, and flags a
   query only when *both* windows individually clear the minimum
   execution-count bar *and* the recent average duration is at least
   `Settings.query_store_regression_factor`× the baseline average —
   entirely self-referential (a query vs. its own history), never one
   query compared against another.
4. **`get_query_store_findings`** — the single entry point: checks
   availability first, and only runs the two DMV joins when it's `True`
   — an unavailable Query Store costs exactly one cheap catalog-view
   read, never two failed joins.
5. **Zero new execution path.** Every DMV read goes through `db.execution
   .execute_readonly_sql` — the exact timeout-enforced, row-capped
   executor `agent.nodes.execute_sql_node`/`POST /execute` already use
   for everything else, not a second mechanism.
6. **`get_cached_query_store_findings`/`clear_query_store_cache`** — a
   small, lock-protected, in-process TTL cache keyed by database name
   (`Settings.query_store_refresh_interval_seconds`, default 5 minutes),
   mirroring `observability.metrics.PerformanceMetrics`'s own "cheap,
   single-process, resets on restart" posture — Query Store reflects
   server-wide historical activity, not anything specific to one
   question, so re-querying both DMV joins on every single `/ask` call
   would be pure waste.

### `recommendation/engine.py` (two new rules, both `DATABASE_PERFORMANCE`)

`RecommendationInputs.query_store_findings: QueryStoreFindings | None` —
feeds `_query_store_high_cost_candidates` (fires when a top query's
`avg_duration_ms` clears `Settings
.query_store_high_duration_ms_threshold`) and
`_query_store_regression_candidates` (one candidate per already-validated
regression — this rule only renders what `get_regressions` already
proved, the identical "adapt, don't recompute" posture every other rule
in this module follows). Both reuse the **existing**
`RecommendationCategory.DATABASE_PERFORMANCE` — "feed findings into the
common recommendation contract" literally, not a new category invented
for this prompt. Both gated on `inputs.query_store_findings.availability
.available` before either rule runs at all. Every claim/evidence string
is built only from already-masked previews, numeric stats, and the hash
fingerprint — never raw query text (verified directly in
`tests/test_recommendation_engine.py`'s own literal-leak regression
test).

### `agent/nodes.py` (additive wiring)

`_query_store_findings_for_state(state, settings)` — resolves the
request's own selected database (`get_connection` + `get_read_only_engine`,
the same calls `_restricted_column_hits_in_sql` already makes), checks
its `db_type`, and only ever calls into `db.query_store` when it's
`"mssql"` — a postgres/mysql/oracle-routed question's connection lookup
is cheap (no new connection opened) but **never reaches `db.query_store`
at all**, the structural (not just documented) enforcement of "does not
break other database providers." Wired into
`generate_recommendations_node` alongside the existing `cost_estimate`/
`performance_snapshot` inputs — no new node, no new graph edge. Fails
open on any error (a Query Store read failure drops only Query-Store-
sourced candidates, never the whole recommendation set).

### `config/settings.py` (10 new settings)

`enable_query_store_insights` (default `True` — a no-op for every
non-mssql `DB_TYPE` regardless), `query_store_lookback_hours` (24h),
`query_store_top_n_queries` (10), `query_store_min_execution_count` (5),
`query_store_regression_factor` (1.5×), `query_store_regression_recent_hours`
(1h), `query_store_timeout_seconds` (5s), `query_store_refresh_interval_seconds`
(300s), `query_store_preview_max_chars` (300), and
`query_store_high_duration_ms_threshold` (1000ms). A new cross-field
validator (`_validate_query_store_window_ordering`) mirrors
`_validate_cost_threshold_ordering`'s own shape: the recent window must
be strictly shorter than the full lookback window, or there would be no
baseline window left to compare against.

### Least privilege (documented, not newly enforced)

Reading Query Store's DMVs requires the connected principal to hold
`VIEW DATABASE STATE` (SQL Server 2016–2019) or `VIEW DATABASE
PERFORMANCE STATE` (SQL Server 2022+) on the target database — a
database-scoped permission, never `VIEW SERVER STATE`/sysadmin. This is
an **optional** grant on top of this project's existing read-only
`DB_USER` role; a `DB_USER` without it simply never sees Query Store
insights (`available=False`), with everything else in the application
working exactly as before.

## 3. Testing

`tests/test_query_store.py` (25 tests): literal masking (string/numeric/
multiple literals, `None`/empty input, unparseable SQL, truncation),
availability (non-mssql, feature flag off, available/not-enabled/never-
configured states, permission-denied and timeout both failing open),
`get_top_queries`/`get_regressions` (correct aggregation/fingerprinting/
masking, fail-open on a query error), `get_query_store_findings`
(unavailable skips both DMV reads entirely — proven via an
assertion-raising mock — available runs both), and the TTL cache (reuse
within the window, independent cache keys per database, a fresh query
after expiry).

`tests/test_recommendation_engine.py` (+9 tests): both new rules'
supported/unsupported cases, an unavailable-Query-Store producing
nothing, a `None` findings being a no-op, and an explicit proof that a
masked preview containing a placeholder never reintroduces a literal
into the rendered recommendation text.

`tests/test_agent_nodes.py` (+11 tests, new
`TestGenerateRecommendationsNodeQueryStore` class): the literal
acceptance criterion, proven structurally — a non-mssql database (this
test file's own default fixture) never calls `db.query_store` at all
(an assertion-raising mock would fail the test if it were ever called);
an mssql database does produce a Query Store–sourced recommendation; the
feature flag being off also never calls it; and a Query Store failure
fails open without losing the rest of the recommendation set.

Full regression suite reran clean after every fix: 3308/3308 backend
tests passing (up from 3272 before this prompt — 36 new tests), 0 ruff
findings, 0 bandit findings, `black --check` clean, mypy clean on every
touched non-test file.

## 4. Known, disclosed limitations (not oversights)

- **Query Store reflects server-wide, historical activity, not
  specifically "this question's own query."** A high-cost/regressed
  query surfaced in a recommendation may or may not be the one the
  current question just executed — disclosed in every such
  recommendation's own `limitations`, never implied otherwise.
- **The TTL cache is single-process, in-memory, and resets on
  restart** — the identical, already-disclosed limitation
  `observability.metrics.PerformanceMetrics` carries for the identical
  reason (no shared store like Redis is part of this application's
  current architecture).
- **Regression detection is a statistical comparison, not a root-cause
  diagnosis** — it identifies *that* a query regressed and by how much,
  never *why* (a plan change, stale statistics, a data-volume shift);
  every regression recommendation's own `action` text says so.
- **No live SQL Server instance with Query Store actually enabled was
  available to verify the DMV queries against real data in this
  pass** — every test here mocks `db.execution.execute_readonly_sql` at
  the dispatch level (the same, already-established posture
  `tests/test_query_cost.py` already discloses for its own MSSQL
  SHOWPLAN strategy). The DMV join shapes are written directly against
  Microsoft's own documented Query Store catalog-view schema, not
  live-verified.
- **No frontend changes in this pass** — these recommendations surface
  through the existing `AskResponse.recommendations`/governed-
  recommendation-store surface Prompts 17/18 already built; nothing new
  was added to the API or UI for this prompt specifically.
