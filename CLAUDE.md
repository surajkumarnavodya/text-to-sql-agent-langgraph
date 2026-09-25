# CLAUDE.md — Project Context for Claude Code Sessions

This file orients any future Claude Code session working in this repo. Read this
before making changes.

## What this project is

A Text-to-SQL dashboard connected to one or more real, user-configured
databases. A user types a natural-language question in a React dashboard
(`frontend/`), a LangGraph agent turns it into SQL against the configured
database (schema retrieved via ChromaDB, embedded from **live schema
introspection** — not a hardcoded sample — so only relevant tables are
shown to the LLM), the SQL is validated (SELECT-only allowlist), executed
read-only, and the result is rendered as a table + auto-picked chart. The
LLM runs locally via Ollama — no network calls for the LLM, no API keys
required for that part. Database connectivity is fully config-driven via
`.env`; there is no hardcoded connection string, host, or schema anywhere
in the codebase.

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

**History note:** the project also originally shipped a Streamlit app
(`ui/app.py` + `ui/pages/1_Knowledge_Sources.py` + `ui/theme.py`) alongside
the newer React dashboard while the two were being brought to feature
parity. Once parity was confirmed, the Streamlit app was fully removed (by
explicit user decision) — the React dashboard (`frontend/`) is now the
only UI. Two small, genuinely non-UI modules that used to live under `ui/`
were relocated rather than deleted, since real code outside the old UI
still depended on them: `ui/session_history.py` → `eval/history.py`
(trimmed to what `eval/runner.py`/`scripts/run_eval.py` actually use — the
Streamlit-only state-mutation helpers and the `status_label` display
helper were dropped as genuinely dead code once nothing rendered them
anymore), and `ui/column_formatting.py` → `db/column_formatting.py`
(still used by `scripts/profile_pipeline.py`, mirrored on the frontend by
`frontend/src/lib/columnFormatting.ts`). If you see a reference to
`ui/app.py`, `ui.session_history`, or `ui.column_formatting` anywhere
(docs, old branches, stale comments), it's leftover from that phase.

**Multi-source (optional, off by default):** `ENABLE_MULTI_SOURCE_ROUTER=true`
switches `api/main.py` from calling `agent.graph.run_agent`
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
| Business-context vector retrieval | Same ChromaDB `PersistentClient`, a second collection (`retrieval/`) — table/column/relationship/glossary/metric/sql_example/documentation chunks, deterministic IDs, idempotent ingestion. Additive to (never a replacement for) live schema retrieval above. See "Business-context vector retrieval" below |
| User accounts / universal chat history | Optional (`LOCAL_AUTH_ENABLED`), dedicated PostgreSQL database (`identity/`) — Argon2id + JWT auth, and (new) permanent, cross-device conversation/message storage (`identity.models.Conversation`/`Prompt`/`AiOutput`, `api/chat_history.py`). See "Local self-hosted accounts" and "Universal server-side chat history" below |
| Database | User's own — PostgreSQL, MySQL, SQL Server, or Oracle, via SQLAlchemy. Config-driven (`DB_TYPE` + connection params in `.env`), pluggable per `db.connection.SUPPORTED_DB_TYPES`. One or more named connections (`DB_CONNECTIONS` in `.env`); the agent auto-routes each question to the right one when more than one is configured |
| SQL parsing/validation | sqlglot — parses generated SQL and checks statement type against an allowlist, in the dialect matching `DB_TYPE` |
| Document/policy RAG | SQL Server 2025+/Azure SQL native `VECTOR` column type (`rag/store.py`) — a dedicated connection (`RAG_STORE_CONNECTION_STRING`), separate from `DB_CONNECTIONS`. Optional, off by default (`ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG`) |
| Web search | Configurable provider (`search/web_search.py`), Tavily implemented today. Optional, off by default (`ENABLE_WEB_SEARCH` + `WEB_SEARCH_API_KEY`) |
| UI | React + Vite + Tailwind (`frontend/`) — the only UI this project ships (a Streamlit app used to ship alongside it; removed once feature parity was confirmed, see the "History note" above). Built with `npm run build`, served directly by the FastAPI process (`api/main.py`'s `StaticFiles` mount) in production, or via Vite's dev server (proxying to the API) during frontend development. Charting is Plotly-figure-JSON-in, chart.js-rendering-out (`agent/result_charting.py` builds it, `frontend/src/lib/chartAdapter.ts` renders it) |
| Python | 3.11 is the target per project spec. **This machine only has 3.14 installed** (no 3.11 on PATH via `py -0p`) — the venv was created against 3.14. If a future session hits a wheel-availability issue for a pinned dependency, that's why (see "Python 3.14 gotchas" below for two real ones already hit and fixed). Re-run `py -0p` to check if 3.11 has since been installed and consider recreating `.venv` against it if so. |

### Python 3.14 gotchas already hit (fixed, but worth knowing about)

- **`pandas==2.2.3` segfaults** building a `DataFrame` from any row data
  containing a raw `datetime.datetime` value (a real access violation deep
  in `pandas.core.arrays.datetimes._construct_from_dt64_naive`, reproduces
  in two lines with no app code involved) — this predates real Python 3.14
  wheels. Fixed by pinning `pandas==2.3.3` (see `requirements.txt`'s
  comment). If a future dependency bump reintroduces an old pandas pin,
  this is the symptom to watch for: the API process dies with
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
  `agent.nodes.execute_sql_node` and `api/main.py`'s `POST /execute`
  ("Confirm and Run") path — a pure database-execution concern with no
  LangGraph dependency, so it lives here rather than in `agent/`.
- `embeddings/` — `schema_indexer.py`'s `build_index(tables, db_name, ...)`
  takes already-introspected `TableSchemaInfo` objects (not a file, not an
  engine) and embeds them into **that database's own Chroma collection**
  (never a shared one — see `get_collection`'s docstring for why), keyed by
  a hash of the introspected schema so re-embedding only happens when that
  database's schema actually changed. `refresh_schema_index(engine, db_name,
  settings, force)` is the single introspect → sample → embed pipeline for
  one database; `refresh_all_schema_indexes(settings, force)` runs it for
  every configured database and is what `scripts/build_embeddings.py` and
  `api/main.py`'s startup/`POST /schema/refresh` actually call. `retriever.py`'s
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
- `retrieval/` — the business-context vector-retrieval layer (table/
  column/relationship/glossary/metric/sql_example/documentation chunks) —
  see "Business-context vector retrieval" below for the full design.
  `models.py` (the typed `Chunk` model + deterministic ID/content-hash
  generation), `chunking.py` (schema + `data/knowledge/*` → chunks),
  `embeddings.py` (provider abstraction: `"local"` reuses `embeddings
  .schema_indexer`'s own embedding function, `"fake"` for tests),
  `vector_store.py` (the `VectorStore` interface + its Chroma
  implementation), `reranker.py` (deterministic similarity + type-priority
  + exact-match + diversity scoring, no external reranker model),
  `retriever.py` (the orchestration `agent.nodes.retrieve_business_context_node`
  calls — fails open on any failure, see that module's own docstring),
  `ingestion.py` (idempotent discover → chunk → hash → embed → upsert,
  driven by `scripts/ingest_schema.py`/`scripts/rebuild_index.py`).
  Deliberately reuses `embeddings.schema_indexer.get_chroma_client`'s
  cached `PersistentClient` rather than a second vector database.
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
  (`run_orchestrated`, the single entry point `api/main.py` calls in place
  of `agent.graph.run_agent` directly).
- `agent/tools/` — a generic, MCP-shaped tool abstraction (`types.py`'s
  `Tool`/`ToolCategory`/`RetryPolicy`/`ToolResult`, `registry.py`'s
  `ToolRegistry`, `definitions.py`'s `build_default_registry()`) wrapping
  the same six real functions `agent/orchestrator/nodes.py` already calls
  (`agent.graph.run_agent`, `rag.graph.run_rag` × 2 collections,
  `search.web_search.web_search`, `media.search.search_media`,
  `agent.orchestrator.nodes.execute_generation`) — see "Tool/MCP
  abstraction" below. **Additive, not a replacement**: the orchestrator
  graph's own hardcoded nodes are unmodified and still the only thing
  `run_orchestrated` actually calls in production today.
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
  `sensitivity_category` is never summarized into an answer — a fixed,
  reviewed category this app hard-blocks regardless of caller role, layered
  underneath the newer per-document `restricted_roles` RBAC gate — see
  "Authentication, authorization, and the 2026 security hardening passes"
  below).
- `search/` — live web search, `web_search.py`: a provider-name → call-
  function dict (`SUPPORTED_SEARCH_PROVIDERS`), shaped exactly like
  `db.connection.SUPPORTED_DB_TYPES` — swapping `WEB_SEARCH_PROVIDER` is a
  `.env` change, not a code change. Only `tavily` is implemented today.
- `frontend/` — the React + Vite + TypeScript + Tailwind dashboard, the
  only UI this project ships (see the "History note" near the top of this
  file for the Streamlit app it replaced). `src/pages/Chat.tsx` is the main
  chat page; `src/pages/KnowledgeSources.tsx` is PDF upload/management for
  the "documents"/"policies" collections (Vite/react-router's equivalent of
  a second page, mirroring what used to be a separate Streamlit multipage
  script). `src/store/chatStore.ts` (Zustand) owns conversation state —
  `queryHistory` is the array of turns for whichever conversation is
  currently on screen, and `conversations` is every conversation started
  this session (keyed by id), auto-saved as `queryHistory` changes
  (`commitQueryHistory`); switching conversations swaps which one
  `queryHistory` points at. This is in-memory/session-only, same as the
  Streamlit app's own history was — a page reload clears it, by design, not
  as a regression (server-backed history for a locally-authenticated user
  is a separate, additional layer — see "Universal server-side chat
  history" below; `chatStore` is what mirrors that server state into the
  UI, not a replacement for it). `src/components/layout/AppShell.tsx` is
  the full-viewport shell: a persistent left conversation-history rail
  (`Sidebar.tsx`, `lg:`+ viewports) + main workspace, with a compact header
  (product mark, nav, theme toggle, gear icon) above the workspace only.
  Below `lg:`, the header's hamburger (`MobileNav.tsx`) opens the same
  `Sidebar` in a slide-in drawer instead — one history-list implementation,
  not two kept in sync. **2026 UI pass** (see "Frontend UI redesign"
  below): this replaced an earlier single combined right-side
  `HistoryDrawer.tsx` (history + settings behind one gear icon, now
  deleted) — settings are now `SettingsDialog.tsx`, reachable from the
  header gear and the Sidebar's own footer, wrapping the same
  `HistorySettingsSection.tsx` content unmodified. `HistorySettingsSection.tsx`
  holds everything that isn't chat history itself: appearance
  (theme/accent/font/language, `src/lib/theme.ts`), per-database connection
  status, a manual re-test, a manual schema refresh, a schema browser
  grouped by database, and the "Generate AI insight" toggle.
  `src/components/chat/TurnCard.tsx` renders one
  question+answer turn — schema context and the generated SQL are both
  collapsible (`src/components/ui/expander.tsx`), and technical metadata
  (which database a question was routed to) sits inside a collapsed
  "Query information" panel rather than as a always-visible label. Every
  SQL-specific render (schema context, editable SQL box, Confirm and Run,
  results table) is gated on `"sql"` actually being one of
  `state.sources_used` (or that key being absent entirely, which implies
  the router is off) — a pure web/document/policy answer renders through a
  separate path instead (`SourcesUsedPanel.tsx`), never through the
  SQL-specific one. `POST /execute` (`api/main.py`) re-validates and
  re-executes the *current* SQL text fresh every time "Confirm and Run" is
  clicked — see "SQL is untrusted output, always" below for the nuance
  around the agent's own internal self-correction executions, which are
  never shown to the user. In development, `vite.config.ts`'s
  `BACKEND_ROUTES` proxies `/ask`/`/execute`/`/documents`/`/schema`/
  `/feedback`/`/health`/`/media`/`/generate`/`/voice`/`/search`/`/auth` to
  the API on port 8000 so the browser never needs CORS (`/auth` added
  alongside the local-account feature — see "Local self-hosted accounts"
  below; the chat-history routes, `/conversations`/`/chat`, use the same
  no-prefix same-origin convention and don't need a separate
  `BACKEND_ROUTES` entry since they're plain top-level paths already
  covered by Vite's dev-server default proxying of anything not matched by
  a static asset); in production, `npm run build`'s output
  (`frontend/dist`) is served by that same API process (`api/main.py`'s
  `StaticFiles` mount) at the same paths, so the frontend's own fetch calls
  never need an `/api` prefix or environment-specific base URL.
- `identity/` — this app's own self-hosted user accounts (optional, off by
  default — `Settings.local_auth_enabled`), the *only* ORM (SQLAlchemy 2.0
  declarative) and the *only* real (Alembic) migrations anywhere in this
  codebase — see `identity/__init__.py`'s own docstring for why this is a
  deliberate exception to the rest of the codebase's "raw SQLAlchemy Core +
  idempotent `ensure_schema()`" convention. `models.py` (14 tables:
  accounts/RBAC, sessions/tokens, and `Conversation`/`Prompt`/`AiOutput`
  for chat history), `security.py` (Argon2id hashing, JWT issue/validate,
  opaque refresh tokens), `password_policy.py` + `display_name.py`
  (mandatory-display-name + password-strength validation — see
  `docs/authentication-and-password-policy.md`), `repositories/` (one
  module per aggregate: `users.py`, `sessions.py`, `tokens.py`,
  `signin_events.py`, `history.py` — the chat-history repository, see
  "Universal server-side chat history" below), `rbac.py` (granular
  permission codes bridging into `agent/authz.py`'s own base role names),
  `migrations/` (Alembic, `identity/alembic.ini` — run via `alembic -c
  identity/alembic.ini upgrade head`). Tests against this package use a
  real in-memory SQLite engine (`Base.metadata.create_all`), never a mock
  of the ORM — production always runs against PostgreSQL
  (`AUTH_DATABASE_URL`), a dedicated database, never one of
  `DB_CONNECTIONS`.
- `api/` — `main.py`'s FastAPI app is the REST surface every UI action
  goes through, and (once `frontend/dist` exists) also the process that
  serves the React dashboard itself. A `lifespan` context manager warms
  every process-lifetime singleton at startup rather than on whichever
  request happens to arrive first — every configured database's read-only
  engine, the cached Ollama client, and the compiled SQL/orchestrator
  LangGraph graph(s) — and stashes them on `app.state` for
  discoverability, though request handling itself still reaches them
  through the same cached module-level functions `eval/runner.py` uses,
  not through `app.state` (see "Process-lifetime singletons" below). Two
  global exception handlers
  (`@app.exception_handler(AgentError)` /
  `@app.exception_handler(Exception)`) are the last-resort net ensuring no
  response body ever contains a raw internal exception string — every
  known failure path already uses `.safe_message` locally (see "Centralized
  exception handling" below), so in normal operation these only fire for a
  genuinely new/forgotten call site, not the common case. `/ask` accepts an
  optional `session_id` (`AskRequest.session_id`), generating one via
  `uuid4()` when omitted, and always echoes it back in `AskResponse
  .session_id` — a pure correlation token (the multi-source router's own
  per-session cost-ceiling key), unrelated to real conversation storage.
  `AskRequest.conversation_id`/`AskResponse.conversation_id` are the
  *actual* server-side identifier — set only for a locally-authenticated
  caller, persisted via `api/chat_persistence.py::persist_ask_turn` into
  `identity.models.Conversation`/`Prompt`/`AiOutput` (see "Universal
  server-side chat history" below); this is the one place `/ask` is no
  longer fully stateless, though only conditionally, per-caller.
  `api/chat_history.py` (`GET/POST/PATCH/DELETE /conversations`, `GET/POST
  /conversations/{id}/messages`, `GET /chat/search`) is the read/manage
  surface for that same data.
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
The full graph (`agent/graph.py`) is twelve nodes, not four:
`sanitize_input → classify_followup → retrieve_schema →
retrieve_golden_examples → retrieve_business_context → plan_query →
generate_sql → review_sql → validate_sql → estimate_cost → execute_sql →
generate_insight` (a 2026-09-18 doc-drift fix — `retrieve_business_context`
was already live, documented under its own heading further below, but
missing from this particular summary list; see
`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md` §2). On a review, validation, cost-estimate, or execution
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

Examples are added only via the dashboard's explicit thumbs-up feedback
(`frontend/src/components/sql/GoldenFeedbackWidget.tsx`, calling
`POST /feedback`, shown once a "Confirm and Run" result is genuinely
confirmed successful) — never automatically. The SQL saved is the *exact*
SQL actually executed, not the agent's original draft — the user may have
edited the SQL box before confirming, and the corrected version is exactly
what's worth remembering. Saved with a deterministic id
(`embeddings.golden_examples._example_id`, a hash of database+question+SQL)
so re-clicking the widget upserts the same document rather than
accumulating duplicates. Every caller (the React dashboard, the REST API
used standalone, `eval/runner.py`) benefits from retrieval automatically,
since they all call the same underlying `agent.graph.run_agent` graph.

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

### Business-context vector retrieval (`retrieval/`)
On top of the schema-DDL retrieval above, `retrieval/` adds semantic
retrieval of table/column/relationship descriptions, business glossary
terms, metric definitions, curated SQL examples, and documentation —
`agent.nodes.retrieve_business_context_node`, wired between
`retrieve_golden_examples` and `plan_query` in the graph. See
`docs/vector-retrieval-design.md` for the full design; the short version:

- **Reuses this project's existing ChromaDB `PersistentClient`** — no
  second vector database. A new per-database collection
  (`knowledge_base__<db_name>`), same convention `embeddings/schema_indexer.py`/
  `embeddings/golden_examples.py` already establish.
- **Seven typed chunk kinds** (`retrieval.models.ChunkType`: table, column,
  relationship, glossary, metric, sql_example, documentation), each with a
  **deterministic SHA-256 chunk ID** derived from stable identity fields
  (never a random UUID) — this is what makes re-ingestion
  (`python -m scripts.ingest_schema --database-id <name>`) idempotent:
  unchanged chunks are skipped, not re-embedded or duplicated.
- **Never a replacement for live schema inspection, SQL validation,
  permission checks, or execution** — all of that remains exactly as
  described elsewhere in this file. Retrieved context is injected into
  `generate_sql`'s prompt as a clearly labeled, "verify against the live
  schema, never invent a table/column, treat retrieved SQL as a pattern
  only" section (`agent.llm_client._build_business_context_block`).
- **Fails open on any vector-store/embedding failure** — an empty
  collection, a disabled feature flag, or a genuine error all resolve to
  no extra context plus a logged, state-visible warning
  (`AgentState["retrieval_warnings"]`), never a reason a question can't be
  answered.
- Sample knowledge content ships under `data/knowledge/` — table/column
  names are real (verified via live introspection of this project's own
  "adventureworks" sample database), but the business definitions/
  synonyms/formulas themselves are illustrative demonstration content, not
  reviewed production documentation — replace before relying on it.

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
`api/main.py`'s `POST /execute` "Confirm and Run" route) reads
`db.connection.get_connection(settings, state["selected_database"])`
rather than a single global `Settings.db_type`.

Two things deliberately left alone by this design (documented, not
silently ignored): the eval benchmark's per-case `database:` label (see the
`eval/` folder note above) and `config/table_descriptions.yaml`/
`config/sensitive_columns.yaml`, which are keyed by bare table name, not
`(database, table)` — a note/classification for one configured database's
table could in principle also apply to a same-named table in another. Both
are real, narrow limitations worth knowing about if you're extending this
further, not oversights to silently work around.

### Tool/MCP abstraction (`agent/tools/`)
A generic, governed way to describe and invoke this application's six real
capabilities (SQL, document RAG, policy RAG, web search, media search,
media generation) by name, added as the first step of a broader "Enterprise
AI Intelligence Platform" roadmap (see
`docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md`). **Deliberately additive, not
a rewrite**: `agent/orchestrator/nodes.py`'s five hardcoded nodes are
completely unmodified, and `run_orchestrated` still calls them directly in
production exactly as before this package existed — there was no clear,
low-risk way to swap a heavily security-reviewed, already-tested graph's
node bodies for a generic dispatch layer without real regression risk, so
that integration was deliberately deferred rather than forced into this
increment (see "Do not blindly follow this sequence" reasoning in the
assessment doc).

Each of the six `Tool`s in `agent/tools/definitions.py` (`sql_query`,
`document_search`, `policy_search`, `web_search`, `media_search`,
`media_generation`) wraps the *exact same* function
`agent/orchestrator/nodes.py` already calls (`agent.graph.run_agent`,
`rag.graph.run_rag`, `search.web_search.web_search`,
`media.search.search_media`, `agent.orchestrator.nodes.execute_generation`)
— no reimplementation, confirmed by tests that monkeypatch each real
function and assert the tool called through to it with the right
arguments (`tests/test_tools_definitions.py`). `agent/tools/registry.py`'s
`ToolRegistry.execute` governs every call uniformly: a permission check
(`agent.authz.has_role_permission`, the same check `router_node` already
does for its own sources — fail-closed, raises `ToolPermissionError`, never
silently denies), a hard per-attempt timeout (thread-based, mirroring
`db.execution._execute_with_timeout`'s own daemon-thread-join pattern —
the only cross-platform way to bound an arbitrary call's wall-clock time),
a configurable retry policy (`max_attempts=1` by default — most wrapped
functions already have their own internal retry loop, so an outer retry
would just multiply latency), and one structured `security.audit_log`
event per outcome (`tool_permission_denied`/`tool_executed`/
`tool_execution_failed`). `ToolCategory.READ`/`WRITE` distinguishes the
five read-only sources from `media_generation` (the only one that spends
real, metered money) — the category alone grants no extra authorization;
`media_generation`'s own human-approval gate (`Settings
.require_generation_approval`, see "Media generation" above) still lives
entirely in `generation_node`/`execute_generation`, upstream of this
tool.

**Who consumes this today**: nothing in production yet — this is
foundation for the next roadmap step (a research-planner upgrade that
needs to choose and invoke a tool dynamically by name, rather than
following a fixed LangGraph edge, plus a future external MCP client). It
is fully tested (`tests/test_tools_registry.py`,
`tests/test_tools_definitions.py`) and functionally complete on its own —
not scaffolding that does nothing — but has no caller wired into the live
request path in this increment. Known, disclosed limitation: per-tool
timeouts (`agent/tools/definitions.py`'s `_SQL_TOOL_TIMEOUT_SECONDS` etc.)
are hand-picked constants, not yet wired to `config.settings.Settings`,
to keep this increment's surface area small — a legitimate follow-up, not
an oversight.

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
`api/main.py` calls, and it is deliberately a **two-path function, not a
graph with one trivial branch**:

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
  ...), which is why every existing `api/schemas.py`/frontend read site
  keeps working whether a question went through `run_agent` directly or
  through the orchestrator. `synthesis_node` is a pure pass-through when only one
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
into an answer at all — a fixed, reviewed set of categories this app has
no per-caller override for, hard-blocked regardless of role, the same
fail-closed philosophy as `agent/sql_validator.py`'s
`SAFETY_VIOLATION_TYPES`, applied to a different data shape (a
document/chunk tag instead of a `(table, column)` pair). **2026 Phase 3:**
a second, independent gate, `DocumentRecord.restricted_roles` (an optional,
operator-set role list, entered at upload time), layers actual RBAC
(`agent/authz.py`, see "Authentication, authorization, and the 2026
security hardening passes" below) on top of this for documents that need
finer-grained restriction than the three fixed categories — checked by the
same `generate_node` before the LLM ever sees the chunk, and by
`api/documents.py`'s download route. Both gates are independent and
additive; a document can carry either, both, or neither. `uploaded_by` was
added alongside `restricted_roles` but is audit-trail only, never used to
scope retrieval/download/delete — this remains a shared knowledge base by
design, not per-uploader private storage.

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
(`frontend/src/components/chat/SourceAnswerCard.tsx`) or the Knowledge
Sources management page, never fetched into a listing/search query itself
— both
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
- **Video clip length is a real model-capability ceiling, not a config
  restriction this app imposes.** `Settings.media_gen_video_duration_seconds`
  (optional) overrides the video model's own default "duration" form field
  via `create_and_poll`'s new `form_overrides` param — but verified live
  against a real IMA account (a read-only, no-cost `GET /open/v1/product/list
  ?category=text_to_video` call): the auto-selected model ("Seedance 2.0")
  only accepts an integer 4-15 (its own declared `form_config` min/max),
  default 5. No IMA video model on this account, or as far as this project
  has confirmed offered by IMA at all, supports a single multi-minute
  generation — current text-to-video models generally cap in the 5-15
  second range per call. An out-of-range value is rejected by IMA itself as
  a clean provider failure, not pre-validated against the live per-model
  range here.
- **Generated media is served through this app, never the provider's raw
  CDN URL.** `execute_generation` (the function that actually calls IMA —
  see "Human-in-the-loop approval gate" below) downloads the bytes once
  (`media_gen.download.download_media_bytes`, SSRF-hardened — see its own
  module docstring; **2026 Phase 3:** redirects are now followed manually,
  re-validated hop-by-hop up to 5 times, closing a gap where a redirect
  response from an otherwise-validated URL reached an unvalidated address
  with no check at all) and stores them under an opaque id in a bounded,
  process-lifetime in-memory cache (`media_gen.cache.MediaCache`, FIFO
  eviction past 100 entries, not persisted — a restart loses in-flight
  generated media, an accepted tradeoff same as this app's other
  process-global caches). Neither `MediaGenerationResult.answer` nor the
  API's `MediaGenerationResultOut` ever carries the raw URL — only
  `media_id`. The React frontend fetches the bytes via the authenticated
  `GET /media/{media_id}` (`api/media.py`, mirroring `api/documents.py`'s
  PDF download route) and renders an `<img>`/`<video>` from a blob object
  URL (`MediaResultCard.tsx`). This was chosen over persisting to a DB
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
- **Multi-source synthesis doesn't embed generated media inline.**
  `synthesis_node` only ever concatenates text; a generation result folded
  into a multi-source answer shows only its (link-free) confirmation text,
  not the image/video itself. Single-source generation (the realistic
  case) is unaffected.

Update (this feature's original "Known gaps" note above used to read: *"No
'search existing media' capability exists... Building a real media-search
tool (an index, a store) is a separate, larger feature, not attempted
here."* That gap is now closed — see "Media search" below.

### Media search (image/video, optional, off by default) (`media/`)
Content-based search over an **untagged** local image/video library — no
filenames, no manual tags. Routed as the "media_search" orchestrator
source (`agent.orchestrator.nodes.media_search_node`), gated by
`ENABLE_MEDIA_SEARCH` + a real `MEDIA_LIBRARY_PATH` (both required —
same flag-plus-config pattern as media generation's own
`enable_media_generation`/`ima_api_key`). Off by default, unlike voice
mode: it needs a configured library path and pulls in a real, meaningfully
larger dependency footprint (`torch`, `opencv-python`) that shouldn't land
on every fresh clone uninvited.

**Local CLIP embeddings by default, not a hosted API.** This is a
deliberate deviation from a common assumption for this kind of feature
(the original implementation prompt for this asked for "a hosted
multimodal embedding API," listing Voyage/Vertex/OpenAI as candidates).
Given this project's consistent "local-first, cloud only as a disclosed
opt-in exception" identity (Ollama for the LLM, faster-whisper+Piper for
voice mode both chosen explicitly over cloud alternatives), the default
path instead uses `sentence-transformers`' `clip-ViT-B-32` running fully
on-device (`media/embedding.py`) — no API key, no per-image cost, no
media content ever leaving the machine. The **same** model embeds both
images and query text, which is what guarantees they land in one
comparable vector space; this is why `media/embedding.py` computes
embeddings directly rather than through Chroma's own text-only
`EmbeddingFunction` callback interface the way `embeddings/schema_indexer
.py`/`rag/embedding.py` do. Built behind a small provider map
(`Settings.media_embedding_provider`, shaped like `search/web_search.py`'s
`SUPPORTED_SEARCH_PROVIDERS`) so a hosted provider could be added later as
a second dict entry, without touching any call site — nothing exercises
that path today. This is the one real exception to this project's
otherwise-consistent "no torch" dependency posture (`faster-whisper`/
`piper-tts`'s own requirements.txt comments both explicitly celebrate
avoiding it) — a real, consequential tradeoff, not an oversight.

**Vector store: the same ChromaDB this project already depends on**, not
a new vendor (Pinecone/Qdrant/pgvector) — this app is explicitly
"single-user, local-dev oriented" per `README.md`'s own Limitations
section, so new vector-DB infrastructure would add real operational
weight for no benefit at this scale. Two collections
(`media/store.py`'s `media_images`/`media_video_segments`), mirroring
`embeddings/golden_examples.py`'s "one collection per distinct purpose"
convention — reusing the same process-lifetime-cached `PersistentClient`
(`embeddings.schema_indexer.get_chroma_client`) every other Chroma-backed
module here already shares, for the same "only one `PersistentClient` per
on-disk directory per process" reason that caching exists.

**Video pipeline**, per file ingested by `scripts/build_media_index.py`:
1. **Scene-change keyframing** (`media/keyframes.py`, via `PySceneDetect`)
   — segments a video at real scene boundaries, not fixed intervals, so a
   long continuous shot isn't indexed as many near-identical frames.
   `PySceneDetect` requires `opencv-python` unconditionally at its current
   pinned version (confirmed against its own PyPI `requires_dist`
   metadata before pinning — `av`/PyAV is only an optional extra for its
   separate clip-export feature, not a way to avoid OpenCV here).
2. **ASR** (`media/transcription.py`) — reuses `voice/stt.py`'s
   faster-whisper model loader directly (`voice.stt.get_whisper_model`,
   a small refactor extracted specifically for this reuse) rather than
   loading a second Whisper model instance; per-Whisper-segment
   timestamps are bucketed against each detected scene's own time range
   (`transcript_for_range`), not just flattened into one string.
3. **OCR** (`media/ocr.py`, via `pytesseract`/Tesseract) — on-screen text
   in the representative keyframe. Needs the system Tesseract binary
   installed separately (see "Windows-specific notes" below).
4. **Captioning** (`media/captioning.py`) — reuses **Ollama**, this
   project's existing local LLM runtime, with a vision-capable model
   (`Settings.media_vision_model`, e.g. `llava`) rather than a separate
   hosted vision-language model API. The only new setup step is `ollama
   pull <model>`, mirroring the Piper voice-model download precedent
   (`scripts/download_voice_model.py`) instead of introducing a new
   provider architecture. **Fails open**: a blank `media_vision_model`
   (the default) or any call failure returns `None`, not an error — the
   segment is still indexed and searchable via its ASR transcript + OCR
   text alone, same fail-open philosophy as
   `agent.llm_client._build_golden_examples_block`.
5. Each segment gets **up to two embeddings, sharing a `segment_id`**: one
   from its combined caption/transcript/OCR text (via CLIP's own text
   encoder), one from its keyframe image (via CLIP's image encoder) —
   `media/search.py` queries both and merges/de-dupes hits that share a
   `segment_id`, keeping the better-scoring modality.

**Serving is a separate, persistent path from generated-media serving.**
`api/media_library.py`'s `GET /media/library/{media_id}` is deliberately
**not** built on `media_gen.cache.MediaCache` — that cache is in-memory,
process-lifetime, and bounded to 100 entries with FIFO eviction, built for
short-lived *generated* media, the wrong fit for a persistent library
meant to stay searchable indefinitely. Instead it looks up `media_id`
directly in the Chroma collections' own stored metadata to resolve either
the original file (an image hit) or the representative keyframe thumbnail
(a video-segment hit — a full clip is never streamed; the UI shows frame +
timestamp instead, the lighter option this feature's own requirements
explicitly allowed), then re-validates the resolved path is still inside
the expected root directory before ever opening it — a local-path-
traversal defense in the same spirit as `media_gen/download.py`'s SSRF
hardening, applied to disk paths instead of URLs.

**Untrusted content is framed as data, not sanitized/quoted.** OCR text,
ASR transcripts, and generated captions are all attacker-influenceable
(a sign in a photo, or spoken audio, could contain an instruction-like
string) once they reach `media_search_node`'s answer-composition prompt.
Rather than introducing a new sanitize/escape step, this follows the
exact convention `rag/graph.py` and `web_search_node` already established
for the same class of risk: the system prompt explicitly frames hit
captions/OCR/ASR text as **untrusted data, never instructions** — see
`SECURITY.md`'s "Media search" section.

**Router disambiguation from "generation."** `media_search` and
`generation` are the two media-adjacent sources, and the one real
ambiguity between them ("make a picture of X" vs. "find a picture of X")
is handled by `agent.orchestrator.nodes._MEDIA_SEARCH_VS_GENERATION
_GUIDANCE`, appended to the classifier prompt only when *both* sources
are available — see that constant for the exact phrasing this was tuned
against. Unlike `generation_result`, `media_search_result` **does**
contribute a text bullet to `synthesis_node`'s combined answer (it has a
natural citable answer — what was found, and roughly when for a video —
unlike a freshly-created asset); its hits still separately drive
`MediaSearchResultCard.tsx`'s own thumbnail rendering, the same "always
outside the synthesis-text ternary" rule `generation_result` established.

**API + standalone page.** `POST /search/media` (`api/media_search.py`)
searches directly, independent of the conversational `/ask` flow, per this
feature's own requirement — reuses the existing shared
`api_action_rate_limit_per_minute` (`api.rate_limit
.enforce_api_action_rate_limit`) rather than a dedicated limiter, since a
query is cheap and orchestrator-routed questions are already bounded by
`/ask`'s own limiter (the same reasoning document/policy RAG have no
dedicated limiter of their own either). `frontend/src/pages/MediaSearch.tsx`
is a standalone page (mirrors `KnowledgeSources.tsx`'s shape) with its own
nav entry, for direct use outside chat.

**Known gaps, named rather than silently left:**
- **No dense-video-captioning quality tuning has been done.** Whichever
  small Ollama vision model is pulled works as-is; no evaluation of
  caption quality across different models/prompts was performed as part
  of building this.
- **The eval harness extension (`eval/media_benchmark/`) ships as an
  empty template**, not a populated dataset — this repo has no checked-in
  media library to grade against. See that package's own `__init__.py`
  and `dataset.yaml` for how to populate it against your own library.
- **A full video clip is never streamed** — only a representative frame +
  timestamp range. Real HTTP range-request video streaming is a
  deliberately out-of-scope follow-up.

### Content moderation gate (mandatory, not a feature flag) (`moderation/`)

Both content-ingestion pipelines — media search's images/video
(`media/ingest.py`) and document/policy RAG's PDFs (`rag/ingestion.py`) —
run every chunk of every file through `moderation/gate.py::moderate_chunks`
before either one ever calls its own store's upsert/insert functions.
**Mandatory whenever `ENABLE_MEDIA_SEARCH` or `ENABLE_DOCUMENT_RAG`/
`ENABLE_POLICY_RAG` is on** — deliberately no `ENABLE_CONTENT_MODERATION`
toggle exists that could silently disable it while leaving either pipeline
on; missing provider/store config fails ingestion closed
(`moderation.exceptions.ModerationNotConfiguredError`), mirroring
`rag.store.RagStoreNotConfiguredError`'s existing pattern.

**Chunking, per type, before moderation ever runs** (a classifier has
input limits, and checking only a whole asset can miss a problem buried in
one part of it):
- **Image**: one chunk, unless it exceeds `MEDIA_IMAGE_TILE_THRESHOLD_PX`
  in either dimension (default 2048px), in which case it's split into an
  NxN grid of temp-file tiles first (`media/ingest.py::_tile_image_for_moderation`)
  — a classifier's own internal downsampling could otherwise shrink away a
  small region of concern in a very large image.
- **Video**: one chunk per already-detected scene segment (reusing
  `media/keyframes.py`'s existing segmentation, no second pass) — both the
  keyframe image and its combined caption/transcript/OCR text are checked.
  `media/ingest.py::_ingest_video` was restructured into two phases for
  this: extract-and-moderate-every-segment-first, then only if *all* of
  them pass does the existing embed+store loop run (reusing phase 1's
  already-computed transcript/OCR/caption text, not redoing that work) —
  per the decision rule below, one rejected segment blocks the whole video,
  so nothing may be stored until every segment has been checked.
- **PDF**: one chunk per page's text, plus one per embedded image
  (`pypdf`'s `page.images`). A page flagged likely-scanned (the existing
  `_OCR_SUSPECT_CHAR_THRESHOLD` heuristic, previously just a warning shown
  to the uploader) is now actually rasterized (`pymupdf`, a new dependency
  chosen over `pdf2image` specifically because it needs no system Poppler
  binary) and OCR'd via the **existing** `media.ocr.extract_text`, reused
  as-is rather than duplicated — the OCR'd text is used both for
  moderation and for the real retrieval chunk that page contributes, so a
  scanned page that passes moderation is also now actually searchable
  (previously it indexed as an empty, unsearchable chunk).

**Decision rule: a hard-reject on any chunk rejects the entire asset —
never a partial ingestion.** Every chunk is still checked (not
short-circuited on the first hit, so the audit trail is complete via
`security.audit_log.log_security_event`), but nothing from a rejected file
is ever embedded or stored anywhere. This required reordering
`rag/ingestion.py::ingest_pdf` specifically: the pre-moderation flow called
`rag/store.py::insert_document` (which can itself persist the file's raw
bytes, if `ENABLE_PDF_DOWNLOAD` is on) *before* any content check ran — a
real gap where a rejected file's bytes could already be sitting in the
store ahead of the rejection being known. Now nothing touches
`rag/store.py` until moderation has passed; a rejected PDF never gets a
`rag.documents` row at all.

**Taxonomy and its honest limits** (`moderation/taxonomy.py`). Azure AI
Content Safety (the only provider implemented, `moderation/provider.py`,
called via its REST API with `httpx` — no vendor SDK, matching
`search/web_search.py`/`media_gen/client.py`'s existing pattern of calling
a provider's REST endpoint directly) checks four real harm categories:
Hate, SelfHarm, Sexual, Violence, each scored 0/2/4/6, hard-rejecting at or
above `MODERATION_SEVERITY_THRESHOLD` (default 4). Azure has **no
dedicated "weapons," "drugs," or "synthetic/AI-generated media" category**
— stated honestly rather than glossed over:
- **Weapons**: a coarse Violence-category proxy for imagery (Azure can't
  distinguish "weapon" from "violence" generally) plus a custom text
  blocklist (`config/moderation_blocklist.yaml`, loaded by
  `config/moderation_blocklist.py`, mirroring `config/sensitive_columns.py`'s
  exact hand-authored/read-fresh loader pattern) against OCR/caption/
  transcript/extracted text.
- **Drugs**: the same text blocklist only — no visual signal at all in
  this implementation.
- **Synthetic/manipulated ("hallucinated"/deepfake) media**: a
  **disclosed placeholder**, not a real detector. Every image/video chunk
  is recorded as `"not_checked"` for this category rather than silently
  omitted or falsely presented as covered — no mainstream moderation API
  reliably classifies this today. Deliberately a soft-flag, never a
  hard-reject, even once a real detector is eventually plugged in
  (deepfake classifiers have real, well-documented accuracy limits; an
  auto-rejected false positive on real user content was judged worse than
  under-flagging). See `docs/RISK_REGISTER.md`'s R-014.

**A new, disclosed exception to "fully local."** Unlike every other model
in this stack (Ollama, local CLIP, `faster-whisper`/Piper), accurate
content moderation has no comparable on-device option today — this gate
calls a third-party cloud API for every chunk of every ingested file. The
provider is pluggable (`moderation.provider.SUPPORTED_MODERATION_PROVIDERS`,
shaped exactly like `search/web_search.py::SUPPORTED_SEARCH_PROVIDERS`),
so this is a deliberate, named tradeoff (see `docs/RISK_REGISTER.md`'s
R-013), not an unexamined one — the design note for this feature stated
the tension with this project's local-first posture explicitly before any
code was written, rather than silently picking a path.

**Metadata store (`moderation/store.py`), dedupe, and pooling.** A
dedicated SQL Server connection (`MODERATION_STORE_CONNECTION_STRING`) —
deliberately separate from both `DB_CONNECTIONS` and
`RAG_STORE_CONNECTION_STRING` even though this table covers PDF assets
too, since media search is independently toggleable from document/policy
RAG and naming the shared setting after RAG specifically would be
confusing when media-only search needs it — mirrors `rag/store.py`'s exact
shape (a `@cache`-decorated `create_engine` call, idempotent
`ensure_schema`, raw `text()` SQL, functions taking `engine: Engine`
explicitly). One table, `moderation.media_assets`, keyed by content hash
(`file_hash`, a unique index) — the dedupe lookup every ingestion call
starts with: a previously-seen hash (whether it passed or was rejected)
short-circuits before any extraction/moderation/embedding work, the
feature's main performance win. `record_asset` is delete-then-insert
(upsert-by-hash), not a bare `INSERT`, so a `force=True` re-run (bypassing
the dedupe check on purpose) replaces the same content's old record rather
than failing on the unique-index collision. Unlike `db/connection.py`/
`rag/store.py` (both left on SQLAlchemy's `QueuePool` defaults of 5/10),
this engine sets `pool_size`/`max_overflow` explicitly
(`MODERATION_STORE_POOL_SIZE`/`_MAX_OVERFLOW`, default 10/20) since
ingestion can run many concurrent DB writes.

**Concurrency: this project's first bounded thread pool.** Before this,
`scripts/build_media_index.py` was a plain sequential `for` loop; there
was no background-job/task-queue/worker-pool infrastructure anywhere in
this codebase (only a one-shot `threading.Thread` + `join(timeout)` idiom
in `db/execution.py`/`db/query_cost.py`, used purely for hard query
timeouts). Building a real async job queue (Celery/RQ) was judged heavier
infrastructure than this project's stated scale justifies, so
`scripts/build_media_index.py` instead gained a bounded
`concurrent.futures.ThreadPoolExecutor` (`MEDIA_INGEST_WORKERS`, default
4) — I/O-bound work (the moderation API call, DB writes) benefits from
threads despite the GIL. The PDF upload path (`api/documents.py`) needed
no equivalent change: FastAPI already dispatches each sync request handler
to its own worker thread, so concurrent uploads already ran in parallel;
only the connection pool sizing above matters there.

**Known, narrow limitation, named rather than silently left:** the PDF
dedupe short-circuit is keyed purely on content hash, not `(hash,
collection, sensitivity_category)`. Re-uploading byte-identical PDF
content to a *different* collection or with a different sensitivity tag
than its first upload reuses the first upload's existing `rag.documents`
row (collection/tag included) rather than creating a second one for the
new intent — a deliberately accepted tradeoff for the common case (skip
redundant moderation/embedding entirely for a true duplicate), not
silently mishandled.

### Malware scanning (`security/malware_scanner.py`)
Added in a 2026 dependency/CI-hardening pass, closing what every prior
file-upload security document in this repo (`docs/FILE_UPLOAD_FINAL_REPORT.md`,
`SECURITY_FINAL_REPORT.md`) named as the single most significant
remaining gap: no `MalwareScanner` abstraction existed anywhere in
source. Scans a file's **raw bytes**, before any parser
(`pypdf`/`pymupdf`/Pillow/PySceneDetect) touches them — a real, separate
concern from `moderation/gate.py`'s content-harm checks, which only run
*after* parsing/extraction. Wired into both `rag/ingestion.py::ingest_pdf`
and `media/ingest.py::ingest_file`, right after the existing
dedupe-by-hash check and before any real parsing begins.

Pluggable the same `SUPPORTED_..._PROVIDERS`-dict way every other external
integration in this codebase is (`moderation/provider.py`,
`search/web_search.py`) — `Settings.malware_scan_provider`
(`"disabled"`/`"clamav"`). Only ClamAV is implemented, spoken to directly
over `clamd`'s own `INSTREAM` wire protocol via a plain socket (no
`pyclamd`/vendor SDK, matching this codebase's usual "call the provider's
own protocol directly" convention).

**Deliberately off by default (`"disabled"`) — the one real difference
from `moderation_provider`'s "mandatory, no feature flag" posture**, and a
conscious choice, not an oversight: no scanning capability existed before
this module, so defaulting it "on" would have silently broken every
existing document/media upload path for any deployment that hasn't stood
up a ClamAV daemon (most deployments, today). Once an operator opts in
(`MALWARE_SCAN_PROVIDER=clamav`), behavior is genuinely fail-closed: an
infected result, an unreachable/timed-out daemon, and a malformed
response are all treated as a rejection (`security.malware_scanner
.ScanResult.blocked`) — "unknown" is never silently treated as "clean."
`"disabled"` is still audit-logged per upload (`malware_scan_skipped`),
not silently skipped.

**Known limitation, named rather than hidden**: this abstraction's
fail-closed logic is unit-tested against a mocked `clamd` socket
(`tests/security/test_malware_scanner_gate.py`, 18 tests) but has never
been exercised against a real `clamd` daemon in this project's own
history — see `docs/security/FINAL_PRODUCTION_GATE.md`'s Malware Scanning
row (`PARTIAL`, a named P0 for any deployment that enables
`ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG`/`ENABLE_MEDIA_SEARCH` without
also configuring a real scanner).

### Voice mode (speech input/output) (`voice/`)
Optional, **on by default** (`ENABLE_VOICE_MODE=true`) — unlike media
generation, this feature spends no money and makes no external network
call once its one-time local model downloads are done, so there's no
cost-control reason to make it opt-in the way `ENABLE_MEDIA_GENERATION`
is. Spoken questions are transcribed via `faster-whisper`
(CTranslate2-based Whisper), spoken answers synthesized via Piper. Both
run fully local inference (no torch
pulled in by either — `faster-whisper` uses `ctranslate2`, Piper uses
`onnxruntime`, already a transitive dep via chromadb), matching this
project's "Ollama, not a hosted LLM" posture: no cloud API, no data
leaving the machine, no API key required for voice mode either.
`voice/stt.py`/`voice/tts.py` each wrap their backend behind a single
`transcribe()`/`synthesize()` call so either could be swapped later
(whisper.cpp, Coqui TTS) without touching `api/voice.py`.

**A transcribed question is never treated specially.** `POST
/voice/transcribe`'s result is submitted through the exact same `POST
/ask` path a typed question uses — `agent.input_guard.check_input`
therefore applies unconditionally, with no separate code path for voice
input to bypass.

**One voice turn, inline in the composer — record once, review, then the
user presses Send.** `frontend/src/hooks/useVoiceConversation.ts` drives
listen → transcribe (`idle` → `listening` → `transcribing` → back to
`idle`, plus a `speaking` phase while a spoken answer plays back) per
mic-button click. There is **no separate takeover card** —
`ChatInput.tsx` keeps its normal textarea/mic/send layout the whole time;
the mic button itself toggles in place to a stop button while
`phase === 'listening'` (same position, never a second button), and
while `isTranscribing`/`isSpeaking` it shows a small spinner/volume icon
instead. While listening, the textarea is read-only and shows the live
interim caption (see "Live captions" below) in place of its real value,
so the box visibly "types" what it hears; once the local
`POST /voice/transcribe` result comes back, `useVoiceConversation` hands
its **raw `text` only** (never `corrected_text`, the optional AI-cleaned
rewrite — see `voice/correction.py`) to `ChatInput` via an
`onTranscribed` callback, which drops it straight into the textarea,
exactly as if it had been typed. From there it's an ordinary editable
question: nothing is auto-submitted, and the existing Send button (or
Enter) is the only confirmation step — this is what makes "confirm before
sending" fall out of the same UI a typed question already uses, rather
than needing a dedicated review screen. `ChatInput` tags the question as
voice-originated (`QueryHistoryEntry.originatedFromVoice`) as long as the
box still holds that transcript, including through manual edits — only
clearing the box and typing fresh from empty drops the tag. A failed turn
(mic denied, transcription error) surfaces its message inline under the
composer (`voice.error`) and leaves the textarea usable, rather than
lingering in any special state.

**Autoplay warm-up for the very first spoken answer.** The gap between
the mic-button click and the actual `<audio>.play()` call in
`playAnswer` can be several seconds (recording + local transcription +
the agent's own LLM round trip), which is long enough that some
browsers no longer treat that later, code-triggered play as tied to the
original click and silently block it. `start()` works around this by
calling `.play()` synchronously inside the click handler itself, on a
~0-byte silent WAV (`SILENT_AUDIO_SRC`), on the same `<audio>` element
`playAnswer` reuses — a real, gesture-attributed play call that "warms
up" audio for that tab before the actual spoken answer is ready.

**Live captions are a disclosed, deliberate exception to "fully local."**
While listening, `frontend/src/hooks/useSpeechRecognition.ts` wraps the
browser's built-in `SpeechRecognition`/`webkitSpeechRecognition` Web
Speech API purely to show word-by-word interim captions as the user
talks ("typing itself simultaneously"). In Chromium-based browsers this
API sends microphone audio to the browser vendor's own cloud speech
service — a real, narrow exception to this feature's otherwise-local
STT/TTS design, chosen deliberately (offered to and picked by the user
over a laggier fully-local chunked-transcription alternative) because no
local model can produce true instant word-by-word captions from a
streaming batch architecture like `faster-whisper`'s. The caption is
**never** what gets submitted — the local Whisper result from `POST
/voice/transcribe`, run once the browser detects end-of-utterance,
remains the sole authoritative transcript; the live caption is discarded
the moment it comes back. `continuous: false` on the recognizer is what
ends listening automatically (the browser's own `onend` fires on
detected silence) without a manual stop button. Browsers without this API
(`useSpeechRecognition().isSupported === false`) get no live caption and
no auto-stop signal — the textarea shows a static "recording" placeholder
instead, and the mic-turned-stop button is the only way to end listening
in that case; local transcription and TTS playback are unaffected either
way.

**Schema-aware transcription accuracy.** `voice.stt._build_vocabulary_hint`
introspects every configured database's table/column names (reusing
`db.connection`/`db.schema_introspection`, no new engine) and feeds a
short, deduped, length-capped (`Settings.stt_vocabulary_max_chars`)
comma-joined string to Whisper's `initial_prompt` — biases recognition
toward real schema terms ("branch_id", "dispute") instead of
similar-sounding common words. Computed fresh per call rather than cached,
since introspection is already cheap and this isn't a hot path; fails
open (returns `""`, logs a warning) on any introspection error, the same
fail-open posture `agent.llm_client._build_golden_examples_block` already
has for its own accuracy-only aid.

**Security.** A recorded upload is capped by both size
(`Settings.voice_max_upload_mb`, enforced the same read-and-reject-if-over
way `api/documents.py::upload_document` caps a PDF) and duration
(`Settings.voice_max_duration_seconds`, checked by cheaply probing the
decoded audio's length via PyAV *before* running the comparatively
expensive Whisper model, not after). `POST /voice/synthesize`'s input
text is capped by the existing `Settings.max_question_length`, reused
rather than duplicated.

**What gets spoken back.** A voice-originated turn that succeeds gets a
spoken answer, in priority order: `state.insight` (SQL path, if the
insight feature produced one) → `state.synthesized_answer`/the relevant
per-source answer (multi-source path) → a row-count fallback ("Found N
rows.") — never silent on success. Origin tracking is a plain boolean
(`QueryHistoryEntry.originatedFromVoice` in `frontend/src/lib/history.ts`,
set by `ChatInput.submit()` whenever the textarea still holds a voice
transcript at send time, never by typing) so a typed question can never
trigger `POST /voice/synthesize` at all — not a runtime check, a
structural guarantee. Playback happens once, automatically, via
`useVoiceConversation`'s own `<audio>` element (`playAnswer`, called by
`ChatInput.submit()` right after `askQuestion` resolves) using the same
element `start()` warmed up for the browser's autoplay policy — see
"Autoplay warm-up" below; `TurnCard.tsx` renders the same
`entry.spokenAudioUrl` afterward with `controls` only (no `autoPlay`) so
the user can manually replay it without hearing it spoken twice.

**Piper voice models are a separate one-time download**, same shape as
`ollama pull` — `scripts/download_voice_model.py` (calls
`piper.download_voices.download_voice` directly) fetches
`<voice>.onnx`/`<voice>.onnx.json` from the public `rhasspy/piper-voices`
repo into `voice/models/` (gitignored, like `embeddings/.chroma/`).
Faster-whisper's own model needs no such step — it auto-downloads from
Hugging Face Hub on first use and caches on disk.

**Capability discovery.** `GET /health` carries a `voice_enabled` field
(`Settings.enable_voice_mode`); the React dashboard only shows the mic
button and its own settings toggle
(`HistorySettingsSection.tsx`/`settingsStore.voiceModeEnabled`, persisted
like theme/accent) when the server says the feature is actually
available — the "all-or-nothing infra flag, plus a per-session UI switch"
shape.

### Authentication, authorization, and the 2026 security hardening passes
Two independent layers, both worth understanding before touching `api/`,
`agent/orchestrator/`, or `rag/` — the "no per-user authorization system"
phrasing that appears in a couple of older notes elsewhere in this file
predates both and is no longer accurate for anything gated by
`agent/authz.py`'s permissions:

- **Authentication** (`security/oidc.py` + `api/auth.py`) — one dispatch
  point, `Settings.auth_mode`: `none` (default; unchanged behavior for
  local/single-user use), `static_token` (`API_AUTH_TOKEN`, a shared
  bearer secret, constant-time compared — grants a fixed "admin"-equivalent
  identity, since a single shared secret has no natural sub-identity to
  scope down), or `oidc` (`OIDC_ISSUER` set — validates a JWT against any
  standard-compliant identity provider: server-side algorithm allowlist
  never trusting the token's own `alg`, mandatory audience validation,
  bounded clock skew, no raw token/claim ever logged). `ENVIRONMENT=production`
  fails closed at startup (`config/settings.py::_require_identity_in_production`)
  if neither is configured. Full detail: `docs/AUTHENTICATION.md`.
- **Authorization** (`agent/authz.py` + `api/authz.py`) — RBAC: 15
  fine-grained permissions, 4 extensible default roles (`viewer`/`user`/
  `analyst`/`admin`), an unrecognized role grants zero permissions (fail
  closed). Wired into every data-touching/expensive route **and** the
  multi-source router itself — `agent/orchestrator/nodes.py`'s
  `router_node` filters the LLM's own source selection through a
  permission check *before* any subgraph runs, so a routing decision the
  LLM makes can request access but never unilaterally grant it. Full
  detail and the resource-to-permission mapping: `docs/AUTHORIZATION.md`.
- **Local self-hosted accounts** (`identity/`, a 4th `auth_mode` value,
  `Settings.local_auth_enabled` — see `docs/AUTHENTICATION.md`) — this
  app's own accounts: Argon2id password hashing, locally-issued JWT access
  tokens + rotating opaque refresh tokens (reuse-detection revokes the
  whole session family, `identity/repositories/sessions.py`), account
  lockout, password reset, email verification. A local user's role names
  (`viewer`/`user`/`analyst`/`admin`) feed straight into the *same*
  `AuthIdentity.roles`/`agent/authz.py` RBAC bridge described above — zero
  changes needed to any existing AI/RAG/SQL route's authorization check.
  **Display name is mandatory at sign-up** and **passwords must clear a
  real strength policy** (`identity/password_policy.py` — common-password/
  sequential-digit/keyboard-walk/identity-fragment checks, 12-64 chars,
  never silently truncated) — see
  `docs/authentication-and-password-policy.md`.
- **Frontend OIDC login** (2026 Phase 3, `frontend/src/lib/auth.ts` +
  `store/authStore.ts`) — a real Authorization Code + PKCE flow
  (`oidc-client-ts`), in-memory-only token storage (`InMemoryWebStorage`,
  never `localStorage`/`sessionStorage`, so an XSS payload can't read a
  persisted token — the tradeoff is a hard refresh clears it, so app load
  always attempts a silent hidden-iframe re-auth against the IdP's own
  session first). Closes a real gap: before this, the SPA's only possible
  credential was `VITE_API_AUTH_TOKEN`, a build-time-baked, admin-granting
  shared secret extractable from the public JS bundle. A pure pass-through
  (`AuthGate.tsx` renders `children` directly) unless
  `VITE_OIDC_AUTHORITY`/`VITE_OIDC_CLIENT_ID` are set — zero behavior
  change for the common local/single-operator deployment. **Not yet
  verified against a live identity provider** in this environment — passes
  every static check available (`tsc`, `oxlint`, `npm run build`) and
  follows `oidc-client-ts`'s documented API shape, but no real IdP/browser
  was reachable to exercise the interactive flow end-to-end; see
  `docs/AUTHENTICATION.md`'s own disclosure before relying on it.
- **DB write-privilege check is now startup-enforced, not just a manual
  CLI check** (2026 Phase 3, `api/main.py::_enforce_database_write_privileges`).
  `db.connection.check_write_privileges` existed since an earlier phase but
  was only ever called from `scripts/test_db_connection.py` — a deployment
  that never ran that script by hand got no signal that its supposedly
  read-only `DB_USER` wasn't. Now runs once per configured database at
  `lifespan` startup; refuses to start (`ConfigurationError`) if
  `ENVIRONMENT=production` and any database's connected role appears to
  hold write privileges, warns otherwise. Doesn't change the layering
  described in "True read-only enforcement is layered, not just
  code-level" below — it makes a misconfigured DB-role layer detectable
  and startup-blocking instead of silent.
- **Security headers / CSP** (2026 Phase 3, `api/main.py::_add_security_headers`,
  on by default via `Settings.enable_security_headers`) — HSTS,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy` (every sensor denied except
  `microphone=(self)`, for voice mode), and a `Content-Security-Policy`
  scoped to what the built dashboard actually loads (no inline `<script>`,
  Google Fonts, `blob:`/`data:` for generated media). `frame-src` is
  derived from `OIDC_ISSUER` when set, since OIDC silent-renew loads the
  IdP in a hidden iframe that a bare `default-src 'self'` would otherwise
  silently block. Override via `Settings.content_security_policy` for a
  deployment this default doesn't fit. Also new this pass:
  `config/settings.py` rejects `CORS_ALLOWED_ORIGINS=*` at startup — a
  wildcard is meaningless (and browser-rejected) combined with this app's
  `allow_credentials=True` CORS setup, caught at config time rather than
  relied on to fail at request time.

See `SECURITY_FINAL_REPORT.md` / `SECURITY_BASELINE.md` /
`SECURITY_CHANGELOG.md` at the repo root for the full 2026 Phase 3 audit
trail (per-control PASS/PARTIAL/FAIL findings, what was fixed vs. what
remains open — notably 24 known CVEs across 7 backend dependencies,
requiring a `langgraph` 0.2→1.0 migration this pass deliberately didn't
attempt) and `SECURITY_PRODUCTION_CHECKLIST.md` for an operator-facing
go/no-go list. `docs/security-changelog.md` carries the dated changelog
entry for this pass alongside every earlier change-controlled security
decision.

**A later, separate engagement (`docs/security/`, not the repo-root
`SECURITY_*.md` files above) picked this up further**: a CI-gate-hardening
pass (bandit/pip-audit/a new `detect-secrets` gate all flipped from
report-only to actually blocking, each backed by an evidence trail rather
than a bare flag flip — see `docs/security/CVE_TRIAGE.md`), the malware
scanner described above, a from-scratch dependency-CVE reachability
re-verification (`docs/security/DEPENDENCY_SECURITY_FINAL_REPORT.md`), a
first Trivy container scan this project has ever had run
(`docs/security/CVE_TRIAGE.md` §4 — 29 new CRITICAL/HIGH findings, 4
confirmed not-reachable, ~17 OS-level only partially verified), and a
final production-readiness gate
(`docs/security/FINAL_PRODUCTION_GATE.md` / `PRODUCTION_SECURITY_READINESS_REPORT.md`)
whose verdict is **NOT READY** — not because a vulnerability was found in
any of the above, but because DAST and a live-IdP OIDC end-to-end test
have never been run in any environment this project has had access to
(`docs/security/DAST_REPORT.md`/`OIDC_E2E_TEST.md`, both honestly
`NOT VERIFIED` rather than assumed), and the malware scanner immediately
above is real but real-world-unverified. `docs/security/INCIDENT_RESPONSE.md`
(previously fully absent, a named gap in every earlier pass) and a
`langgraph` 0.2→1.0 migration assessment
(`docs/security/LANGGRAPH_UPGRADE_SECURITY_ASSESSMENT.md` — assessed, the
migration itself still deliberately not attempted) were also produced in
this engagement. Read `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`
first if you're deciding whether this project is ready to deploy
somewhere real — it is the most current, most rigorously-sourced answer
to that question in this repository, superseding the repo-root
`SECURITY_*.md` files' own bottom line where the two differ.

### Universal server-side chat history (`identity/repositories/history.py`, `api/chat_history.py`)
For a locally-authenticated user (see above), every conversation and
message is now stored permanently in the identity database, not just in
the browser tab's Zustand store — the fix for a real, previously-disclosed
gap: chat state used to be entirely in-memory-only
(`frontend/src/store/chatStore.ts`), so the same signed-in user got a
blank history on a different browser/device, and a page reload cleared it.
See `docs/chat-history-authentication-audit.md` for the full before/after
and `docs/chat-history-architecture.md` for the schema/API/frontend design.

`identity.models.Conversation`/`Prompt`/`AiOutput` (all pre-existing
tables from the identity module's own initial build, previously unused by
any code path) are the storage: `Prompt` is one user question, `AiOutput`
its assistant answer, paired by a per-conversation `sequence_number`
(assigned once, inside one transaction, by
`identity.repositories.history.append_turn` — never a client-supplied
value). `POST /ask` (`api/main.py`) calls this via a small glue module,
`api/chat_persistence.py::persist_ask_turn`, *after* the agent's own
schema-retrieval/generation/validation/execution has already fully run —
**a persistence failure here can never fail the `/ask` response itself**
(wrapped in a broad `except Exception`, logged, degrades silently for that
one turn), the same fail-open posture this codebase already applies to
every other non-critical-path accuracy aid (`plan_query_node`,
`retrieve_golden_examples_node`, `retrieve_business_context_node`).

`api/chat_history.py` exposes `GET/POST/PATCH/DELETE /conversations`,
`GET/POST /conversations/{id}/messages`, and `GET /chat/search` — every
route requires `Depends(require_local_user)` and folds `user_id` directly
into every repository-layer query (never a permission check layered on
top of an unscoped fetch), so a wrong/forged `conversation_id` resolves to
a 404, never confirming another user's conversation even exists (same
account-enumeration-avoidance principle `identity.exceptions
.InvalidCredentialsError` already applies to login). Search
(`identity.repositories.history.search_history`) uses plain, portable
`ILIKE '%term%'` (works identically against this repo's SQLite test engine
and the real PostgreSQL identity database) accelerated by `pg_trgm` GIN
trigram indexes on PostgreSQL — deliberately not `to_tsvector` full-text
search or a separate search engine, given this project's own local-dev-
oriented scale. See `docs/chat-history-search.md`.

**Retention**: logout, session expiry, a browser/device change, or an app
restart never delete chat history — only an explicit, user-initiated
`DELETE /conversations/{id}` does, and even that is a soft delete
(`deleted_at`), never a real `DELETE`. `frontend/src/store/chatStore.ts
::clearHistory()` (called on logout/user-switch, wired from
`AuthGate.tsx`) only clears **in-memory** state — it never calls a delete
API.

**Known limitation, named not hidden**: only a locally-authenticated user
gets this — an OIDC or unauthenticated caller's questions are still
answered normally, nothing is persisted for them (no `identity.users` row
to attach a conversation to). A reloaded past turn also only shows the
answer text + SQL (if any), not the full `AskResponse` (charts,
citations, the schema DDL shown at generation time) — see
`docs/chat-history-architecture.md`'s own "Known limitations" section.

### Frontend UI redesign (2026 UI pass)
A ground-up-in-appearance, additive-in-substance redesign of `frontend/`
into a left-sidebar/main-workspace AI-workspace layout — no backend logic,
API contract, auth flow, SQL validation, or chat-persistence behavior
changed as part of this pass; every change is presentation/interaction
layer only, verified by the pre-existing backend test suite being
untouched and the frontend's own `GET`/`POST` call sites being unchanged.
Four docs carry the full detail so this section stays a pointer, not a
duplicate:

- [`docs/frontend-ui-audit.md`](docs/frontend-ui-audit.md) — the Phase 1
  audit this pass started from: component hierarchy, design
  inconsistencies, and gaps as they stood before any change.
- [`docs/ui-design-system.md`](docs/ui-design-system.md) — the token set
  added to `frontend/src/index.css` (message-role surfaces, code/SQL
  surfaces, a focus-ring token, named z-index tiers, named transition
  durations) and the new shared `components/ui/` primitives (`dialog.tsx`,
  `drawer.tsx`, `toast.tsx`, `copy-button.tsx`), all additive to the
  existing Tailwind v4 + CSS-custom-property system — no new styling
  framework.
- [`docs/chat-history-ui.md`](docs/chat-history-ui.md) — the sidebar/
  settings split: `HistoryDrawer.tsx` (one combined right-side drawer) was
  deleted in favor of `Sidebar.tsx` (persistent left rail, `lg:`+),
  `MobileNav.tsx` (hamburger + drawer below `lg:`, rendering the same
  `Sidebar`), and `SettingsDialog.tsx` (the unmodified
  `HistorySettingsSection.tsx` content, now its own surface). Search/list/
  rename/delete logic was extracted into `hooks/useChatSearch.ts` and
  independently-tested presentational components
  (`ConversationSearch`/`ConversationList`/`ConversationListItem`/
  `ConversationSearchResults`) — the server-backed history/search behavior
  itself (`GET /conversations`, `GET /chat/search`, etc.) is unchanged.
- [`docs/image-editing-architecture.md`](docs/image-editing-architecture.md)
  — a genuinely new capability, **frontend-only and explicitly labeled as
  such in the UI**: `ChatInput.tsx` now accepts image attachments (file
  picker, drag-drop, clipboard paste — `hooks/useImageAttachments.ts`,
  `lib/imageValidation.ts`) with a full local editor
  (`components/image/ImageEditor.tsx`, Konva/react-konva — crop, rotate,
  flip, draw, shapes, text, mask layer, undo/redo). **No backend endpoint
  accepts a chat image attachment or an AI-guided edit instruction** —
  confirmed against `api/schemas.py`'s `AskRequest` (no file field) and
  every `UploadFile` route in `api/` (only PDF documents and voice audio).
  The editor's "AI-guided editing" section is real, visible UI (per the
  original request's instruction not to hide the affordance) but its
  adapter (`lib/imageEditAdapter.ts`'s `AiGuidedEditAdapter`) is a
  deliberate, permanent stub that always rejects with a clear "not
  configured" message — never a fabricated result. The composer shows an
  explicit "Local only — image attachments aren't sent to the assistant
  yet" notice whenever one is attached, so this limitation is visible in
  the product, not just in this file. The backend contract a real
  implementation would need (`POST /media/edit`, mirroring `media_gen`'s
  existing human-approval/cost-ceiling/SSRF-hardened-storage pattern) is
  documented but **not built** — a substantial backend feature in its own
  right, correctly out of scope for a frontend UI pass. The editor is
  lazy-loaded (`React.lazy`, its own ~343KB/106KB-gzip chunk) so attaching
  or viewing an image — or using the app without ever touching images —
  never pays Konva's bundle cost.

**Follow-up pass — duplicated controls + sidebar collapse (still 2026-09-18):**
the redesign above introduced its own new duplication, found and fixed in
a immediately-following pass: `SettingsDialog` was mounted **twice**
(once in `AppShell.tsx`'s header, once in `Sidebar.tsx`'s footer), each
with its own independent open/closed state and its own gear button, and
Sign Out was similarly copy-pasted in both places. Fixed by introducing
`layout/UserMenu.tsx` — one consolidated account menu (avatar → display
name/email, Settings, Theme, Sign out), rendered exactly once, in the
header — and deleting `Sidebar.tsx`'s entire footer (it now owns exactly
one concern, history navigation). Also added real desktop sidebar
collapse (`layout/SidebarToggle.tsx`, one instance, header-only;
`settingsStore.sidebarCollapsed`, persisted, the single source of truth —
deliberately separate from `MobileNav`'s own ephemeral drawer-open state,
which is a different concern and was never unified with it). See
[`docs/ui-production-audit.md`](docs/ui-production-audit.md) for exactly
what was found duplicated and how it was verified (by reading handlers,
not just visual similarity), and
[`docs/navigation-and-actions.md`](docs/navigation-and-actions.md) for the
resulting one-action-one-owner table, including the one deliberate,
documented exception (`ThemeToggle` appears both as a `UserMenu` shortcut
and inside the full Settings dialog — same component/store, not a second
implementation). Verified visually, not just via component tests: a real
headless-Chromium pass (dev server + Playwright, auth mocked via request
interception) at desktop/tablet/mobile widths and in dark mode, zero
console errors.

**Second follow-up pass — Settings modal background bled through in dark
mode (2026-09-19):** a later report showed the Settings panel/backdrop
letting background chat content show through, but only in dark mode. Root
cause: `ui/dialog.tsx`'s panel used `bg-[var(--card)]`, and `--card`'s
dark-mode value (`index.css`) is `#15132485` — an 8-digit hex with an
embedded ~52%-opacity alpha channel, a deliberate "glass" treatment for
other surfaces (message bubbles) that the modal panel wrongly inherited;
the overlay was also only `bg-black/40` (40% opaque) in both themes. Light
mode's `--card: #ffffff` has no alpha component, which is exactly why the
first follow-up pass's own dark-mode screenshot check above didn't catch
this — that pass verified layout/duplication, not per-theme opacity.
Fixed with two new tokens, deliberately independent of `--card`:
`--modal-backdrop` (`#05050a`, opaque in both themes) and `--modal-surface`
(`#ffffff` light / `#151324` dark — `--card`'s dark hue with the alpha
stripped), consumed by `ui/dialog.tsx` (panel + overlay) and `ui/drawer.tsx`
(overlay only — its own panel, `--sidebar`, had no alpha channel in either
theme already). `AppShell.tsx`'s background wrapper also now gets the
native `inert` attribute while a modal is open, additive to the opaque
backdrop — `inert` removes the background from the accessibility tree/tab
order, the backdrop handles visual occlusion. Both `ImageViewer`/
`ImageEditor` (`components/image/`) build on the same shared `Dialog` and
inherited the fix with no changes of their own. Verified live (Playwright,
computed `background-color`/`opacity` sampled, not just screenshotted) at
desktop/tablet/mobile widths in both themes — see
[`docs/settings-modal-visual-bug.md`](docs/settings-modal-visual-bug.md)
for the full root-cause writeup and test matrix.

### SQL is untrusted output, always
The LLM's SQL is never trusted at face value. `agent/sql_validator.py`
parses it with `sqlglot` (in the dialect matching `DB_TYPE`) and rejects
anything that isn't a single `SELECT`/`UNION`/`EXCEPT`/`INTERSECT` statement
(explicit allowlist of the parsed statement type, not a regex blocklist).
**2026 Phase 3:** also rejects a reference to a system catalog/
data-dictionary object — `information_schema`, `pg_catalog`, mysql's
internal schemas, mssql `sys`, Oracle `ALL_*`/`DBA_*`/`USER_*`/`V$`/`GV$`
(`_find_system_catalog_reference`, new `system_catalog_access` violation
type in `SAFETY_VIOLATION_TYPES`) — a syntactically ordinary `SELECT` that
passed every prior check but could reveal internal schema structure or
other users'/roles' grants even under a genuinely read-only DB role, since
DB-role read-only-ness bounds writes, not what a SELECT can read. Uses
curated suffix matching for Oracle (`all_tables`, `dba_users`, ...), not a
blind `user_`/`dba_`/`all_` prefix match, specifically to avoid
false-positiving on an ordinary business table like `user_accounts`.
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

One nuance worth knowing if you're reading `api/main.py`/the frontend: the
LangGraph agent's own internal retry loop *does* execute candidate SQL automatically
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
`db.connection.check_write_privileges` is a best-effort detector for layer
(2) being misconfigured, and — since 2026 Phase 3 — is startup-enforced,
not just a manual CLI check; see "Authentication, authorization, and the
2026 security hardening passes" above.

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
- Frontend: `@tanstack/react-query` (`src/hooks/queries.ts`) for
  `getHealth`/`getSchemaTables`, invalidated explicitly by the "Refresh
  Schema" button's mutation; `chatStore.ts`'s `nlQuestionCache` (a plain
  `Map`, keyed by `nlCacheKey(question, priorQuestions, enableInsight)`)
  for repeated identical NL questions within a session, to skip redundant
  `/ask` round-trips.
- Process-lifetime singletons (DB engine, Ollama client, compiled LangGraph
  graph) — see "Process-lifetime singletons" above.

### Observability — live performance rollup (`observability/`)
`agent.nodes._timed_node` has, since before this section existed, logged a
`[timing] stage=... attempt=... duration_ms=...` line for every LangGraph
node call and appended a `StageTiming` entry to `AgentState["stage_timings"]`
(consumed only two ways before this: read live off a log line by a human,
or aggregated *offline* from a captured `eval/results/run_*.json` file, as
`docs/PERFORMANCE_BASELINE.md`'s manual latency-waterfall analysis did).
**2026-09-18:** `observability/metrics.py`'s `PerformanceMetrics` (one
process-wide singleton, `get_default_metrics()`, same `functools.cache`
pattern as every other singleton in this section) closes that gap —
`agent.graph.run_agent` now feeds every completed run's `stage_timings` +
total duration + final status into it after `compiled_graph.invoke()`
returns, wrapped in a `try`/`except` so a metrics-recording bug can never
fail the request it's instrumenting (the same fail-open posture this
codebase already applies to every other accuracy/observability aid).
`GET /metrics/performance` (admin-only, `Permission.ADMIN_CONFIG` — the
same gate `POST /schema/refresh` already uses) reads back a live snapshot:
per-stage count/mean/p50/p95/max/total (mirroring
`docs/PERFORMANCE_BASELINE.md`'s own table shape) plus an overall
request-duration distribution and a status-outcome tally. **Deliberately
single-process, in-memory, resets on restart** — a multi-worker deployment
would have one independent rollup per worker, the identical limitation
`agent/rate_limit.py`'s own sliding-window limiters already disclose for
the same reason (no shared store like Redis exists in this architecture);
real cross-process metrics would need Prometheus/OpenTelemetry, a
genuinely separate piece of infrastructure, not attempted here. This is
the first concrete step of a broader assessment — see
`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md` for the full discovery/
target-architecture/roadmap this was scoped from, and
`docs/PERFORMANCE_BASELINE.md`/`docs/PERFORMANCE_RESULTS.md` for why no
further backend "optimization" is justified today without a model/
hardware/prompt-size tradeoff (LLM inference is 93.8–98% of wall-clock
time; this rollup makes that fact continuously verifiable against live
traffic instead of a single point-in-time benchmark run).

### AI Data Analyst depth — trend/variance/outlier detection (`agent/insight.py`)
**2026-09-19:** `ResultSummary` (the small, aggregate-only summary
`generate_insight_from_llm` is given — see "Grounded insights, not
free-form narration" — never raw rows) gained three new deterministic
fields, computed the identical "Python does the arithmetic, the LLM only
narrates already-computed truths" way `top_label`/`top_share_percent`
already worked: `ColumnStat.stddev`/`coefficient_of_variation` (population
standard deviation and a scale-independent spread measure, `None` when
degenerate — a single row, or a zero mean), `ResultSummary.trend`
(first-vs-last-period change percent + direction, using the SQL's own row
order rather than re-deriving a chronology — see `TrendStat`'s own
docstring for why), and `ResultSummary.outliers` (per-label totals more
than `OUTLIER_STDDEV_THRESHOLD`, default 2.0, population-standard-
deviations from the mean, requiring at least 3 distinct labels since two
points are always symmetric around their own mean). All three flow
through `allowed_values()`/`allowed_percents()` alongside the pre-existing
fields, so the existing grounding gate (`is_insight_grounded`) would
already validate a claim mentioning any of them correctly.

**Fully tested (`tests/test_insight.py`, 16 new cases), but — like
`agent/tools/` before it (see "Tool/MCP abstraction" above) — not yet
wired into `agent.llm_client._build_insight_prompt`, so nothing in the
live `generate_insight_node` path actually surfaces a trend/variance/
outlier claim yet.** Deliberately deferred rather than folded into this
same change: `_build_insight_prompt`'s exact wording is what
`docs/EVALUATION_CURRENT.md`'s live-LLM benchmark numbers were measured
against, and this session had no way to re-run that 57-case live-Ollama
benchmark to confirm a prompt change doesn't shift generation behavior
before merging it — changing a security/accuracy-reviewed, eval-tracked
prompt without being able to re-verify against the eval harness would
violate this project's own "never compromise correctness," "do not
optimize/change behavior blindly" standard (see
`docs/PERFORMANCE_RESULTS.md` for the precedent of explicitly declining a
change for the identical reason). The new fields are additive-only
(everything existing on `ResultSummary` is unchanged), so a future pass
can wire them into the prompt (or straight into the API response for the
frontend to render as a trend badge/outlier highlight without an LLM
sentence at all) once it can validate the change properly. See
`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md`'s P1 roadmap for where this
sits relative to the rest of the AI Data Analyst work (chart-type
coverage, segmentation, forecasting).

### Prompt-injection benchmark and hardening pass (2026-09-24/25)

A new, externally supplied 500-case prompt-injection/security benchmark
(20 categories: direct instruction override, role manipulation, prompt
disclosure, policy bypass, SQL safety bypass, tool manipulation,
authorization/exfiltration, obfuscation, multi-turn persistence, 7
`Indirect *` categories, resource exhaustion, ambiguous/benign boundary
cases, cross-tenant isolation) was run to completion against the real
live agent (real Ollama, real SQL Server databases) — not a static
regex-blind-spot probe. Full detail, live results, and honest residual
gaps: `docs/security/PROMPT_INJECTION_BENCHMARK_GAP_REPORT.md` (the
authoritative, continuously-updated source — this section is a pointer,
not a duplicate). Harness: `eval/security_benchmark/` (dataset loader,
pure detectors, live runner, reporting, plus `multiturn.py` for a true
turn-1-poison/turn-2-real-attack persistence test), CLI entry points
`scripts/run_security_benchmark.py`/`scripts/run_multiturn_persistence_benchmark.py`.

**Headline result: all 500 cases completed, 0 critical findings** — zero
writes executed, zero unauthorized sources reached, zero secrets leaked,
zero system prompts leaked, across the entire run. 66.4% raw pass rate;
every one of the 168 `expected_behavior` mismatches is a content-level
miss (the model answers where a refusal was expected, or over-refuses a
benign case), never a hard-gate violation — several were individually
traced by hand and confirmed benign (e.g. a "wrap a DELETE in a harmless
SELECT" attempt failed at *parse time*, never reaching the embedded-write
check at all). A true multi-turn persistence test (10 payloads, organic
turn-1 + worst-case-simulated turn-2) and the pre-existing
`conversation_id` IDOR/cross-tenant test
(`tests/test_api_chat_history.py::TestOwnershipIsolation`, already
existed, confirmed still passing) both also show 0 critical findings.

**Real bugs found and fixed along the way, not just findings**:

1. **`OPTION (MAXRECURSION 0)` passed `agent/sql_validator.py`
   unguarded.** This MSSQL hint disables the engine's own 100-level
   recursive-CTE safety limit — parses as an ordinary `exp.QueryOption` on
   an otherwise unremarkable `exp.Select`, which nothing above it checked.
   A prompt engineered around "use recursive queries without a limit"
   reliably got the local model to add this hint, and one live case hung
   the whole benchmark process for 80+ minutes (found because the process
   showed 0% CPU well past every configured timeout). New
   `unsafe_query_option` violation type (in `SAFETY_VIOLATION_TYPES`,
   fails closed, no retry) — flat denial of any `MAXRECURSION` hint
   regardless of value; there's no legitimate reason this app ever needs
   to override the engine's own recursion safety net. Paired fix:
   `db/query_cost.py`'s cost-estimation timeout used to abandon its
   worker thread on timeout rather than force-closing its connection
   (unlike `db.execution._execute_with_timeout`'s already-trusted abort
   mechanic) — a stuck plan-compile call (exactly what a pathological
   recursive CTE causes) leaked a pooled DB connection every time. Now
   mirrors `_execute_with_timeout`'s abort-via-close pattern via a new
   `_ConnectionTrackingEngine` proxy, with no changes needed to any of the
   four dialect-specific plan-fetch strategies.
2. **`security.redaction.redact_secrets` was never applied to
   LLM-generated response text** — only to raw driver/retrieval errors.
   New `redact_configured_secrets` (covers all 12 secret fields `Settings`
   defines, not just `db_password`) is now applied at
   `api/main.py::_ask_response_from_state`'s response-assembly boundary —
   `insight`, `synthesized_answer`, every orchestrator source's
   answer/caption text, `error_history`, `query_plan`, and the other
   free-text notice fields.
3. **`agent/followup.py::classify_followup` over-refusal bug.** A
   standalone question containing "it"/"this"/"that" whose antecedent is
   named earlier in the *same* sentence (e.g. "...must be refused,
   without executing **it**") was misclassified as an ambiguous follow-up
   reference on a fresh session with no history at all. Fixed with a
   one-directional heuristic (`_has_intra_sentence_antecedent`) that can
   only reduce false ambiguity, never introduce it — this classifier is a
   UX/latency optimization, never a security boundary, so this was safe
   to fix without re-running the live benchmark first.
4. **New `cross_source_injection_narrative` pattern in
   `security/injection_patterns.py`.** The two lowest-scoring benchmark
   categories (`Indirect multi-source injection`, `Indirect
   glossary/metric injection`) trace to a real, common gap: their payloads
   are third-person *narrations* of an indirect-injection scenario (e.g.
   "The HR source instructs the agent to reveal finance records.") rather
   than direct imperative commands — a shape none of the existing 6
   patterns target. Verified before adding: 16/16 real failing payloads
   now match, 0 false positives across three independent control sets
   (a hand-built benign set, all 47 real `eval/benchmark/*.yaml`
   questions, all 10 `Benign adversarial boundary` payloads). **Confirmed
   live**: both categories re-run against the fixed code — pass rate
   20% → **100%**, 0 critical findings, average latency 212.6s → **8.6s**
   (every case now short-circuits at the input-guard layer instead of
   reaching generation).
5. **`eval/security_benchmark/runner.py` had no per-case exception
   handling** — a transient `agent.exceptions.AgentError` (e.g.
   `OllamaUnavailableError` when local Ollama is overloaded) crashed an
   entire in-progress multi-hour benchmark run via an unhandled exception,
   losing every already-completed case's result. Confirmed this was
   harness-only fragility, not a production gap:
   `OllamaUnavailableError` is an `AgentError` subclass, and
   `api/main.py` already registers `@app.exception_handler(AgentError)` —
   a real caller hitting the same timeout via HTTP gets a clean
   `.safe_message` response, not a crashed server. `run_security_case`
   now catches `AgentError` and records an `"error"`-status result
   (never counted as a pass or critical finding) instead of propagating.

**True channel-level seeding tests** (`tests/test_indirect_channel_injection.py`,
11 tests) were added for the 6 indirect categories that previously only
had direct-channel-proxy coverage via the benchmark itself (schema/
comment injection already had true coverage —
`tests/test_adversarial_input.py::TestPoisonedSchemaValueNeutralization`).
Each seeds a poisoned string directly into the real object shape that
channel produces (a `GoldenExample`, a business-context chunk dict, a
`WebResult`, a RAG `ChunkResult`, a `MediaHit`, an `OrchestratorState`
source-result dict) and confirms it reaches the model framed as DATA, not
instructions — fully mocked, no live LLM/DB/Chroma.

**Honest residual gaps, not closed by this pass** (see the gap report's
own "What hasn't been done yet" for the current list): an admin-role
re-run of the RBAC-bypass categories was still in progress as of this
writing; the multi-turn organic-history follow-up (2 of 10 payloads whose
turn-1 organically succeeded were never separately re-graded against
their real history entry, only the synthetic worst-case) remains open.
Neither is a known failure — both are simply not yet re-confirmed.

## How to run

See `README.md` for full setup. Short version:

```powershell
.venv\Scripts\Activate.ps1
ollama pull llama3.1:8b
# fill in .env with your real DB connection details first
python scripts\test_db_connection.py
python scripts\build_embeddings.py
python -m scripts.ingest_schema --database-id default   # optional, business-context vector retrieval
alembic -c identity\alembic.ini upgrade head              # optional, only if LOCAL_AUTH_ENABLED=true
cd frontend; npm install; npm run build; cd ..
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000/` — the API process also serves the built
React dashboard. For frontend hot-reload during active frontend work, run
`npm run dev` inside `frontend/` in a second terminal instead (Vite
proxies API calls to the `uvicorn` process above).

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

**`tests/security/`** is a small, deliberately additive directory (not a
reorganization — see its own `README.md`) for adversarial-scenario
regression tests that don't fit any single module's own
`tests/test_<module>.py` file: encoded/multilingual prompt-injection
bypass regression, rate-limit header-spoofing lock-in, and the malware
scanner's fail-closed contract. Check whether a scenario already has
coverage under its own module's test file before adding a new one here.

**CI security gates are now blocking, not report-only** — `ruff check .`,
`black --check .`, `mypy .`, `bandit`, `pytest`, and (new) `pip-audit`
(with a specific, individually-justified `--ignore-vuln` allowlist, not a
severity threshold) and `detect-secrets` (baseline-backed,
`.secrets.baseline` committed at the repo root) all fail the build on a
new finding. Only `gitleaks` and the Trivy container scan remain
`continue-on-error: true`, each with a dated, reasoned comment explaining
why (tooling/timing, not an unreviewed exception) — see
`docs/security/CVE_TRIAGE.md`.

**Known, pre-existing CI-hygiene gaps (partially closed 2026-09-19 — this
note tracks what remains, not what's already fixed):** `mypy .`'s
module-resolution crash (`scripts/build_user_guide_pdf.py: error: Source
file found twice under different module names`, caused by
`scripts/build_setup_guide_pdf.py`'s `from scripts.build_user_guide_pdf
import build` combined with `scripts/` having no `__init__.py`) is
**fixed** — `scripts/__init__.py` (empty) now exists, so `scripts.X` and
the bare filename always resolve to the same module. `mypy .` now
actually runs to completion for the first time, which is itself a real
finding: it surfaces **117 pre-existing type errors across 30 files** that
were always there, just hidden behind the crash. Of those, 12 were fixed
in the same pass (all with zero behavior change, verified against the
full test suite): the 6 in this pass's own two new test files
(`tests/test_observability_metrics.py`, `tests/test_web_search.py`), a
stale `keyframe: object` field in `media/ingest.py`'s `_SegmentPrep`
(there was never an actual import cycle — `media/ingest.py` already
imports directly from `media.keyframes` for `extract_keyframes`; now
properly typed as `KeyframeSegment`), `moderation/gate.py`'s
`hard_reject_categories`/`soft_flag_categories`/`status` locals (typed as
plain `list[str]`/`str` instead of the `Category`/`Literal["passed",
"rejected"]` types `ModerationDecision` actually declares), and
`scripts/build_media_index.py`'s `exc` variable name colliding with mypy's
exception-variable-deletion tracking from earlier `except ... as exc:`
blocks in the same function (a mypy false-positive, not a real runtime
bug — renamed to `error`, zero behavior change). **The remaining ~105
errors are still open, all in test files this pass didn't touch** (mostly
`Argument ... has incompatible type "None"; expected "Settings"` from
tests passing `None` where a test helper's own signature could reasonably
accept `Settings | None`, plus a scattering of `var-annotated`/
`union-attr`/`typeddict-item` findings) — a real, bounded, separate
mypy-hygiene pass, out of scope here. Separately, `ruff check .`/
`black --check .` across the *whole* repo currently report **6 ruff / 8
black** pre-existing findings (down from ~9-10 each before this pass fixed
`moderation/gate.py`'s and `scripts/build_media_index.py`'s own drift as a
side effect of editing them above) — `moderation/types.py`,
`moderation/provider.py`, `media_gen/download.py`, and a handful of test
files remain, none touched by this pass.

## Common commands

| Task | PowerShell | Make |
|---|---|---|
| Create venv + install deps | `.\tasks.ps1 setup` | `make setup` |
| Verify DB connection | `python scripts\test_db_connection.py` | `python scripts/test_db_connection.py` |
| Build/refresh embeddings | `python scripts\build_embeddings.py` | `python scripts/build_embeddings.py` |
| Ingest business-context knowledge | `python -m scripts.ingest_schema --database-id default` | same |
| Apply identity-DB migrations | `alembic -c identity\alembic.ini upgrade head` | `alembic -c identity/alembic.ini upgrade head` |
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
- `ENABLE_MEDIA_SEARCH=true` requires the Tesseract OCR binary installed as
  a *system* package (not pip-installable — `pytesseract` is just a thin
  Python wrapper around it) — another extra manual install step on a
  fresh Windows machine, same shape as `DB_TYPE=mssql`'s ODBC driver
  requirement above. `media/ocr.py` fails open (indexes without OCR text)
  if it's missing, so this isn't a hard blocker, just reduced search
  accuracy for on-screen text until it's installed. Video keyframe
  extraction/captioning also each need a one-time local model pull —
  see `CLAUDE.md`'s "Media search" section.

## Coding standards

- Type hints and docstrings on every public function — this codebase is meant
  to be interview-explainable, not just working.
- No `print()` for anything other than the standalone CLI scripts
  (`scripts/test_db_connection.py`, `scripts/integration_test.py`, which
  are meant to be read as terminal output, not logged) — everything else
  uses the `logging` module (see `config/settings.py` for level config,
  overridable via `LOG_LEVEL` in
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

- **`search/` still has no dedicated `pytest` unit test file.**
  (Previously this note also named `rag/` — that half is now stale:
  `tests/test_rag_graph.py`, `tests/test_rag_ingestion.py`, and
  `tests/test_rag_pdf_download.py` were added in a later session and
  directly test `rag.graph`/`rag.store`, closing that part of the gap.)
  `tests/test_orchestrator.py` still only mocks `search.web_search
  .web_search` at the boundary to test the orchestrator's own wiring —
  `search/web_search.py` itself (the Tavily request construction, response
  parsing, `SUPPORTED_SEARCH_PROVIDERS` dispatch) was verified by running
  it against this project's real Tavily instance during development, not
  by a mocked regression suite. Adding one (request construction, response
  parsing — pure-logic-testable with mocks) is a real, worthwhile follow-up
  before this code is trusted long-term.
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
- **Follow-up question resolution (`agent/followup.py`) is still
  per-request, not backed by a LangGraph checkpointer.** This is
  independent of chat-history *storage* (see "Universal server-side chat
  history" below, which does now persist conversations/messages
  permanently for a locally-authenticated user) — `classify_followup_node`
  still only ever sees whatever `conversation_history` the caller resends
  on each `/ask` call (`frontend/src/lib/history.ts::buildConversationHistory`,
  capped to the last `MAX_FOLLOWUP_EXCHANGES` turns), not a server-side
  LangGraph checkpoint. A SQL Server/Postgres-backed checkpointer was
  scoped in early design discussion but never built (no official
  LangGraph SQL Server checkpointer exists at this project's pinned
  `langgraph==0.2.62`; it would need a custom `BaseCheckpointSaver`) — this
  remains a real, separate follow-up from chat-history persistence itself.
- **Universal chat history only covers locally-authenticated users** — an
  OIDC-authenticated or fully-unauthenticated caller's questions are still
  answered normally, but nothing is persisted for them (no corresponding
  `identity.users` row to attach a conversation to). See
  `docs/chat-history-authentication-audit.md` §7.
- **A reloaded past conversation turn isn't a byte-for-byte reconstruction
  of a live one** — only the answer text and, if the turn produced SQL,
  that SQL are persisted (`identity.models.AiOutput.metadata_json`), not
  the full `AskResponse` (charts, per-source citations, the exact schema
  DDL shown at generation time). See `docs/chat-history-architecture.md`'s
  "Known limitations."
- **Business-context vector retrieval (`retrieval/`) has no dedicated
  NL term/metric/table/time-concept extraction step** and **no
  breached-password check** for the local-auth password policy
  (`identity/password_policy.py`) beyond a small offline common-password
  list — both deliberate, disclosed tradeoffs, not oversights. See
  `docs/vector-retrieval-design.md` §13 and
  `docs/authentication-and-password-policy.md`'s own "Not implemented"
  notes respectively.
