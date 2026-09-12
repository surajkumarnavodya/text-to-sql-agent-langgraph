# CLAUDE.md — Project Context for Claude Code Sessions

This file orients any future Claude Code session working in this repo. Read this
before making changes.

## What this project is

A Text-to-SQL dashboard connected to one or more real, user-configured
databases. A user types a natural-language question in Streamlit, a
LangGraph agent turns it into SQL against the configured database (schema
retrieved via ChromaDB, embedded from **live schema introspection** — not a
hardcoded sample — so only relevant tables are shown to the LLM), the SQL is
validated (SELECT-only allowlist), executed read-only, and the result is
rendered as a table + auto-picked Plotly chart. The LLM runs locally via
Ollama — no network calls for the LLM, no API keys required for that part.
Database connectivity is fully config-driven via `.env`; there is no
hardcoded connection string, host, or schema anywhere in the codebase.

**Multiple databases:** `DB_CONNECTIONS` in `.env` can list more than one
named connection (`config.settings.DatabaseConnectionConfig`, one full
`DB_<NAME>_*` field set per name) instead of the single legacy `DB_*` block.
When more than one is configured, `retrieve_schema_node` auto-routes each
question to whichever database's schema looks most relevant
(`embeddings.retriever.select_database`) before generating SQL — there is
no manual database picker anywhere in the UI/API. A plain single-database
`.env` (the common case, and everything this file describes elsewhere
unless multi-database is called out explicitly) still works unchanged: it's
internally treated as one connection named `"default"`. See "Multi-database
auto-routing" under Key design decisions below.

**History note:** the project originally shipped with a bundled sample
DuckDB e-commerce database for demo purposes. That was fully removed (by
explicit user decision, no fallback/demo mode kept) in favor of connecting
only to a real database via SQLAlchemy. If you see references to DuckDB,
`db/schema.sql`, or `scripts/seed_db.py` anywhere (docs, old branches,
stale comments), they're leftover from that phase and should be treated as
wrong, not as a parallel supported mode.

**Multi-source (optional, off by default):** `ENABLE_MULTI_SOURCE_ROUTER=true`
switches `ui/app.py`/`api/main.py` from calling `agent.graph.run_agent`
directly to calling `agent.orchestrator.graph.run_orchestrated`, which adds
a router in front of the SQL pipeline and can fan a question out to up to
three more sources: an "documents" and a separate, more access-sensitive
"policies" PDF collection (agentic RAG, native SQL Server `VECTOR` storage),
and live web search (Tavily). The SQL pipeline itself is never modified by
any of this — see "Multi-source orchestration" under Key design decisions
below, and `docs/MULTI_SOURCE_GUIDE.md` for how to configure each source.

## Tech stack

| Concern | Choice |
|---|---|
| LLM runtime | Ollama, default model `llama3.1:8b` (swap via `.env` / `config/settings.py`, e.g. `sqlcoder`, `duckdb-nsql`) |
| Config / validation | Pydantic v2 — `config/settings.py`'s `Settings` is a `pydantic_settings.BaseSettings` (env-var-driven, `Field`/`Literal`-validated); secrets are `pydantic.SecretStr`; request/response models (`api/schemas.py`) and several boundary dataclasses were converted to `BaseModel` too. See "Pydantic-based configuration and validation" below |
| Orchestration | LangGraph — explicit state machine, not a black-box agent. Two graphs: `agent/graph.py` (the SQL pipeline, always present) and, when multi-source is enabled, `agent/orchestrator/graph.py` (router + fan-out, sitting in front of it) |
| Schema retrieval | ChromaDB (persisted locally) — embeds table DDL synthesized from live introspection, retrieves top-k relevant tables per question |
| Database | User's own — PostgreSQL, MySQL, SQL Server, or Oracle, via SQLAlchemy. Config-driven (`DB_TYPE` + connection params in `.env`), pluggable per `db.connection.SUPPORTED_DB_TYPES`. One or more named connections (`DB_CONNECTIONS` in `.env`); the agent auto-routes each question to the right one when more than one is configured |
| SQL parsing/validation | sqlglot — parses generated SQL and checks statement type against an allowlist, in the dialect matching `DB_TYPE` |
| Document/policy RAG | SQL Server 2025+/Azure SQL native `VECTOR` column type (`rag/store.py`) — a dedicated connection (`RAG_STORE_CONNECTION_STRING`), separate from `DB_CONNECTIONS`. Optional, off by default (`ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG`) |
| Web search | Configurable provider (`search/web_search.py`), Tavily implemented today. Optional, off by default (`ENABLE_WEB_SEARCH` + `WEB_SEARCH_API_KEY`) |
| UI | Streamlit + Plotly. `ui/app.py` (chat) + `ui/pages/1_Knowledge_Sources.py` (PDF upload/management, Streamlit's native multipage convention) |
| Python | 3.11 is the target per project spec. **This machine only has 3.14 installed** (no 3.11 on PATH via `py -0p`) — the venv was created against 3.14. If a future session hits a wheel-availability issue for a pinned dependency, that's why (see "Python 3.14 gotchas" below for two real ones already hit and fixed). Re-run `py -0p` to check if 3.11 has since been installed and consider recreating `.venv` against it if so. |

### Python 3.14 gotchas already hit (fixed, but worth knowing about)

- **`pandas==2.2.3` segfaults** building a `DataFrame` from any row data
  containing a raw `datetime.datetime` value (a real access violation deep
  in `pandas.core.arrays.datetimes._construct_from_dt64_naive`, reproduces
  in two lines with no app code involved) — this predates real Python 3.14
  wheels. Fixed by pinning `pandas==2.3.3` (see `requirements.txt`'s
  comment). If a future dependency bump reintroduces an old pandas pin,
  this is the symptom to watch for: the Streamlit process dies with
  `Segmentation fault`/`Windows fatal exception: access violation` right
  after a query with a date/datetime column succeeds, not a normal Python
  exception.
- **SQL Server non-default-schema execution**: an engine resolves an
  unqualified table name against the *connecting user's own default
  schema*, not `DB_<NAME>_SCHEMA` — a database whose tables all live under
  a non-default schema (this project's own `HrAutomationDb` example:
  tables under `employee`, connection's default schema `dbo`) fails every
  query with "Invalid object name" otherwise. Fixed by
  `agent.sql_validator.qualify_table_schema`, applied only at the
  execution step (not to `state["sql"]` itself, which stays bare-named for
  the schema-anomaly/restricted-column checks and the UI). Unrelated to
  Python 3.14, but discovered in the same session — worth knowing if you
  configure a database with a non-default schema.

## Folder conventions

- `config/` — all tunables (model name, Ollama host, DB connection fields,
  Chroma path, row limit, timeout, max retries) live in `config/settings.py`,
  sourced from `.env`. Never hardcode a model name, path, or connection
  detail anywhere else — import from here. `Settings` (a
  `pydantic_settings.BaseSettings` — see "Pydantic-based configuration and
  validation" below) is a passive config bag; it validates *individual*
  malformed values (e.g. `DB_PORT=abc`) at load time but does not require DB
  fields to be present just to import the module — `db/connection.py`
  validates *combinations* (e.g. "DB_TYPE set but DB_HOST missing") at the
  point something actually tries to connect.
- `db/` — `connection.py` owns the SQLAlchemy engine lifecycle: builds the
  connection URL from config (`build_connection_url`), exposes a cached
  `get_engine()`/`get_read_only_engine()`, and `test_connection()` (a
  `SELECT 1` round-trip with best-effort failure classification — auth,
  host-unreachable, db-not-found, driver-missing, timeout, unknown). Every
  one of these accepts either the legacy global `Settings` or one specific
  `Settings.databases` entry (a named connection — see `DbConnectionLike`),
  so the same functions serve both a plain single-database setup and a
  multi-database one; `get_connection(settings, name)` looks one up by
  name. `schema_introspection.py` is the **sole source of truth** for schema
  shape: `introspect_schema(engine, schema)` uses SQLAlchemy's `Inspector`
  to pull real tables/columns/types/FKs and synthesizes a compact
  `CREATE TABLE`-style DDL string per table (for LLM prompt consistency,
  not necessarily valid executable DDL). `get_schema_fingerprint()` hashes
  that output for Chroma cache invalidation. `execution.py` owns read-only
  SQL execution mechanics (`execute_readonly_sql` — background-thread
  timeout enforcement plus a `fetchmany()` row cap), shared by
  `agent.nodes.execute_sql_node` and `ui/app.py`'s "Confirm and Run" path —
  a pure database-execution concern with no LangGraph dependency, so it
  lives here rather than in `agent/`.
- `embeddings/` — `schema_indexer.py`'s `build_index(tables, db_name, ...)`
  takes already-introspected `TableSchemaInfo` objects (not a file, not an
  engine) and embeds them into **that database's own Chroma collection**
  (never a shared one — see `get_collection`'s docstring for why), keyed by
  a hash of the introspected schema so re-embedding only happens when that
  database's schema actually changed. `refresh_schema_index(engine, db_name,
  settings, force)` is the single introspect → sample → embed pipeline for
  one database; `refresh_all_schema_indexes(settings, force)` runs it for
  every configured database and is what `scripts/build_embeddings.py` and
  `ui/app.py`'s schema initialization actually call. `retriever.py`'s
  `retrieve_relevant_schema(question, db_name, ...)` does top-k similarity
  search over one database's table-level DDL chunks — unchanged in shape
  from before, just explicitly scoped to one database's collection now.
  `retriever.py`'s `select_database(question, settings)` is the
  auto-router: with one configured database it short-circuits immediately
  (no behavior change); with several, it compares each database's own
  best-matching table and picks the winner, before per-table retrieval
  runs. See "Multi-database auto-routing" below. `golden_examples.py` is a
  sibling collection on the same Chroma client/persist dir, one per
  configured database (`f"golden_examples__{db_name}"`) — see "Golden-dataset
  feedback loop" below.
- `agent/` — LangGraph nodes live in `nodes.py`, one function per node, each
  taking and returning `AgentState` (defined in `state.py`). `graph.py`
  wires them together and compiles the graph. `sql_validator.py` is the
  security boundary — see below. `AgentState["selected_database"]` records
  which configured database a question was auto-routed to (set once by
  `retrieve_schema_node`); the sqlglot dialect used for validation/cost
  estimation is resolved from *that* database's `db_type`
  (`db.connection.get_connection(settings, selected_database)` +
  `get_sqlglot_dialect()`) — never a single hardcoded/global engine.
  `complexity.py` is the source of the adaptive retry budget and the
  planning/review gate (`detect_complexity_signals`/`compute_max_retries`,
  see "Agentic query planning + plan-conformance self-correction" below) —
  a cheap regex heuristic, no LLM call, computed once by `run_agent()` per
  question.
- `agent/orchestrator/` — the multi-source router, one level up from
  `agent/`'s own SQL-only graph, never the other way around: `nodes.py`
  (`router_node`/`classify_sources` — LLM classification only when 2+
  sources are configured, else a zero-cost short-circuit; `sql_subgraph_node`,
  which calls `agent.graph.run_agent` as an opaque function, never
  reimplementing any of it; `document_rag_node`/`policy_rag_node`/
  `web_search_node`; `synthesis_node`, a pure pass-through unless 2+ sources
  actually fired), `state.py` (`OrchestratorState` *extends* `AgentState`,
  never replaces it — see "Multi-source orchestration" below), `graph.py`
  (`run_orchestrated`, the single entry point `ui/app.py`/`api/main.py`
  call in place of `agent.graph.run_agent` directly).
- `rag/` — document/policy agentic RAG, one implementation shared by both
  the "documents" and "policies" collections (parameterized by collection
  name, not two near-duplicate modules): `store.py` (SQL Server native
  `VECTOR` storage, a dedicated connection — see `Settings.
  rag_store_connection_string`'s docstring for why it's never one of
  `DB_CONNECTIONS`), `ingestion.py` (PDF extract via `pypdf` → chunk →
  embed → store, flags likely-scanned pages as OCR-needed rather than
  silently indexing nothing), `embedding.py` (shared embedding function,
  reused by ingestion *and* retrieval so both live in the same vector
  space), `retriever.py` (top-k retrieval + LLM relevance grading + query
  rewrite), `graph.py` (`build_rag_subgraph`/`run_rag` — retrieve → grade →
  rewrite/retry (bounded) → generate-with-citations, or an
  insufficient-information fallback; a "policies" chunk carrying a
  `sensitivity_category` is never summarized into an answer — this app has
  no per-user authorization system, so generation fails closed instead).
- `search/` — live web search, `web_search.py`: a provider-name → call-
  function dict (`SUPPORTED_SEARCH_PROVIDERS`), shaped exactly like
  `db.connection.SUPPORTED_DB_TYPES` — swapping `WEB_SEARCH_PROVIDER` is a
  `.env` change, not a code change. Only `tavily` is implemented today.
- `ui/app.py` — the only file that imports Streamlit for the chat page. It
  imports `agent.orchestrator.graph.run_orchestrated` and calls it (which
  is itself a pure pass-through to `agent.graph.run_agent` unless
  `ENABLE_MULTI_SOURCE_ROUTER` is set); it does not contain any agent logic
  itself. Manual "Confirm and Run" button gates *displayed* execution — see
  "SQL is untrusted output, always" below for the nuance around the agent's
  own internal self-correction executions; it validates and executes
  against whichever database the displayed SQL was actually routed to
  (`state["selected_database"]`), not a re-guessed one. Every SQL-specific
  render (schema context, editable SQL box, Confirm and Run, results table)
  is gated on `"sql"` actually being one of `state["sources_used"]` (or
  that key being absent entirely, which implies the router is off) — a
  pure web/document/policy answer renders through a separate path instead
  (`_render_sources_used`/`_render_source_answer`), never through the
  SQL-specific one. On startup, `test_connection()` is checked for every
  configured database; a setup screen and `st.stop()` only happen if *all*
  of them fail (one down database doesn't block the others). The sidebar
  exposes per-database connection status, a manual re-test, a manual schema
  refresh (all databases), a schema browser grouped by database, and which
  database the most recent question was routed to.
- `ui/theme.py` — the dark/light theme system shared by both Streamlit
  pages (multipage apps don't share a layout wrapper, so each page calls
  this independently rather than duplicating CSS). `render_theme_toggle()`
  (a sidebar `st.toggle`) writes `st.session_state.theme_mode`, which
  persists across page navigation within a session;
  `inject_theme_css()` reads it back (via `get_theme_mode()`, defaulting
  to `"light"` — flipping the toggle never changes the default experience
  for anyone who doesn't touch it) and emits the matching `<style>` block.
  Server-side conditional CSS, not a client-side JS switch — Streamlit
  reruns the whole script on the toggle click, so the next render just
  picks the other variable set. Deliberately doesn't touch
  `.streamlit/config.toml` (loaded once at server startup, can't be
  flipped per-session at runtime) — instead overrides Streamlit's actual
  rendered surfaces directly via `data-testid` selectors, the same
  technique the original light-only styling already used. Known,
  disclosed gap: Streamlit's own built-in chrome (the top header bar, its
  native hamburger "Settings" menu) isn't reachable by this CSS and won't
  flip with the toggle.
- `ui/pages/1_Knowledge_Sources.py` — PDF upload + management for the
  "documents"/"policies" collections (Streamlit's native multipage
  convention: any script under `ui/pages/` becomes its own page
  automatically, no change to `ui/app.py` needed). Two tabs, each showing a
  clear "not configured" message rather than a broken upload form if its
  `ENABLE_*_RAG` flag or `RAG_STORE_CONNECTION_STRING` isn't set. A policy
  upload gets an optional sensitivity-category selector (compensation/
  disciplinary/legal/none — see `rag/store.py`'s `SensitivityCategory`).
- `api/` — `main.py`'s FastAPI app is the programmatic/scripted-access
  surface alongside the Streamlit UI, same underlying graph either way (see
  "Multi-source orchestration" above). A `lifespan` context manager warms
  every process-lifetime singleton at startup rather than on whichever
  request happens to arrive first — every configured database's read-only
  engine, the cached Ollama client, and the compiled SQL/orchestrator
  LangGraph graph(s) — and stashes them on `app.state` for
  discoverability, though request handling itself still reaches them
  through the same cached module-level functions `ui/app.py`/
  `eval/runner.py` use, not through `app.state` (see "Process-lifetime
  singletons" below). Two global exception handlers
  (`@app.exception_handler(AgentError)` /
  `@app.exception_handler(Exception)`) are the last-resort net ensuring no
  response body ever contains a raw internal exception string — every
  known failure path already uses `.safe_message` locally (see "Centralized
  exception handling" below), so in normal operation these only fire for a
  genuinely new/forgotten call site, not the common case. `/ask` accepts an
  optional `session_id` (`AskRequest.session_id`), generating one via
  `uuid4()` when omitted, and always echoes it back in `AskResponse
  .session_id` — a pure correlation token today (the API stays fully
  stateless, per `ConversationExchangeIn`'s docstring), but the identifier
  future server-side conversation state (the multi-source router keeping
  context coherent across sources) will key off of.
- `scripts/` — standalone entry points: `test_db_connection.py` (verify
  `.env` before booting anything else — prints pass/fail, DB version, table
  count, or a classified readable error, per configured database),
  `build_embeddings.py` (introspect + embed every configured database, with
  `--force`), `integration_test.py` (manual, requires real database(s), not
  part of the pytest suite — see `tests/` below; also demonstrates routing
  when 2+ databases are configured), `run_benchmark.py` (the Text-to-SQL
  benchmark runner — see `eval/` below; also manual/real-DB-required, not
  part of the pytest suite), `build_user_guide_pdf.py` (rebuilds
  `docs/User_Guide.pdf` from `USER_GUIDE.md` via `reportlab` — the actual
  source of truth for that PDF; re-run after every `USER_GUIDE.md` edit.
  Previously a standalone binary with no checked-in source at all, three
  revisions hand-authored outside the repo — this replaced that with a
  reproducible build, closing a real doc-drift gap
  `docs/PRODUCTION_READINESS_REPORT.md` had flagged).
- `eval/` — the Text-to-SQL benchmark: `schema.py` (dataset + result data
  model), `dataset_loader.py` (loads `eval/benchmark/*.yaml`),
  `evaluators.py` (grades one case — **execution-accuracy first**: gold
  SQL is executed live and the agent's actual result set is compared
  against it, never SQL-text similarity alone), `metrics.py` (reduces
  graded results into the named benchmark metrics), `runner.py` (drives
  `agent.graph.run_agent` for every case), `reporting.py` (markdown report
  + JSON baseline serialization), `regression.py` (compares a run against
  `eval/baselines/latest.json`). `eval/benchmark/*.yaml` holds the actual
  cases, split by difficulty/category; `eval/eval_questions.yaml` and
  `scripts/run_eval.py` are the superseded predecessor, kept unmodified —
  see both files' deprecation notes. Each case's `database:` field is
  currently an inert descriptive label (e.g. `AdventureWorksDW2025`), not a
  `Settings.databases` connection name — the benchmark runner resolves one
  engine/dialect globally, same as before multi-database support existed.
  Routing the benchmark itself per-case is a known, deliberately
  out-of-scope follow-up (see the multi-database auto-routing design note
  below).
- `tests/` — pytest, all fully mocked, no real DB or Ollama required.
  `conftest.py`'s autouse `_isolate_settings_from_real_environment` fixture
  strips every env var one of `Settings`'s fields could read before each
  test, so a test building `Settings(**partial_kwargs)` never silently
  inherits this developer's own real `.env` (see "Pydantic-based
  configuration and validation" above for why that isolation is needed at
  all now). Mirrors package names (`test_sql_validator.py`, `test_connection.py`,
  `test_schema_introspection.py`, `test_schema_retriever.py`,
  `test_agent_nodes.py` — including `TestPlanQueryNode`/`TestReviewSqlNode`,
  `TestRetrieveGoldenExamplesNode`, and the nested-aggregate
  `TestValidateSqlRejectsNestedAggregates` cases,
  `test_complexity.py` (the adaptive-retry-budget heuristic, including the
  "top N *by* metric" false-positive regression case),
  `test_llm_client_planning.py` (the plan/review response parsers'
  fail-open contracts, and the golden-examples prompt-block ordering), and
  `test_golden_examples.py` (the golden-dataset store itself — id
  determinism/upsert idempotency, similarity filtering, fail-open on a
  Chroma error) — plus `test_db_router.py` (the multi-database
  auto-router, `embeddings.retriever.select_database`, and
  `retrieve_schema_node`'s retry-reuses-the-same-database contract),
  `test_orchestrator.py` (source availability, LLM classification parsing +
  fallback, single-source short-circuit, multi-source fan-out/synthesis,
  and the `ENABLE_MULTI_SOURCE_ROUTER`-off pass-through guarantee — all
  mocked at the `rag.llm.call_ollama`/`rag.graph.run_rag`/
  `search.web_search.web_search` seams, no real SQL Server/Tavily/Ollama
  needed), and `test_eval_*.py` for the benchmark framework's own logic
  (dataset loading, grading, metrics, regression detection — not a live
  run, which stays manual like `run_benchmark.py` itself). `rag/` and
  `search/` don't yet have their own fully-mocked unit test files (see
  "Known gaps" below) — they were verified against this project's real
  SQL Server/Tavily/Ollama instances instead during development.

## Key design decisions

### Self-correcting retry loop (LangGraph)
The full graph (`agent/graph.py`) is eleven nodes, not four:
`sanitize_input → classify_followup → retrieve_schema →
retrieve_golden_examples → plan_query → generate_sql → review_sql →
validate_sql → estimate_cost → execute_sql → generate_insight`. On a review, validation, cost-estimate, or execution
failure, a conditional edge routes back to `generate_sql` (or, for a
"missing reference" execution error, back to `retrieve_schema`) with the
error message appended to the state's history, so the LLM sees what went
wrong and can correct itself. Capped at `MAX_RETRIES = 3`
(`config/settings.py`) as a base, but not a single flat number in
practice — `agent/complexity.py::compute_max_retries` widens it by up to
`COMPLEX_QUERY_MAX_RETRY_BONUS` (default 2) extra attempts for a question
whose text matches a "harder than usual" signal (top-N-per-group phrasing,
year-over-year/period growth, several metrics at once), computed once by
`run_agent()` and stored in `state["max_retries"]` — every retry-vs-give-up
check reads that, not the raw setting. After the budget is exhausted, the
graph ends in a terminal `failed` state and the UI surfaces the last error
rather than looping forever. This is the interview-relevant piece: it's a
small explicit state machine, not a ReAct-style free-form agent,
specifically so the retry/error-feedback path is inspectable and boundable.
See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full per-node
walkthrough and the complete retry-routing table.

### Agentic query planning + plan-conformance self-correction
`plan_query_node` (between `retrieve_schema` and `generate_sql`) and
`review_sql_node` (between `generate_sql` and `validate_sql`) are a
decompose-then-check pair, gated by the exact same complexity signals that
widen the retry budget above (`state["complexity_signals"]`) and by
`ENABLE_QUERY_PLANNING` (default `true`). For an ordinary question that
matches no signal, both nodes are a pure pass-through — zero LLM calls,
zero added latency, identical behavior to before this feature existed. For
a question that does match, `plan_query_node` makes one LLM call producing
a short ordered plan (grouping, metrics, filters, and an explicit call-out
when a top-N-per-group ranking needs `ROW_NUMBER()`/`RANK()` instead of
`TOP`/`LIMIT` + `GROUP BY`, or a period-over-period comparison needs
`LAG()`/`LEAD()` instead of a nested aggregate), which is then injected
into every `generate_sql` attempt's prompt for that question.
`review_sql_node` makes a second LLM call checking the *generated* SQL
against that same plan (`PASS`/`FAIL: <reason>`); a `FAIL` feeds the
critique back into another `generate_sql` attempt, sharing the same
`retry_count`/`state["max_retries"]` budget as every other retryable
failure — not a second, unbounded loop. Both nodes fail open on an
unreachable Ollama server or an unparseable response (log it, proceed as
if there were no plan/pass the review) — this feature is an accuracy aid,
never a reason a question can't be answered. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#2-retry--self-correction-semantics)
for the full reasoning.

### Golden-dataset feedback loop
`retrieve_golden_examples_node` (between `retrieve_schema` and
`plan_query`) looks up human-approved (question, SQL) pairs from a
per-database ChromaDB collection (`embeddings/golden_examples.py`,
`f"golden_examples__{db_name}"` — deliberately modeled on
`embeddings/schema_indexer.py`'s pattern rather than `rag/store.py`'s
SQL-Server-`VECTOR` one, since the core pipeline already depends
unconditionally on a local Chroma client and this reuses the exact same
embedding runtime already resident in the process, no new optional
subsystem). Gated by `ENABLE_GOLDEN_EXAMPLES` (default `true` — safe even
with an empty store, since retrieval on an empty/missing collection just
returns no examples) and `GOLDEN_EXAMPLES_MIN_SIMILARITY` (default 0.75 —
a poor match is withheld entirely rather than injected, since a
misleading example is worse than none). When one or more examples clear
the threshold, they're injected into every `generate_sql` attempt's
prompt (`agent.llm_client._build_golden_examples_block`) as reference-only
few-shot material, on top of the static `_QUERY_PATTERNS_BLOCK` — framed
as DATA, never instructions, the same security posture every other
per-question prompt block already has. Fails open exactly like
`plan_query_node`/`review_sql_node` above: a disabled flag, an
unreachable/empty store, or a lookup error all resolve to "no examples,"
never a reason a question can't be answered.

Examples are added only via the UI's explicit thumbs-up feedback
(`ui/app.py`, `st.feedback("thumbs")`, shown once a "Confirm and Run"
result is genuinely confirmed successful) — never automatically, and
never from the API today (a natural, easy follow-up, not built yet). The
SQL saved is `QueryHistoryEntry.confirmed_sql` (the *exact* SQL actually
executed), not the agent's original draft — the user may have edited the
SQL box before confirming, and the corrected version is exactly what's
worth remembering. Saved with a deterministic id
(`embeddings.golden_examples._example_id`, a hash of database+question+SQL)
so re-clicking the widget upserts the same document rather than
accumulating duplicates. Both the UI and the API benefit from retrieval
automatically, since both call the same underlying `agent.graph.run_agent`
graph.

### Nested-aggregate detection (validator + execution backstop)
A real, reproduced failure: a local model asked for something like
"year-over-year growth" reliably reaches for `AVG(CASE WHEN ... THEN
SUM(x) ELSE 0 END)` — an aggregate nested inside another aggregate, which
every supported engine (`mssql`/`postgres`/`mysql`/`oracle`) rejects.
`agent/sql_validator.py::_find_nested_aggregate` walks the `sqlglot` AST
and catches this shape statically, before the query ever reaches the
database (`violation_type="nested_aggregate"`, retryable, not a
`SAFETY_VIOLATION_TYPES` failure). `agent/error_classification.py`'s
`ExecutionErrorCategory.AGGREGATE_NESTING` is the execution-time backstop
for whatever the static check misses (e.g. a dialect-specific aggregate
`sqlglot` doesn't classify as `exp.AggFunc`). Both retry categories get a
targeted rewrite hint (`agent.llm_client._ERROR_CATEGORY_HINTS`) pointing
the model at a pre-aggregating CTE or a window function instead of nesting
the aggregate calls. The generation system prompt also carries a standing
rule against this shape plus two few-shot patterns (top-N-per-group via
`ROW_NUMBER()`, period-over-period via `LAG()`) so the mistake is less
likely in the first place, not just caught after the fact.

### Schema scoping (why ChromaDB at all)
For a large real schema, dumping every table's DDL into the prompt burns
context and increases hallucinated joins on irrelevant tables. Each table's
synthesized DDL (from live introspection) is embedded as one chunk; at query
time we retrieve the top-k (`SCHEMA_TOP_K`, default 4) most relevant tables
and only inject those into the generation prompt. This matters a lot more
now than it did with the old bundled 5-table sample schema — a real
production database can easily have hundreds of tables, which is exactly
the case this code path is written for.

### Multi-database auto-routing
`DB_CONNECTIONS` in `.env` can name more than one database
(`config.settings.DatabaseConnectionConfig`, collected into
`Settings.databases`). Each configured database gets its **own** Chroma
collection (`embeddings.schema_indexer.get_collection`'s `db_name` param) —
never a shared one, because FK-bridge/keyword-match expansion in
`embeddings/retriever.py` only makes sense within one database's own
foreign-key graph, and a shared collection would also risk table-name
collisions between two databases that happen to share a table name.

Routing itself (`embeddings.retriever.select_database`) is a cheap,
separate pre-step: with one configured database it short-circuits
immediately (no Chroma query, no behavior/latency change for a plain
single-database setup — the overwhelmingly common case); with several, it
queries every database's collection for its own single best-matching table
(`n_results=1`) and picks the database that wins. The existing, unmodified
top-k/FK-bridge/keyword-fallback retrieval logic then runs exactly as
before, scoped to that one winning database's collection.

`retrieve_schema_node` calls `select_database` only on the *first* pass
through a question and stores the result in `AgentState["selected_database"]`.
The one retry path that re-enters `retrieve_schema` (`execute_sql`'s
`missing_reference` retry — see the self-correcting retry loop above)
**reuses** that stored value rather than re-routing: a retry must keep
targeting the same database attempt 1 already generated/executed SQL
against. Every downstream dialect/engine resolution
(`validate_sql_node`, `estimate_query_cost_node`, `execute_sql_node`, and
`ui/app.py`'s "Confirm and Run") reads `db.connection.get_connection(settings,
state["selected_database"])` rather than a single global `Settings.db_type`.

Two things deliberately left alone by this design (documented, not
silently ignored): the eval benchmark's per-case `database:` label (see the
`eval/` folder note above) and `config/table_descriptions.yaml`/
`config/sensitive_columns.yaml`, which are keyed by bare table name, not
`(database, table)` — a note/classification for one configured database's
table could in principle also apply to a same-named table in another. Both
are real, narrow limitations worth knowing about if you're extending this
further, not oversights to silently work around.

### Multi-source orchestration (router + subgraphs)
`ENABLE_MULTI_SOURCE_ROUTER` (default `false`) puts a router in front of
the SQL pipeline, in `agent/orchestrator/`. Router + subgraphs, not a
single tool-calling agent, was a deliberate choice, for the same reason the
SQL pipeline itself is an explicit LangGraph state machine and not a
ReAct-style agent: every source's safety boundary (the SQL validator, the
policy-sensitivity gate, the "web content is untrusted" framing) stays
separately testable and inspectable rather than folded into one model's
implicit tool-selection reasoning.

`agent.orchestrator.graph.run_orchestrated` is the one entry point
`ui/app.py`/`api/main.py` call, and it is deliberately a **two-path
function, not a graph with one trivial branch**:

- Flag off (the default): `run_orchestrated` calls `agent.graph.run_agent`
  directly and returns its result completely unwrapped — not "the
  orchestrator with one destination," the literal same call the UI made
  before this package existed. `eval/runner.py` and the standalone scripts
  also still call `run_agent` directly and are entirely unaffected. This is
  what makes the byte-for-byte-unchanged guarantee for SQL-only questions
  provable rather than just claimed: the orchestrator graph is never even
  constructed on this path.
- Flag on: the orchestrator graph actually runs —
  `router → {sql_subgraph, document_rag, policy_rag, web_search} (any
  combination) → synthesis → END`. `router_node` calls
  `agent.orchestrator.nodes.get_available_sources(settings)` (checks each
  source's `ENABLE_*` flag *and* the config it actually needs — an enabled
  flag with nothing configured behind it is not "available"); with ≤1
  source available it short-circuits with **zero LLM calls**, mirroring
  `embeddings.retriever.select_database`'s own single-database
  short-circuit. With 2+, one LLM call (`classify_sources`) picks which
  source(s) apply, falling back to *every* available source (never zero) on
  an unparseable response. `route_after_router` returns a **list** of
  destination node names — LangGraph fans out to all of them as parallel
  branches in the same graph step before `synthesis` runs (confirmed
  against this project's pinned LangGraph version before this was built,
  not assumed) — this is what lets "compare policy X with the database"
  hit two subgraphs in one step rather than sequentially.
- `OrchestratorState` (`agent/orchestrator/state.py`) *extends* `AgentState`
  rather than replacing it — `sql_subgraph_node`'s full `run_agent()` result
  merges into it under the exact same keys (`status`, `sql`, `result_rows`,
  ...), which is why every existing `ui/app.py` read site keeps working
  whether a question went through `run_agent` directly or through the
  orchestrator. `synthesis_node` is a pure pass-through when only one
  source fired (that source's own answer stands unedited, no LLM call
  spent restating something already complete); with 2+, each source's
  contribution is shown under its own labeled heading, never blended into
  one unattributed claim.
- **Known limitation, found during testing, not yet fixed:** every routed
  subgraph receives the *same full, un-decomposed question text*. A
  cleanly single-topic multi-source question works fine (tested: "compare
  our leave policy with sales in the database" correctly fanned out and
  retrieved both), but a question that's really two separate asks mashed
  into one sentence can retrieve poorly on *both* sides even though each
  side would have worked fine alone. Per-source query decomposition (asking
  the router or a follow-up LLM call to produce a source-specific
  sub-question) would fix this — deliberately out of scope for the initial
  build, flagged here rather than silently left as a mystery if you hit it.

See `docs/ARCHITECTURE.md`'s "Multi-source orchestration" section for the
full diagram and per-node walkthrough, and `docs/MULTI_SOURCE_GUIDE.md` for
how to configure and use each source (including where the Tavily API key
goes and how to upload a policy PDF).

### Document/policy agentic RAG (`rag/`)
Same code serves "documents" (general uploads) and "policies" (more
access-sensitive), parameterized by collection name
(`rag.graph.build_rag_subgraph(collection)`), not two near-duplicate
modules — they're structurally identical and only differ in how
`generate_node` treats a sensitive chunk. The subgraph is
retrieve → grade → (rewrite → retry, bounded by `RAG_MAX_RETRIES`) →
generate-with-citations, or an insufficient-information fallback after
retries are exhausted — the same bounded self-correction philosophy as the
SQL pipeline's retry loop, a separate knob because a bad chunk retrieval
and a bad SQL parse aren't the same kind of budget.

Storage (`rag/store.py`) is SQL Server 2025+/Azure SQL's native `VECTOR`
column type — confirmed against this project's actual target instance
before being built, not assumed, including one real bug found and fixed
along the way: a long (~7000+ character) embedding JSON string gets bound
by pyodbc as `ntext` rather than `nvarchar`, and SQL Server's `VECTOR` cast
rejects `ntext` as a source type ("Explicit conversion from data type ntext
to vector is not allowed") — fixed by casting through `NVARCHAR(MAX)`
first (`rag.store._VECTOR_CAST`). Storage is a **dedicated connection**
(`RAG_STORE_CONNECTION_STRING`), never one of `DB_CONNECTIONS` — chunk/
embedding storage isn't business data and shouldn't share a schema or
connection pool with a configured database.

Policy sensitivity (`compensation`/`disciplinary`/`legal`, set per-document
at upload time — `rag/store.py`'s `SensitivityCategory`) is enforced by
`rag/graph.py`'s `generate_node` refusing to summarize a sensitive chunk
into an answer at all, since this app has no per-user authorization system
to check who's allowed to see it — the same fail-closed philosophy as
`agent/sql_validator.py`'s `SAFETY_VIOLATION_TYPES`, applied to a different
data shape (a document/chunk tag instead of a `(table, column)` pair).

Retrieved chunk text is framed as **untrusted data, never instructions**
in `rag/graph.py`'s `_GENERATE_SYSTEM_PROMPT` — the same "SQL is untrusted
output" principle below, applied to what a poisoned/malicious uploaded PDF
could contain, since that's this feature's realistic injection vector.

**Downloadable source PDF (`ENABLE_PDF_DOWNLOAD`, default `true`):** the
original uploaded PDF's bytes are stored in a new `rag.documents.pdf_bytes`
column (`rag/store.py::ensure_schema` migrates an existing table
automatically — an idempotent `ALTER TABLE ... ADD` guard, the one schema
migration this module has needed) and served on demand
(`get_document_bytes`) from a chat answer's citation
(`ui/app.py::_render_source_answer`) or the Knowledge Sources management
page, never fetched into a listing/search query itself — both
`list_documents` and `similarity_search` project a cheap `has_pdf_bytes`
boolean instead. Before this existed, ingestion discarded the raw bytes
right after text extraction, so **only documents uploaded after this
shipped are downloadable** — there's nothing to backfill from for an
already-ingested one; re-uploading it is the only way to make it
downloadable. The chat-answer download button is built strictly from the
existing `citations` list, never a separately-fetched document list — this
is what makes it inherit the sensitivity gate above for free: a restricted
policy match already produces `citations == []` before any button could be
built from it, so there's no separate access check to remember. The
Knowledge Sources page's own per-document download button has no such
gate, but that page already shows every document's `sensitivity_category`
and offers an unconditional delete button for all of them — a download
button there is consistent with that page's already-fully-privileged,
no-per-user-authorization exposure level, not a new escalation.

### Live web search (`search/`)
`search/web_search.py` mirrors `db/connection.py`'s `SUPPORTED_DB_TYPES`
pattern exactly: `SUPPORTED_SEARCH_PROVIDERS` maps a provider name to its
call function, so `WEB_SEARCH_PROVIDER` is a `.env` change, not a code
change. Only `tavily` is implemented today. Results are wrapped in a fixed
`WebResult` shape before they ever reach a prompt and are explicitly framed
as external/live/untrusted data in the answer-generation prompt
(`agent.orchestrator.nodes.web_search_node`) — the answer text itself
always opens with "According to a live web search:", so it's never
presented as if it came from the company's own systems, and the same
"data, not instructions" principle as ingested PDF content applies here
too, since a search result's content is exactly as attacker-influenceable
as a stored database value or an uploaded document.

### Media generation (image/video) (`media_gen/`)
`media_gen/` is a tested IMA Studio client wired into the orchestrator as a
`"generation"` source (`agent.orchestrator.nodes.generation_node`). Image
generation is confirmed working end-to-end against a real IMA account (a
live text-to-image call succeeded: generation, download, and serving via
`GET /media/{media_id}`); video generation shares the same client/
task-creation code path but hasn't been separately confirmed with a live
call yet. `ENABLE_MEDIA_GENERATION` still stays off by default so a fresh
clone never spends real IMA credits without the operator deliberately
opting in. Two design points worth knowing if you touch this:

- **Image vs. video is a cheap keyword heuristic, not a second LLM call**
  (`generation_node.infer_media_kind`, mirroring `agent/complexity.py`'s
  own regex-heuristic style) — a question containing "video"/"clip"/
  "animate"/"animation"/"footage"/"motion" gets `generate_video`, else
  `generate_image`. `generate_audio` is built and tested but not
  auto-routed here (nothing in this feature's scope asks for audio).
- **Generated media is served through this app, never the provider's raw
  CDN URL.** `execute_generation` (the function that actually calls IMA —
  see "Human-in-the-loop approval gate" below) downloads the bytes once
  (`media_gen.download.download_media_bytes`, SSRF-hardened — see its own
  module docstring) and stores them under an opaque id in a bounded,
  process-lifetime in-memory cache (`media_gen.cache.MediaCache`, FIFO
  eviction past 100 entries, not persisted — a restart loses in-flight
  generated media, an accepted tradeoff same as this app's other
  process-global caches). Neither `MediaGenerationResult.answer` nor the
  API's `MediaGenerationResultOut` ever carries the raw URL — only
  `media_id`. The React frontend fetches the bytes via the authenticated
  `GET /media/{media_id}` (`api/media.py`, mirroring `api/documents.py`'s
  PDF download route) and renders an `<img>`/`<video>` from a blob object
  URL (`MediaResultCard.tsx`); `ui/app.py` runs `run_orchestrated`
  in-process and reads the cache directly (`_render_generation_result`),
  no HTTP round trip needed. This was chosen over persisting to a DB
  column (bigger lift, no real need yet) or passing the provider's URL
  straight through (simpler, but the link can expire and there's no
  server-side re-inspection of the bytes before display).
- **Human-in-the-loop approval gate before any provider call
  (`Settings.require_generation_approval`, default `true`).** Generation
  is the only orchestrator source that spends real, metered money —
  `generation_node` only *proposes* what would be generated
  (`status="pending_approval"`, no `media_id`, nothing charged) until a
  human explicitly confirms via `POST /generate/confirm`
  (`api/generation.py`) or the matching "Generate" button in both UIs.
  `execute_generation` (extracted out of the old `generation_node` body)
  is the one function both the confirm endpoint and the
  approval-disabled path call — it re-runs its own safety/rate-limit
  checks regardless of which caller reaches it, since it has two
  independent entry points. Mirrors the SQL pipeline's own "Confirm and
  Run" gate, applied to the one source here that costs real money — see
  `SECURITY.md`'s "Media generation" section for the full security
  rationale (added 2026-09-13, alongside SSRF hardening, a strengthened
  content-policy check, new rate limits, and a session-scoped
  expensive-source cost ceiling — `docs/security-changelog.md`'s matching
  entry has the complete list).
- The router's `classify_sources` prompt carries explicit few-shot
  examples distinguishing a genuine "create new media" request from a
  plain "show me the data" one that merely mentions a picture/video in
  passing (`agent.orchestrator.nodes._GENERATION_FEW_SHOT_GUIDANCE`) — see
  that constant for the exact phrasing this was tuned against.

**Known gaps, named rather than silently left:**
- **No "search existing media" capability exists.** A question that really
  means "find the existing photo/recording of X" has no dedicated tool to
  route to — it simply falls through to the ordinary data sources like any
  other question, same as before this feature existed. Building a real
  media-search tool (an index, a store) is a separate, larger feature, not
  attempted here.
- **Multi-source synthesis doesn't embed generated media inline.**
  `synthesis_node` only ever concatenates text; a generation result folded
  into a multi-source answer shows only its (link-free) confirmation text,
  not the image/video itself. Single-source generation (the realistic
  case) is unaffected.

### SQL is untrusted output, always
The LLM's SQL is never trusted at face value. `agent/sql_validator.py`
parses it with `sqlglot` (in the dialect matching `DB_TYPE`) and rejects
anything that isn't a single `SELECT`/`UNION`/`EXCEPT`/`INTERSECT` statement
(explicit allowlist of the parsed statement type, not a regex blocklist).
Execution happens on a read-only-by-convention SQLAlchemy engine
(`db.connection.get_read_only_engine()`), with a row cap (`MAX_RESULT_ROWS`,
enforced *both* via `LIMIT` in the SQL text and independently via
`fetchmany()` at the cursor level, so a malformed/mistranslated query can't
bypass it just by lacking a working `LIMIT`) and a query timeout enforced at
the driver level where a cheap session-level `SET` exists (Postgres, MySQL)
and via forced connection-abort otherwise (SQL Server, Oracle — see
`db/execution.py::_execute_with_timeout`). This validation step runs
**every** time SQL is about to be displayed or executed for the user,
including after the user hand-edits the SQL box in the UI — an edit is
exactly as untrusted as an LLM generation.

One nuance worth knowing if you're reading `ui/app.py`: the LangGraph
agent's own internal retry loop *does* execute candidate SQL automatically
(that's how it detects and self-corrects runtime errors like an unknown
column) — those internal executions are safe (read-only, validated,
row-capped, timed out) but are never shown to the user. Nothing is rendered
until the user clicks **Confirm and Run**, and that button always
re-validates and re-executes the *current* SQL text fresh, rather than
trusting whatever the agent's last internal attempt produced.

### True read-only enforcement is layered, not just code-level
`get_read_only_engine()` does not itself strip write privileges — there's no
generic, cross-database way to do that purely at the SQLAlchemy layer. The
real guarantee is two layers: (1) the SQL validator, described above, and
(2) the `.env` `DB_USER` should be a genuinely read-only database
role/account (documented in README's Security section, not silently
assumed). If you're asked to "harden" this further, that's the layer to
push on — a DB-level read-only user, not more code-level checks, since the
validator is already an AST-based allowlist rather than a blocklist.

### Pydantic-based configuration and validation
`config.settings.Settings` is a `pydantic_settings.BaseSettings` (not a
plain `@dataclass`, as it was originally) — field types, `Field(gt=0)`
constraints, and `Literal` types (`log_redaction_level`) get automatic
env-var coercion and validation essentially for free, instead of the
hand-written `_env_int`/`_validate_security_settings()` loop this replaced.
A `Settings.__init__` override still guarantees the codebase's
long-standing `ConfigurationError` contract: it catches
`pydantic.ValidationError` (raised by Pydantic's own type coercion, e.g.
`MAX_RETRIES=abc`) and translates it, while two `model_validator`s
(cross-field cost-threshold ordering; filling in a default `databases`
entry) raise `ConfigurationError` directly — verified to propagate
unwrapped through Pydantic's validation machinery, since Pydantic only
intercepts `ValueError`/`TypeError`/`AssertionError` to build its own
`ValidationError`. `DatabaseConnectionConfig` is a plain (non-Settings)
`BaseModel`, `frozen=True` like `Settings` itself.

**Because `Settings` now reads `os.environ` for any field a caller doesn't
explicitly pass** (the entire point of `BaseSettings` — unlike the old
dataclass, which just used its class-level default), constructing
`Settings(**partial_kwargs)` directly is no longer isolated from this
machine's real `.env` the way it used to be. `tests/conftest.py`'s
autouse `_isolate_settings_from_real_environment` fixture strips every
env var one of `Settings`'s fields could read before each test — without
it, a test's `Settings(enable_document_rag=True)` would silently inherit
this project's own `ENABLE_POLICY_RAG=true`/`ENABLE_WEB_SEARCH=true` etc.
from the real `.env` for every field it didn't explicitly override. Tests
that build a `Settings` copy with a few fields changed
(`tests/test_connection.py::_settings` is the canonical example) rebuild
via `Settings(**{**base.__dict__, **overrides})` rather than
`BaseModel.model_copy(update=...)`, because `model_copy` skips validation
entirely — several tests (`test_settings_validation.py` in particular)
depend on a bad override still raising `ConfigurationError`.

Secrets (`db_password`, `db_connection_string`, `api_auth_token`,
`rag_store_connection_string`, `web_search_api_key`) are `pydantic.SecretStr`
(see `security/secrets.py`'s docstring) rather than the project's old
hand-rolled `str` subclass, which only masked `repr()`/`%r` and stayed
fully transparent everywhere else (`str()`, f-strings, even `.upper()`).
Pydantic's version masks `str()`/`repr()`/`%r`-formatting alike and isn't
a `str` subclass at all — every call site that needs the real value
(`db/connection.py`'s URL builder, `api/auth.py`'s bearer-token check,
`rag/store.py`, `search/web_search.py`, `security/redaction.py`) must call
`.get_secret_value()` explicitly now; a leftover `str(secret)` would
silently send/compare/search for the literal string `"**********"`
instead.

Several other hand-rolled `@dataclass`es that sit at a real validation
boundary were converted to `BaseModel` (`ConfigDict(frozen=True)`, same
shape as before) the same way: `agent/sql_validator.py`'s
`ValidationResult` (gained a `model_validator` enforcing its
is_valid/error/violation_type invariant), `db/query_cost.py`'s
`CostEstimate`, `agent/insight.py`'s `ColumnStat`/`ResultSummary`,
`db/connection.py`'s `ConnectionTestResult`,
`config/sensitive_columns.py`'s `ColumnClassification`, and
`config/table_descriptions.py`'s `TableDescription`. Purely-internal
plumbing dataclasses with no external validation boundary (`DbTypeInfo`/
`WritePrivilegeCheckResult` in `db/connection.py`, `db/query_cost.py`'s
`_RawPlanInfo`, `db/schema_introspection.py`'s introspection types, the
`eval/` benchmark's own dataclasses, and others) were deliberately left
alone — converting everything would have added validation overhead with
no real boundary to protect. `api/schemas.py`'s request/response models
(already Pydantic) were hardened alongside this: `extra="forbid"` +
`str_strip_whitespace=True` on request models (`AskRequest`,
`ConversationExchangeIn`), `frozen=True` on every response model.
`AskRequest.question` deliberately does NOT duplicate
`Settings.max_question_length` as a schema-level `Field(max_length=...)`
— that cap is config-driven and already enforced downstream by
`agent.input_guard.check_input`, and a static schema-level number would
either hardcode the wrong value or drift from it.

### Centralized exception handling (safe vs. internal messages)
Every `agent.exceptions.AgentError` (and its subclasses —
`OllamaUnavailableError`, `MalformedLLMOutputError`, `SchemaRetrievalError`,
`SqlExecutionTimeoutError`, `OffTopicQuestionError`) now carries two
messages, not one: `str(exc)` stays the full internal detail (a raw
driver/Chroma/Ollama error — genuinely useful for logs and for feeding
back into the retry loop's own self-correction prompt), and `.safe_message`
is a short, non-technical sentence with no driver internals, hostnames, or
connection-string-shaped text, each subclass defaulting to something
sensible. This exists because those two things used to be the same string
everywhere — `agent/nodes.py` and `api/main.py` both embedded `str(exc)`
directly into fields the client sees (`AskResponse.error_history`/
`failure_explanation`), which is exactly how a raw DB error could reach a
response.

Two complementary fixes ride along with this:
- `security.redaction.redact_secrets` (previously applied only to
  `db/connection.py`'s connection-test failures) is now also applied to the
  raw driver text in `agent.nodes.execute_sql_node`'s
  `(SQLAlchemyError, TimeoutError)` handling and
  `embeddings.retriever.retrieve_relevant_schema`'s generic-exception
  wrapping — both are genuine "text this app did not construct itself"
  call sites (see that module's docstring) that weren't covered before.
  Redaction happens once, before the text is used anywhere (logs, the
  retry-feedback prompt, and the user-facing fields alike) — a redacted
  password doesn't change what a syntax/missing-column error says, so
  retry self-correction quality is unaffected.
- `api/main.py` registers two global handlers:
  `@app.exception_handler(AgentError)` (uses `.safe_message`) and
  `@app.exception_handler(Exception)` (a generic "something went wrong"
  body) — both log the full detail server-side, tagged with the request's
  correlation ID, and guarantee no response body ever contains raw
  exception text, even for a failure mode nothing already handles locally.
  `/ask`'s own local `except AgentError` (previously
  `except SchemaRetrievalError`, widened to the whole hierarchy so a future
  source's exception is covered the same way without another special case)
  is the common path in practice — it keeps returning the existing
  200-with-a-`"failed"`-body shape, just built from `.safe_message` now;
  the global handlers are the defense-in-depth net behind it. `/health`'s
  own raw `f"Unreachable: {exc}"` strings (that endpoint has no
  `Depends(verify_api_key)` — it's meant to be reachable by an
  orchestrator with no API key, so a leak there is a *public*, not just
  internal, exposure) are now redacted the same way.

### Process-lifetime singletons (DB engine, Ollama client, compiled graph)
Three things are now built once per process instead of once per call,
using the same `functools.cache`/`lru_cache` pattern
`config.settings.get_settings()` and `db.connection._cached_engine`
already established:
- `agent.llm_client._get_ollama_client(host, timeout)` (public wrapper:
  `get_ollama_client(settings)`) — every `generate_*_from_llm` call site
  used to build a fresh `ollama.Client` (and therefore a fresh underlying
  `httpx.Client` connection pool) on every single call, including every
  retry. Reused now, keyed by `(host, timeout)`.
- `agent.graph.build_graph()` and `agent.orchestrator.graph
  .build_orchestrator_graph()` — previously rebuilt (re-wiring all ten SQL
  nodes, or the orchestrator's six) on *every* `run_agent()`/
  `run_orchestrated()` call. A compiled LangGraph graph is stateless (all
  per-question state lives in the `initial_state` dict passed to
  `.invoke()`), and graph *shape* never depends on `Settings` (`plan_query`/
  `review_sql` are pass-throughs when planning is off, not conditionally
  omitted nodes — see "Agentic query planning" above), so caching is safe
  and this was the single largest avoidable per-question cost of the three.
- `db.connection._cached_engine` — already a singleton before this (see
  its own docstring); unchanged.

`api/main.py`'s `lifespan` context manager (see the `api/` folder note
above) exists purely to move each of these singletons' one-time build cost
from whichever request arrives first to process startup — none of them
were *incorrect* without it, since the cached functions build lazily on
first use either way. **Test isolation note:** because these caches persist
for the life of the Python process, `tests/conftest.py`'s
`_clear_process_singleton_caches` autouse fixture clears all three before
every test — without it, a test that monkeypatches `ollama.Client` (or a
node function referenced by a compiled graph) to a fake would silently see
no effect whenever an earlier test already populated that cache slot,
since the cached function body simply wouldn't re-run.

### Caching
- Chroma embeddings: SHA-256 fingerprint of the *introspected* schema
  (`db.schema_introspection.get_schema_fingerprint`), not a file hash
  anymore since there's no file. Stored alongside the Chroma persist dir;
  `schema_indexer.build_index()` skips re-embedding if the fingerprint
  matches. The UI's "Refresh Schema" button re-introspects and calls
  `build_index()` normally (not forced) — the skip-if-unchanged behavior is
  what makes clicking it cheap when nothing's actually changed.
- Streamlit: `@st.cache_resource` for settings, the startup connection
  check, and the introspection+embedding step (cleared and re-run
  explicitly by the "Refresh Schema" button); `@st.cache_data` for query
  results keyed by SQL text; a simple in-memory dict cache in `session_state`
  for repeated identical NL questions within a session, to skip redundant
  LLM calls.
- Process-lifetime singletons (DB engine, Ollama client, compiled LangGraph
  graph) — see "Process-lifetime singletons" above.

## How to run

See `README.md` for full setup. Short version:

```powershell
.venv\Scripts\Activate.ps1
ollama pull llama3.1:8b
# fill in .env with your real DB connection details first
python scripts\test_db_connection.py
python scripts\build_embeddings.py
streamlit run ui\app.py
```

## How to run tests / lint

```powershell
.\tasks.ps1 test     # pytest
.\tasks.ps1 lint      # ruff check + black --check + mypy
.\tasks.ps1 format    # black + ruff --fix
```

Equivalent `make test`, `make lint`, `make format` targets exist in the
`Makefile` for anyone on WSL/macOS/Linux. All pytest tests are fully mocked
(no real DB, no Ollama) — `scripts/integration_test.py` is the separate,
manual, real-DB-required script; it is never run by `pytest` or CI.

## Common commands

| Task | PowerShell | Make |
|---|---|---|
| Create venv + install deps | `.\tasks.ps1 setup` | `make setup` |
| Verify DB connection | `python scripts\test_db_connection.py` | `python scripts/test_db_connection.py` |
| Build/refresh embeddings | `python scripts\build_embeddings.py` | `python scripts/build_embeddings.py` |
| Run app | `.\tasks.ps1 run` | `make run` |
| Run tests | `.\tasks.ps1 test` | `make test` |
| Lint | `.\tasks.ps1 lint` | `make lint` |
| Manual real-DB integration check | `python scripts\integration_test.py` | `python scripts/integration_test.py` |

## Windows / Visual Studio-specific notes

- This is a Python project opened in Visual Studio via **File > Open > Folder**,
  not a `.sln`-driven C#/.NET project. A minimal `.sln` file exists only so the
  folder can also be opened via "Open Solution" if preferred — it does not
  define real build configurations.
- PowerShell is the primary shell; venv activation is
  `.venv\Scripts\Activate.ps1`, not `source .venv/bin/activate`. If script
  execution is blocked, the user needs to run PowerShell as themselves (not
  admin) and check `Get-ExecutionPolicy` — do not suggest
  `Set-ExecutionPolicy Unrestricted` machine-wide; `RemoteSigned` for
  `CurrentUser` scope is the least-surprise fix.
- `.vs/` (Visual Studio's own cache folder) is gitignored.
- Chroma writes local files (the persist dir under `embeddings/.chroma/`) —
  gitignored, regenerated by `scripts/build_embeddings.py`. There is no
  local database file anymore (that was DuckDB-specific and is gone).
- `DB_TYPE=mssql` requires the Microsoft ODBC Driver for SQL Server
  installed as a *system* package (not pip-installable) — see README's
  driver table. This is the one DB_TYPE with an extra manual install step
  on a fresh Windows machine.
- Ollama must be running as a background service (`ollama serve`, or it's
  already running if installed via the Windows installer) before the agent or
  UI is started — `config/settings.py` reads `OLLAMA_HOST` from `.env`,
  default `http://localhost:11434`.

## Coding standards

- Type hints and docstrings on every public function — this codebase is meant
  to be interview-explainable, not just working.
- No `print()` for anything other than the Streamlit UI's own display logic
  and the standalone CLI scripts (`scripts/test_db_connection.py`,
  `scripts/integration_test.py`, which are meant to be read as terminal
  output, not logged) — everything else uses the `logging` module (see
  `config/settings.py` for level config, overridable via `LOG_LEVEL` in
  `.env`). The agent nodes log each state transition (node entered, retry
  count, validation result) so the terminal shows the agent's reasoning
  steps live. **Never log the connection string, password, or full result
  rows** — log `DB_TYPE`/`DB_NAME`/table names/row counts only; this is a
  real security property of the codebase, not just a style preference.
- Black for formatting, ruff for linting, mypy for type checking — config in
  `pyproject.toml`. Run `.\tasks.ps1 lint` before considering a change done.

## Known gaps / follow-ups (multi-source RAG)

Named explicitly rather than silently left for a future session to
rediscover:

- **`rag/` and `search/` have no dedicated `pytest` unit test files yet.**
  `tests/test_orchestrator.py` mocks their entry points
  (`rag.llm.call_ollama`, `rag.graph.run_rag`, `search.web_search.web_search`)
  to test the orchestrator's own wiring, but `rag/store.py`,
  `rag/ingestion.py`, `rag/retriever.py`, `rag/graph.py`'s internal nodes,
  and `search/web_search.py` itself were verified by running them against
  this project's real SQL Server/Tavily/Ollama instances during
  development (documented end-to-end: ingestion, retrieval, grading,
  sensitivity blocking, the insufficient-information fallback, and a real
  multi-source fan-out all confirmed working), not by a mocked regression
  suite. Adding one (chunking logic, the `VECTOR` cast SQL construction,
  Tavily response parsing — all pure-logic-testable with mocks) is a real,
  worthwhile follow-up before this code is trusted long-term.
- **Per-source query decomposition doesn't exist** — see "Multi-source
  orchestration"'s "Known limitation" note above.
- **`DB_<NAME>_SCHEMA` is one schema per connection, not "all schemas."**
  A database whose real tables span many schemas (this project's own
  `HrAutomationDb`: 243 tables across 11 schemas) only exposes one at a
  time to the agent. Documented in `.env`'s own comments, not silently
  worked around.
- **The eval harness (`eval/`) was not extended for multi-source/
  cross-source questions.** It still only exercises `agent.graph.run_agent`
  (the SQL-only path) — a real gap if multi-source accuracy needs the same
  execution-accuracy-first rigor the SQL benchmark already has.
- **No persistent conversation memory across app restarts.** Follow-up
  question resolution (`agent/followup.py`) is session-only, same as
  before multi-source support existed — a SQL Server-backed LangGraph
  checkpointer was scoped in early design discussion but never built (no
  official LangGraph SQL Server checkpointer exists at this project's
  pinned `langgraph==0.2.62`; it would need a custom
  `BaseCheckpointSaver`).
