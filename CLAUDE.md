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
  detail anywhere else — import from here. `Settings` is a passive config
  bag; it validates *individual* malformed values (e.g. `DB_PORT=abc`) at
  load time but does not require DB fields to be present just to import the
  module — `db/connection.py` validates *combinations* (e.g. "DB_TYPE set
  but DB_HOST missing") at the point something actually tries to connect.
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
  runs. See "Multi-database auto-routing" below.
- `agent/` — LangGraph nodes live in `nodes.py`, one function per node, each
  taking and returning `AgentState` (defined in `state.py`). `graph.py`
  wires them together and compiles the graph. `sql_validator.py` is the
  security boundary — see below. `AgentState["selected_database"]` records
  which configured database a question was auto-routed to (set once by
  `retrieve_schema_node`); the sqlglot dialect used for validation/cost
  estimation is resolved from *that* database's `db_type`
  (`db.connection.get_connection(settings, selected_database)` +
  `get_sqlglot_dialect()`) — never a single hardcoded/global engine.
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
- `ui/pages/1_Knowledge_Sources.py` — PDF upload + management for the
  "documents"/"policies" collections (Streamlit's native multipage
  convention: any script under `ui/pages/` becomes its own page
  automatically, no change to `ui/app.py` needed). Two tabs, each showing a
  clear "not configured" message rather than a broken upload form if its
  `ENABLE_*_RAG` flag or `RAG_STORE_CONNECTION_STRING` isn't set. A policy
  upload gets an optional sensitivity-category selector (compensation/
  disciplinary/legal/none — see `rag/store.py`'s `SensitivityCategory`).
- `scripts/` — standalone entry points: `test_db_connection.py` (verify
  `.env` before booting anything else — prints pass/fail, DB version, table
  count, or a classified readable error, per configured database),
  `build_embeddings.py` (introspect + embed every configured database, with
  `--force`), `integration_test.py` (manual, requires real database(s), not
  part of the pytest suite — see `tests/` below; also demonstrates routing
  when 2+ databases are configured), `run_benchmark.py` (the Text-to-SQL
  benchmark runner — see `eval/` below; also manual/real-DB-required, not
  part of the pytest suite).
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
  Mirrors package names (`test_sql_validator.py`, `test_connection.py`,
  `test_schema_introspection.py`, `test_schema_retriever.py`,
  `test_agent_nodes.py`), plus `test_db_router.py` (the multi-database
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
The full graph (`agent/graph.py`) is eight nodes, not four:
`sanitize_input → classify_followup → retrieve_schema → generate_sql →
validate_sql → estimate_cost → execute_sql → generate_insight`. On a
validation, cost-estimate, or execution failure, a conditional edge routes
back to `generate_sql` (or, for a "missing reference" execution error,
back to `retrieve_schema`) with the error message appended to the state's
history, so the LLM sees what went wrong and can correct itself. Capped at
`MAX_RETRIES = 3` (`config/settings.py`) — after that, the graph ends in a
terminal `failed` state and the UI surfaces the last error rather than
looping forever. This is the interview-relevant piece: it's a small
explicit state machine, not a ReAct-style free-form agent, specifically so
the retry/error-feedback path is inspectable and boundable. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full per-node
walkthrough and the complete retry-routing table.

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
