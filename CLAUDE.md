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
| LLM runtime | Ollama, default model `llama3.1:8b` (swap via `.env` / `config/settings.py`, e.g. `sqlcoder`, `duckdb-nsql`). Per-question model selection is also supported (`AskRequest.model`, validated against `Settings.ollama_allowed_models`) — see "Configurable Ollama model selection for Text-to-SQL" below |
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
  `AgentState["selected_model"]` is the identical pattern applied to Ollama
  model selection (`model_registry.py` — configured/allowed vs. installed
  vs. selectable, see "Configurable Ollama model selection" below): resolved
  once by `run_agent()`, read (never re-selected) by every LLM call
  `nodes.py` makes for that question.
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
- `agent/provenance.py`, `analytics/`, `recommendation/`, `semantic/` —
  typed contracts for the Enterprise AI Analytics & Recommendation
  Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`):
  `agent/provenance.py`'s `DataTruthLevel`/`ProvenancedClaim`
  (DATABASE_FACT/AI_INFERENCE/CONFIRMED_BUSINESS_TRUTH); `analytics/`'s
  `AnalyticsProvider` + a real adapter over `agent.insight.ResultSummary`'s
  already-computed, never-rendered `trend`/`outliers`/`stddev`;
  `recommendation/`'s `RecommendationProvider` (contracts only, no
  implementation exists); `semantic/metrics.py`'s governed
  `MetricDefinition`/`MetricRegistry`, loading the same
  `data/knowledge/metrics.yaml` `retrieval/` already parses for fuzzy
  retrieval, deliberately not a reuse of `retrieval.models.ChunkType
  .METRIC`. Same "additive, not wired into any live route yet" posture as
  `agent/tools/` above — see `02_TARGET_ARCHITECTURE.md` for the full
  design and its explicit "stubbed today, wired later" table.
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
- `attachments/` — chat file attachments (images, PDF, DOCX, XLSX, PPTX,
  TXT, MD, CSV, JSON) given directly to the model as context for the
  question that attached them — see "Chat attachments" below for the full
  design. `models.py` (`Attachment`/`ProcessedAttachment`/`AttachmentError`),
  `validation.py`, `storage.py` (sanitized on-disk storage, id-only paths),
  `store.py` (the bounded, in-memory, owner-scoped `AttachmentStore`),
  `image_processing.py`/`vision.py` (normalize → data URL; local Ollama
  vision description with an OCR fallback), `processors/` (one
  `FileProcessor` per file kind + a registry), `context_builder.py`
  (delimited, truncation-safe prompt context), `pipeline.py` (the
  validate → scan → store → process orchestration `api/attachments.py`
  and `graph.py` both call, plus `register_derived_image` for a resize/
  remove-text output), `state.py`/`graph.py` (the attachment-QA LangGraph
  subgraph, wired into `agent/orchestrator/` as a new `"attachments"`
  source). `ocr_extract.py`/`image_ops.py`/`inpaint.py`/`capabilities.py`
  are the explicit image-action modules (OCR-with-regions, deterministic
  resize, classical-inpainting text removal, and the live capability
  registry, respectively) — see "Chat attachments, image actions, and
  their security hardening" below for the full design.
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
The full graph (`agent/graph.py`) is twelve nodes:
`sanitize_input → classify_followup → retrieve_schema →
retrieve_golden_examples → retrieve_business_context → plan_query →
generate_sql → review_sql → validate_sql → estimate_cost → execute_sql →
generate_insight`. On a review, validation, cost-estimate, or execution
failure, a conditional edge routes back to `generate_sql` (or, for a
"missing reference" execution error, back to `retrieve_schema`) with the
error message appended to state history, so the LLM sees what went wrong
and can correct itself. Capped at `MAX_RETRIES = 3` (`config/settings.py`)
as a base, widened per-question by `agent/complexity.py::compute_max_retries`
(up to `COMPLEX_QUERY_MAX_RETRY_BONUS` extra attempts for a "harder than
usual" question, stored in `state["max_retries"]` — every retry-vs-give-up
check reads that, never the raw setting). After the budget is exhausted,
the graph ends in a terminal `failed` state rather than looping forever.
It's a small, explicit state machine, not a ReAct-style free-form agent,
specifically so the retry/error-feedback path is inspectable and boundable.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#2-retry--self-correction-semantics)**
for the full per-node walkthrough and the complete failure-category →
retry-or-fail-closed routing table (which failures never retry — e.g.
safety violations, off-topic input, timeouts — and why).

### Agentic query planning + plan-conformance self-correction
`plan_query_node` (between `retrieve_schema` and `generate_sql`) and
`review_sql_node` (between `generate_sql` and `validate_sql`) are a
decompose-then-check pair, gated by the exact same complexity signals that
widen the retry budget above (`state["complexity_signals"]`) and by
`ENABLE_QUERY_PLANNING` (default `true`). For an ordinary question that
matches no signal, both nodes are a pure pass-through — zero LLM calls,
zero added latency. For a question that does match, `plan_query_node`
makes one LLM call producing a short ordered plan; `review_sql_node`
makes a second LLM call checking the generated SQL against that plan,
feeding a `FAIL` back into another `generate_sql` attempt from the same
retry budget above — never a second, unbounded loop. Both nodes fail open
on an unreachable Ollama server or an unparseable response — this feature
is an accuracy aid, never a reason a question can't be answered.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#agentic-query-planning-and-plan-conformance-review)**
for the full reasoning and the four regex signal patterns that trigger it.

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
`POST /feedback/golden-example`, shown once a "Confirm and Run" result is
genuinely confirmed successful) — never automatically. The SQL saved is
the *exact* SQL actually executed, not the agent's original draft — the
user may have edited the SQL box before confirming, and the corrected
version is exactly what's worth remembering. Saved with a deterministic id
(`embeddings.golden_examples._example_id`, a hash of database+question+SQL)
so re-clicking the widget upserts the same document rather than
accumulating duplicates. Every caller (the React dashboard, the REST API
used standalone, `eval/runner.py`) benefits from retrieval automatically,
since they all call the same underlying `agent.graph.run_agent` graph.

**`POST /feedback/message`** (`feedback/store.py`, a separate ChromaDB
collection) is the general-purpose counterpart: a like/dislike (plus an
optional comment) on *any* assistant answer — SQL, document/policy RAG,
web, or media, not only a confirmed-and-executed SQL result
(`frontend/src/components/chat/ResponseFeedbackWidget.tsx`). Purely
additive — a thumbs-up on a confirmed SQL answer still separately calls
`POST /feedback/golden-example` too — and never feeds few-shot retrieval
itself; it's a read-later feedback log, one shared collection (not
per-database), fails open on any storage error exactly like the golden
store above.

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
media generation) by name — a uniform contract (permission check, timeout,
retry policy, structured audit logging) wrapping the exact same functions
`agent/orchestrator/nodes.py` already calls, no reimplementation.
**Deliberately additive, not a rewrite: nothing in the live request path
calls through this registry yet.** `agent/orchestrator/nodes.py`'s
hardcoded nodes are completely unmodified, and `run_orchestrated` still
calls them directly in production — this is foundation for a future
tool-dispatching planner and a future external MCP client, not scaffolding
that does nothing (it's fully tested on its own).

**Read [`docs/TOOLS.md`](docs/TOOLS.md)** for the full contract and design,
and [`docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md`](docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md)
for the roadmap this is the first step of.

### Multi-source orchestration (router + subgraphs)
`ENABLE_MULTI_SOURCE_ROUTER` (default `false`) puts a router
(`agent/orchestrator/`) in front of the SQL pipeline. Router + subgraphs,
not a single tool-calling agent, was a deliberate choice: every source's
safety boundary (the SQL validator, the policy-sensitivity gate, the
"web content is untrusted" framing) stays separately testable rather than
folded into one model's implicit tool-selection reasoning.

**Flag off (the default):** `agent.orchestrator.graph.run_orchestrated`
calls `agent.graph.run_agent` directly and returns its result completely
unwrapped — the orchestrator graph is never even constructed. This is what
makes the byte-for-byte-unchanged guarantee for SQL-only questions
provable, not just claimed. **Flag on:** `router_node` picks one or more of
`sql`/`documents`/`policy`/`web`/`generation`/`media_search` (zero LLM
calls with ≤1 source available; one LLM call to classify with 2+, falling
back to *every* available source, never zero, on an unparseable response)
and fans them out as parallel branches in one LangGraph step before
`synthesis_node` composes the final answer — a pure pass-through when only
one source fired, a labeled per-source section when 2+.

**Known limitation, not yet fixed:** every routed subgraph receives the
same full, un-decomposed question text — a question that's really two
unrelated asks mashed into one sentence can retrieve poorly on both sides
even though each alone would work. Per-source query decomposition would
fix this; out of scope for the initial build.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#4-multi-source-orchestration)**
for the full diagram, router/fan-out/synthesis mechanics, and
`OrchestratorState`'s shape (it *extends* `AgentState`, never replaces
it), and [`docs/MULTI_SOURCE_GUIDE.md`](docs/MULTI_SOURCE_GUIDE.md) for
how to configure and use each source.

### Document/policy agentic RAG (`rag/`)
One implementation (`rag.graph.build_rag_subgraph(collection)`) serves
both "documents" and "policies" — structurally identical, differing only
in `generate_node`'s sensitivity check. A policy chunk tagged
`compensation`/`disciplinary`/`legal` at upload time is **never**
summarized into an answer — checked before the LLM ever sees the chunk,
fail-closed, the same philosophy as `agent/sql_validator.py`'s
`SAFETY_VIOLATION_TYPES`. **2026 Phase 3** added a second, independent
gate, `DocumentRecord.restricted_roles` (real RBAC — see "Authentication,
authorization, and the 2026 security hardening passes" below); a document
can carry either gate, both, or neither. The chat-answer PDF download
button is built strictly from the `citations` list, which is why a
restricted match (`citations == []`) automatically has no download
button — there's no separate access check to remember.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#documentpolicy-agentic-rag-rag)**
for the full retrieve → grade → rewrite → generate flow, the SQL Server
`VECTOR` storage design (including a real `ntext`/vector-cast bug found
and fixed), and the untrusted-content framing.

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
A tested IMA Studio client wired into the orchestrator as a `"generation"`
source. Image generation is confirmed working end-to-end against a real
IMA account; video generation shares the same code path but hasn't been
separately confirmed live. `ENABLE_MEDIA_GENERATION` stays off by default
so a fresh clone never spends real IMA credits unintentionally.

**Invariants that must not regress:** generated media is served through
this app, never the provider's raw CDN URL — `execute_generation`
downloads the bytes once (SSRF-hardened, redirects re-validated hop-by-hop),
caches them under an opaque `media_id`, and neither the answer text nor
the API response (`MediaGenerationResultOut`) ever carries the raw URL.
Generation is gated by a human-in-the-loop approval step before any
provider call (`Settings.require_generation_approval`, default `true`) —
the only orchestrator source that spends real, metered money; both the
confirm endpoint and the approval-disabled path funnel through the same
`execute_generation` function, which re-runs its own safety/rate-limit
checks regardless of entry point. Video clip length is capped by the
provider's own model (currently 4-15s), not a restriction this app
imposes.

**Read [`SECURITY.md`](SECURITY.md)'s "Media generation" section** for the
full security rationale (SSRF hardening, content-policy check, rate
limits, cost ceiling) and [`docs/RESPONSIBLE_AI.md`](docs/RESPONSIBLE_AI.md)
for the content-policy check's disclosed limitations (a keyword heuristic,
not real moderation). **Known gap:** multi-source synthesis doesn't embed
generated media inline — a generation result folded into a multi-source
answer shows only its (link-free) confirmation text.

### Media search (image/video, optional, off by default) (`media/`)
Content-based search over an **untagged** local image/video library (no
filenames, no manual tags), routed as the `"media_search"` orchestrator
source. Gated by `ENABLE_MEDIA_SEARCH` + a real `MEDIA_LIBRARY_PATH`. Off
by default — it pulls in a real, meaningfully larger dependency footprint
(`torch`, `opencv-python`) that shouldn't land on every fresh clone
uninvited.

**Invariants that must not regress:** embeddings are local CLIP
(`sentence-transformers`' `clip-ViT-B-32`), not a hosted API — the one
real exception to this project's otherwise-consistent "no torch"
dependency posture, a deliberate, disclosed tradeoff (not an oversight).
OCR/ASR/captioning text is framed as **untrusted data, never
instructions** in the answer-composition prompt, same as web-search/RAG
content. Video keyframe/thumbnail serving (`GET /media/library/{media_id}`)
is a separate, persistent path from generated-media's own bounded
in-memory cache — a full video clip is never streamed, only a frame +
timestamp.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#11-media-search)**
for the full video pipeline (scene-change keyframing, ASR, OCR,
captioning, dual embeddings) and known gaps, and
[`SECURITY.md`](SECURITY.md)'s "Media search" section for the
untrusted-content framing detail. Needs the system Tesseract OCR binary
installed separately — see "Windows / Visual Studio-specific notes" below.

### Content moderation gate (mandatory, not a feature flag) (`moderation/`)

Both content-ingestion pipelines — media search's images/video and
document/policy RAG's PDFs — run every chunk of every file through
`moderation/gate.py::moderate_chunks` before either one's store ever
persists anything. **Mandatory whenever `ENABLE_MEDIA_SEARCH` or
`ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG` is on** — deliberately no
`ENABLE_CONTENT_MODERATION` toggle exists that could silently disable it
while leaving either pipeline on; missing provider/store config fails
ingestion closed.

**Invariants that must not regress:** a hard-reject on any chunk rejects
the **entire** asset — never a partial ingestion — and nothing from a
rejected file is ever embedded or stored anywhere (a rejected PDF never
gets a `rag.documents` row at all, including its raw bytes). Azure AI
Content Safety (the only provider) has no dedicated "weapons"/"drugs"/
"synthetic media" category — stated honestly via a text-blocklist proxy
for the first two and a disclosed `"not_checked"` placeholder (never a
hard-reject) for deepfake detection, not silently glossed over. This is
the one required exception to this project's "fully local" model
posture — accurate content moderation has no on-device option today.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#12-content-moderation-gate)**
for the full per-type chunking strategy (image/video/PDF), the dedupe
store, and the concurrency design, and
[`docs/RISK_REGISTER.md`](docs/RISK_REGISTER.md)'s R-013/R-014 for the
disclosed tradeoffs above.

### Malware scanning (`security/malware_scanner.py`)
Scans a file's **raw bytes**, before any parser touches them — a separate
concern from the content-moderation gate above, which only runs after
extraction. Wired into both `rag/ingestion.py::ingest_pdf` and
`media/ingest.py::ingest_file`, right after the dedupe-by-hash check.
Pluggable (`Settings.malware_scan_provider`, `"disabled"`/`"clamav"`),
spoken to directly over `clamd`'s `INSTREAM` protocol.

**Invariant: deliberately off by default, but genuinely fail-closed once
enabled** — an infected result, an unreachable/timed-out daemon, and a
malformed response are all treated as a rejection; "unknown" is never
silently treated as "clean." `"disabled"` is still audit-logged per
upload, not silently skipped.

**Known limitation:** fail-closed logic is unit-tested against a mocked
`clamd` socket only — never exercised against a real daemon in this
project's history. See
[`docs/security/FINAL_PRODUCTION_GATE.md`](docs/security/FINAL_PRODUCTION_GATE.md)'s
Malware Scanning row and [`docs/RISK_REGISTER.md`](docs/RISK_REGISTER.md)'s
R-016.

### Voice mode (speech input/output) (`voice/`)
Optional, **on by default** (`ENABLE_VOICE_MODE=true`) — unlike media
generation, this feature spends no money and makes no external network
call once its one-time local model downloads are done. Transcription via
`faster-whisper`, synthesis via Piper — both fully local inference (no
`torch`).

**Invariants that must not regress:** a transcribed question is **never**
treated specially — `POST /voice/transcribe`'s result is submitted
through the exact same `POST /ask` path a typed question uses, so
`agent.input_guard.check_input` applies unconditionally, with no separate
code path for voice input to bypass. Nothing is auto-submitted from a
voice turn — the existing Send button is the only confirmation step — and
`originatedFromVoice` (set only when the textarea still holds a voice
transcript at send time, never by typing) is a structural guarantee, not
a runtime check, that a typed question can never trigger
`POST /voice/synthesize`.

**Disclosed, deliberate exception to "fully local":** live word-by-word
captions while listening use the browser's built-in `SpeechRecognition`
Web Speech API, which (in Chromium) sends microphone audio to the browser
vendor's own cloud speech service. The caption is **never** what gets
submitted — the local Whisper result remains the sole authoritative
transcript, discarded and replaced the moment it returns.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#13-voice-mode)** for
the full UI state machine, autoplay warm-up mechanics, schema-aware
transcription vocabulary hinting, and upload size/duration security
limits.

### Authentication, authorization, and the 2026 security hardening passes
Two independent layers — the "no per-user authorization system" phrasing
that appears in a couple of older notes elsewhere in this file predates
both and is no longer accurate for anything gated by `agent/authz.py`'s
permissions.

**Invariants that must not regress:** `Settings.auth_mode` is the one
dispatch point (`none`/`static_token`/`oidc`/local self-hosted accounts,
plus Google sign-in layered on local accounts) — `ENVIRONMENT=production`
fails closed at startup if identity is unconfigured. RBAC
(`agent/authz.py`) is wired into every data-touching/expensive route
**and** the multi-source router itself — `router_node` filters the LLM's
own source selection through a permission check *before* any subgraph
runs, so a routing decision the LLM makes can request access but never
unilaterally grant it; an unrecognized role grants zero permissions (fail
closed). Local accounts and Google sign-in both feed the *same*
`AuthIdentity.roles`/RBAC bridge — zero changes needed to any existing
route's authorization check.

**Read [`docs/AUTHENTICATION.md`](docs/AUTHENTICATION.md)** for the four
auth modes, frontend OIDC login, and Google sign-in (including what's
live-verified vs. not), and
[`docs/AUTHORIZATION.md`](docs/AUTHORIZATION.md) for the full
resource-to-permission mapping. For the broader security posture
(security headers/CSP, DB write-privilege startup enforcement, CI gates,
CVE triage) see [`SECURITY.md`](SECURITY.md) and
[`docs/security-changelog.md`](docs/security-changelog.md); for the
current, most-rigorously-sourced production-readiness verdict (as of this
writing: **NOT READY**, over DAST/live-IdP gaps, not a found
vulnerability), start at
[`docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`](docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md)
— it supersedes the repo-root `SECURITY_*.md` files' own bottom line where
the two differ.

### Universal server-side chat history (`identity/repositories/history.py`, `api/chat_history.py`)
For a locally-authenticated user, every conversation and message is
stored permanently in the identity database — the fix for a real,
previously-disclosed gap (chat state used to be entirely in-memory-only,
so the same signed-in user got a blank history on a different
browser/device, and a page reload cleared it).

**Invariants that must not regress:** every route requires
`Depends(require_local_user)` and folds `user_id` directly into every
repository-layer query, so a wrong/forged `conversation_id` resolves to a
404, never confirming another user's conversation even exists. A
persistence failure here can never fail the `/ask` response itself
(fail-open, same posture as every other non-critical-path accuracy aid).
Retention is soft-delete only (`deleted_at`) — logout, session expiry, or
an app restart never delete chat history; only an explicit
`DELETE /conversations/{id}` does. **Opening a saved conversation never
re-executes SQL, re-fetches a web page, or re-runs OCR/vision** — only an
explicit "Confirm and Run" click does. **Known limitation:** only a
locally-authenticated user gets this — an OIDC or unauthenticated
caller's questions are answered normally but nothing is persisted.

**Read [`docs/chat-history-architecture.md`](docs/chat-history-architecture.md)**
for the schema/API/frontend design (§6.5 covers the full-turn-metadata
fix and what still isn't restored on reload — retrieved-schema DDL, a
chart's own customization),
[`docs/chat-history-authentication-audit.md`](docs/chat-history-authentication-audit.md)
for the before/after, and
[`docs/chat-history-search.md`](docs/chat-history-search.md) for the
search design.

### Frontend UI redesign (2026 UI pass)
A ground-up-in-appearance, additive-in-substance redesign of `frontend/`
into a left-sidebar/main-workspace layout. **Invariant that must not
regress:** no backend logic, API contract, auth flow, SQL validation, or
chat-persistence behavior changed as part of this pass or its two
follow-up bug-fix passes (duplicated Settings/Sign-out controls; a
dark-mode-only modal-transparency bug from an inherited "glass" CSS
token) — every change is presentation/interaction layer only.

**Read, per topic:**
- [`docs/frontend-ui-audit.md`](docs/frontend-ui-audit.md) — the Phase 1 audit this pass started from.
- [`docs/ui-design-system.md`](docs/ui-design-system.md) — the CSS token set and shared `components/ui/` primitives added.
- [`docs/chat-history-ui.md`](docs/chat-history-ui.md) — the sidebar/settings split (`Sidebar.tsx`, `MobileNav.tsx`, `SettingsDialog.tsx`) and the extracted, independently-tested history-search components.
- [`docs/image-editing-architecture.md`](docs/image-editing-architecture.md) — the local image editor (Konva/react-konva). At the time of this UI pass, AI-guided editing was a deliberate, permanent stub; it was later implemented for real — see "AI-guided (generative) image editing" further below, which supersedes that original limitation.
- [`docs/ui-production-audit.md`](docs/ui-production-audit.md) — the duplicated-controls fix (`UserMenu.tsx`, sidebar collapse).
- [`docs/settings-modal-visual-bug.md`](docs/settings-modal-visual-bug.md) — the dark-mode transparency fix (`--modal-backdrop`/`--modal-surface` tokens).
- [`docs/navigation-and-actions.md`](docs/navigation-and-actions.md) — the resulting one-action-one-owner table for every control in the shell.

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
`agent.nodes._timed_node` logs a per-node timing line and appends a
`StageTiming` entry to `AgentState["stage_timings"]`.
`observability/metrics.py`'s `PerformanceMetrics` (a process-wide
singleton) aggregates every completed run's timings and status into a
live snapshot, read back via `GET /metrics/performance` (admin-only).

**Invariant that must not regress: deliberately single-process,
in-memory, resets on restart** — a multi-worker deployment would have one
independent rollup per worker (the identical limitation
`agent/rate_limit.py`'s own sliding-window limiters disclose); real
cross-process metrics would need Prometheus/OpenTelemetry, not attempted
here.

**Read [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md)** for the full
correlation-ID/logging/health-check picture, and
[`docs/PERFORMANCE_BASELINE.md`](docs/PERFORMANCE_BASELINE.md)/[`docs/PERFORMANCE_RESULTS.md`](docs/PERFORMANCE_RESULTS.md)
for why no further backend "optimization" is justified today without a
model/hardware/prompt-size tradeoff (LLM inference is 93.8–98% of
wall-clock time).

### Optional, opt-in SQL result charting (`frontend/src/lib/chartEngine.ts`)
A confirmed SQL result never auto-renders a chart — an unobtrusive
"Visualize" button is the only way one appears. Fully client-side
(`plotly` was removed as a backend dependency).

**Invariant that must not regress: the backend's own
`chart_recommendation` is only ever a seed for the *initial* choice —
every chart type's actual enabled/disabled state is recomputed from the
real returned columns/rows on the frontend, every time.** "mixed"
(bar+line) never adds a second y-axis — two measures of different scale
get two charts, never a dual axis, per the loaded data-viz skill's
non-negotiable. A chart config is session-only and does not survive a
page reload or a reloaded-from-server past conversation turn (same
disclosed limitation as "Universal server-side chat history" above).

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#14-sql-result-charting)**
for the full engine design (11 chart types, the NL follow-up chart-switch
detector, the jsdom/Chart.js test-infrastructure fix) and current test
coverage.

### AI Data Analyst depth — trend/variance/outlier detection (`agent/insight.py`)
`ResultSummary` gained three deterministic fields —
`ColumnStat.stddev`/`coefficient_of_variation`, `ResultSummary.trend`, and
`ResultSummary.outliers` — computed the same "Python does the arithmetic,
the LLM only narrates already-computed truths" way the pre-existing
`top_label`/`top_share_percent` fields work, and validated by the same
grounding gate (`is_insight_grounded`).

**Invariant: fully tested but NOT yet wired into the live prompt — do not
treat as user-facing.** `agent.llm_client._build_insight_prompt` is
unchanged, so nothing in the live `generate_insight_node` path surfaces a
trend/variance/outlier claim yet, deliberately deferred: this prompt's
exact wording is what `docs/EVALUATION_CURRENT.md`'s live-LLM benchmark
numbers were measured against, and changing it without being able to
re-run that benchmark would violate this project's "never compromise
correctness" standard.

See [`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md`](docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md)'s
P1 roadmap for where this sits relative to the rest of the AI Data
Analyst work.

### Prompt-injection benchmark and hardening pass (2026-09-24/25)

A 500-case prompt-injection/security benchmark (20 categories) was run to
completion against the real live agent (real Ollama, real SQL Server
databases) — not a static regex-blind-spot probe. Harness:
`eval/security_benchmark/`, CLI entry points
`scripts/run_security_benchmark.py`/`scripts/run_multiturn_persistence_benchmark.py`.

**Headline result as of this writing: all 500 cases completed, 0 critical
findings** — zero writes executed, zero unauthorized sources reached,
zero secrets or system prompts leaked. 66.4% raw pass rate; every
`expected_behavior` mismatch is a content-level miss, never a hard-gate
violation. Five real bugs were found and fixed along the way — including
an unguarded `OPTION (MAXRECURSION 0)` SQL hint that could hang a query
indefinitely (now a flat-denied `unsafe_query_option` violation type), a
secret-redaction gap in LLM-generated response text, and a new
`cross_source_injection_narrative` detection pattern for third-person-
narrated indirect-injection payloads (confirmed live: pass rate on the
two affected categories went 20% → 100%).

**This is a point-in-time result, not a standing guarantee** — re-run the
benchmark after any change to `agent/sql_validator.py`,
`security/injection_patterns.py`, or the orchestrator's prompt
construction before trusting these numbers still hold. **Honest residual
gaps, not yet closed as of this writing:** an admin-role re-run of the
RBAC-bypass categories, and 2 of 10 multi-turn-persistence payloads not
yet separately re-graded against their real history entry.

**Read [`docs/security/PROMPT_INJECTION_BENCHMARK_GAP_REPORT.md`](docs/security/PROMPT_INJECTION_BENCHMARK_GAP_REPORT.md)**
(the authoritative, continuously-updated source) for live results per
category, the exact fix for each of the five bugs above, and the current
state of the residual gaps.

### Chat attachments, image actions, and their security hardening (`attachments/`)
Chat file attachments (images, PDF, DOCX, XLSX, PPTX, TXT, MD, CSV, JSON)
are given directly to the model as context for the question that attached
them, via `POST /attachments/upload` → the `"attachments"` orchestrator
source. Deliberately **not** `rag/ingestion.py`'s pipeline: a chat
attachment is ephemeral, per-conversation, per-caller content, not a
permanent shared knowledge-base entry — storage is a bounded, in-memory,
process-lifetime registry (`attachments/store.py`), never a database row.
Explicit image actions (OCR/text-extraction, deterministic resize, and
OpenCV-based text removal — never a generative model, never a solid-color
rectangle) are separately callable, not just implicit side effects of
asking a question about an image.

**Invariants that must not regress:**
- An attached image is **never silently discarded** — if no vision model
  is configured (or a call fails), it falls back to OCR instead of being
  dropped, and the result records `vision_unavailable=True` so the UI
  shows an honest degraded-mode notice.
- `router_node` **forces** `"attachments"` into the route whenever the
  caller attached a file, regardless of what the LLM classifier picks.
  A deterministic pre-check (`_looks_like_attachment_only_question`)
  additionally keeps an attachment-only question (e.g. "extract the text
  from this image") from also reaching SQL generation just because a
  database happens to be configured — a real routing bug found and fixed,
  not a hypothetical.
- Resize and remove-text actions always produce a **brand-new**
  attachment (`register_derived_image`) — the original is never mutated
  in place.
- Every successfully-processed attachment's extracted text is scanned for
  injection patterns (detection-only, never block) and, if
  `Settings.malware_scan_provider` is enabled, malware-scanned before any
  parser touches the raw bytes. `"disabled"` (the default) cannot
  silently ship to `ENVIRONMENT=production` alongside chat attachments,
  document RAG, policy RAG, or media search — a startup `model_validator`
  refuses to start otherwise.
- DOCX/XLSX/PPTX (plain ZIP archives) get a decompression-bomb guard
  (`attachments/zip_safety.py`, metadata-only, no actual decompression)
  and PDFs get a catalog-level dangerous-content preflight
  (`attachments/pdf_safety.py` — embedded JavaScript, auto-open actions)
  before parsing — both closed gaps found by a direct security-review
  audit, not assumed-covered by the malware scanner above. The PDF
  preflight is catalog-level only, not a full page/annotation walk — a
  disclosed scope boundary, not a silent gap.
- Every processor call runs on a bounded thread pool with a hard timeout
  (`Settings.attachment_processing_timeout_seconds`) — a pathological
  file can't tie up a request thread indefinitely.

**Known, disclosed limitations:** an attachment is global to the caller,
not scoped to a configured database. PPTX speaker notes aren't extracted
(only slide title + body text). A legacy binary Office format
(`.doc`/`.xls`/`.ppt`) is rejected with a message naming the modern
extension needed instead. OCR/text-removal capability flags report
whether the `pytesseract` *Python package* is importable, not whether the
Tesseract *system binary* is installed — a missing binary degrades one
request to an empty result with a warning rather than flipping the
capability flag off. No live ClamAV daemon has ever exercised the
malware-scanner's fail-closed path in this project's history — only a
mocked socket.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#15-chat-attachments-image-actions-and-security-hardening)**
for the full pipeline (per-file-kind processors, the 7-node LangGraph
subgraph, the capability-registry pattern, and the routing-fix/zip-PDF-
safety/injection-detection hardening pass in full detail), and
[`SECURITY.md`](SECURITY.md)'s "Chat attachments — security controls"
section for the user-facing security summary.

### Scale-out program: Phase 0 harness + a same-session hardening pass (2026-09-26)
`docs/SCALE_OUT_PROMPT.md` is an 11-phase program (committed verbatim)
taking this app from single-instance toward horizontally scalable. Phase
0 (done): a load-test harness (`eval/load/`) plus a measured baseline
(`docs/SCALE_BASELINE.md`) — actually running it found and fixed a real,
previously-invisible bug (`db/connection.py::get_engine` was passing a
password-masked connection string to `create_engine`, silently breaking
every discrete-field connection with a real password).

**Invariants that must not regress:** `/ask` concurrency is bounded
(`ThreadPoolExecutor` sized to `Settings.max_concurrent_ask_requests`,
with a per-caller cap) with real admission control — a caller past either
cap gets an immediate 429, before any LLM/DB work starts. Rate/concurrency
limits and attachment ownership are keyed by real identity
(`security.oidc.real_caller_subject`), not raw client IP — includes a
fixed cross-user data-isolation bug where local-auth users were pooled
into one shared "no owner" bucket.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#5-scale-out-program)**
for the full per-change writeup, and
[`docs/SCALE_BASELINE.md`](docs/SCALE_BASELINE.md)'s "Honest capacity
statement" for what's now suitable for multi-instance deployment vs. what
still isn't (rate limiting, the Chroma schema index, and the compiled-
graph/Ollama-client singletons are all still process-local; no true
cross-thread cancellation, streaming, or distributed coordination yet).

### Configurable Ollama model selection for Text-to-SQL (2026-09-27)
A caller may pick which locally-installed Ollama model answers a given
question (`POST /ask`'s optional `model` field) instead of always using
`Settings.ollama_model`. Three deliberately separate concepts:
configured/allowed (`Settings.ollama_allowed_models`, server-validated),
this app's own curated display metadata (cosmetic only), and installed
locally (a live Ollama lookup, queried only by `GET /models`, never by
`/ask` itself).

**Invariants that must not regress:** model selection is **request-scoped,
never global** (`AgentState["selected_model"]`, resolved once, read-only
downstream — the identical `selected_database` pattern) — verified with a
real-threading regression test that no process-global mutable model
variable exists. `POST /ask`'s `model` field is validated *before* any
admission-control slot is acquired or LLM/DB work starts; an
unrecognized/disallowed value is an `HTTP 400`, not a graceful "failed"
run. This feature only ever threads `model` through the four SQL-pipeline
LLM calls — the embedding model, vision model, voice transcription/
synthesis, and RAG/web-search LLM calls are untouched.

**Remaining limitations:** model availability is not live-reloaded
mid-process (a `.env` change needs a restart). No per-role model
restriction exists — every role that can reach `/ask` can select any
configured/installed model. Insight/planning/review always use the exact
same model chosen for generation.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#6-configurable-ollama-model-selection)**
for the full three-concept design, the `GET /models`/frontend
`ModelSelector.tsx` wiring, and a real `pydantic-settings` env-decoding
bug found and fixed while building this.

### Enterprise scalability/security assessment: identity-aware rate limiting, trusted-proxy IP resolution, structured logging, graceful shutdown (2026-09-27)
A dedicated architecture/scalability/security assessment (synthesizing,
not re-deriving, the Scale-out program above plus `docs/THREAT_MODEL.md`)
found and fixed six further, in-place gaps without introducing any new
infrastructure: opt-in trusted-proxy IP resolution
(`security/client_ip.py`, `Settings.trusted_proxy_count`), identity-aware
rate limiting extended to every remaining rate-limited route, structured
JSON logging, a liveness/readiness split (`GET /live` alongside
`GET /health`), graceful shutdown of the `/ask` thread pool, and a startup
warning when `MAX_CONCURRENT_ASK_REQUESTS` exceeds a replica's own
DB-pool capacity.

**Invariant that must not regress:** at `Settings.trusted_proxy_count`'s
default of `0`, client-IP resolution never reads `X-Forwarded-For` —
identical to pre-assessment behavior, locked in by
`tests/security/test_rate_limit_header_spoofing.py`. Only an operator who
explicitly sets the trusted-hop count opts into trusting that header.

**Read [`docs/ENTERPRISE_SCALABILITY_SECURITY_ASSESSMENT.md`](docs/ENTERPRISE_SCALABILITY_SECURITY_ASSESSMENT.md)**
(also summarized in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#7-enterprise-scalabilitysecurity-assessment))
for the full report, current-state/target-state diagrams, and the honest
capacity statement — no million-user or specific-concurrent-user claim is
made anywhere in this pass's documentation.

### AI-guided (generative) image editing (2026-09-27)
Closes a real, previously-disclosed gap: the Edit-image modal's
"AI-guided editing" panel was real UI wired to a deliberate, permanent
stub. Provider: IMA Studio's `image_to_image` task category, chosen after
live verification — none of its 5 available models expose a native
mask/inpainting parameter, a real provider constraint, not worked around
silently.

**Invariants that must not regress:** IMA's lack of a mask channel is
handled by converting this app's own canonical mask into a translucent
red overlay for that one adapter only (`ImaImageEditProvider`) — disclosed
to the user via `ImageEditResult.warnings`, never presented as
pixel-exact masking. A completed edit is always stored as a **brand-new**
attachment (`register_derived_image`) — the original is never mutated.
Generated bytes are decoded/verified via Pillow before being stored —
untrusted output, exactly like "SQL is untrusted output" applied to
pixels. **No SQL-path leakage, structurally guaranteed**:
`attachments/ai_edit.py` imports neither `agent.graph` nor `agent.nodes`
and is reached only through its own dedicated REST route, never through
`/ask`. Only the 4 genuinely generative presets reach the paid endpoint —
blur and table-extraction route to the existing free/analysis endpoints
instead, never the AI-edit one.

**Known, disclosed limitations:** IMA's visual-overlay mask is a
best-effort convention — the model can still edit outside the highlighted
region. A live call against IMA's real infrastructure confirmed the
upload/request/error-propagation path but failed at generation itself
with IMA's own "insufficient points" business error (the configured
account has no credits) — **a successful live generation has not been
confirmed**, only the request path up to that point.

**Read [`docs/image-editing-architecture.md`](docs/image-editing-architecture.md)**'s
"AI-guided editing: the real implementation" section (also summarized in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#8-ai-guided-generative-image-editing))
for the full design and every other disclosed limitation.

### Google sign-in (2026-09-28)
A second way to reach this app's own self-hosted accounts — "Continue
with Google" on both sign-in and sign-up. Layered entirely on top of the
pre-existing local-account system; does not touch OIDC mode, the
static-token mode, or `agent/authz.py`'s RBAC. Uses Google Identity
Services' ID-token flow (not OAuth authorization-code), since this is
authentication only — no `client_secret` is ever held server-side.

**Invariants that must not regress:** `security/google_oidc.py
::verify_google_id_token` validates signature against Google's live,
rotating public keys, issuer, exact `aud`, `azp`, and a server-issued
single-use nonce, and reads `hd` (hosted-domain restriction) from the
**signed claim**, never inferred from the email's `@domain` suffix. `sub`
is the only durable identity key ever stored. Identity linking
(`identity/repositories/external_identities.py`) never auto-merges an
account by email alone, and an unverified-email collision is refused
generically (a non-confirming 401) to avoid becoming an
account-enumeration oracle.

**Honest verification status:** the Alembic migration and every
identity-linking function are live-verified against a real PostgreSQL
database. **A real, browser-based, end-to-end Google sign-in has not been
exercised in this environment** — successful token verification is
necessarily mocked in HTTP-level tests.

**Read [`docs/AUTHENTICATION.md`](docs/AUTHENTICATION.md#google-sign-in-2026-09-28)**
(also summarized in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#9-google-sign-in))
for every server-side check and the full "what's verified vs. not"
breakdown.

### Secure conversation sharing (2026-09-28)
Turns a conversation into a controlled, read-only snapshot other people
may view — never a live query channel, an authentication session, or a
way to reach this app's SQL/RAG/web/model/image pipelines from a shared
view.

**Invariants that must not regress:** `identity/share_policy.py` is the
single RBAC/ABAC decision point every route/resolver calls before
acting — deny-by-default on an unrecognized action, role, or missing
tenant; owner/viewer/anonymous-link-viewer are the only three roles, no
"Editor." `build_share_projection` is a hard **allowlist** (not a
blocklist) of which persisted-turn fields a viewer may ever see — a
newly-added internal field defaults to invisible. This app has no real
multi-tenant model; only the new sharing tables carry a scoped
`tenant_id` — a deliberate, narrower decision than a full retrofit.

**A real bug found only by live-testing the running app, not by
`TestClient` alone:** raising `HTTPException` discards headers a route
had already set on its injected `Response` — the *denial* path of the
anonymous-reachable `GET /share-view/{ref}` was serving default
cache/referrer headers instead of `private, no-store`/`no-referrer`.
Fixed and re-verified live. **Not verified:** a real reverse proxy/CDN was
not available to confirm it honors `Cache-Control: private, no-store` for
this path prefix.

**Read [`docs/SHARING_SECURITY.md`](docs/SHARING_SECURITY.md)** (also
summarized in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#10-secure-conversation-sharing))
for the full data model, token lifecycle, and API surface.

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
  history" above, which does now persist conversations/messages
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
