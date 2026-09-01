# CLAUDE.md — Project Context for Claude Code Sessions

This file orients any future Claude Code session working in this repo. Read this
before making changes.

## What this project is

A Text-to-SQL dashboard connected to a real, user-configured database. A
user types a natural-language question in Streamlit, a LangGraph agent turns
it into SQL against the configured database (schema retrieved via ChromaDB,
embedded from **live schema introspection** — not a hardcoded sample — so
only relevant tables are shown to the LLM), the SQL is validated
(SELECT-only allowlist), executed read-only, and the result is rendered as a
table + auto-picked Plotly chart. The LLM runs locally via Ollama — no
network calls for the LLM, no API keys required for that part. Database
connectivity is fully config-driven via `.env`; there is no hardcoded
connection string, host, or schema anywhere in the codebase.

**History note:** the project originally shipped with a bundled sample
DuckDB e-commerce database for demo purposes. That was fully removed (by
explicit user decision, no fallback/demo mode kept) in favor of connecting
only to a real database via SQLAlchemy. If you see references to DuckDB,
`db/schema.sql`, or `scripts/seed_db.py` anywhere (docs, old branches,
stale comments), they're leftover from that phase and should be treated as
wrong, not as a parallel supported mode.

## Tech stack

| Concern | Choice |
|---|---|
| LLM runtime | Ollama, default model `llama3.1:8b` (swap via `.env` / `config/settings.py`, e.g. `sqlcoder`, `duckdb-nsql`) |
| Orchestration | LangGraph — explicit state machine, not a black-box agent |
| Schema retrieval | ChromaDB (persisted locally) — embeds table DDL synthesized from live introspection, retrieves top-k relevant tables per question |
| Database | User's own — PostgreSQL, MySQL, SQL Server, or Oracle, via SQLAlchemy. Config-driven (`DB_TYPE` + connection params in `.env`), pluggable per `db.connection.SUPPORTED_DB_TYPES` |
| SQL parsing/validation | sqlglot — parses generated SQL and checks statement type against an allowlist, in the dialect matching `DB_TYPE` |
| UI | Streamlit + Plotly |
| Python | 3.11 is the target per project spec. **This machine only has 3.14 installed** (no 3.11 on PATH via `py -0p`) — the venv was created against 3.14. If a future session hits a wheel-availability issue for a pinned dependency, that's why. Re-run `py -0p` to check if 3.11 has since been installed and consider recreating `.venv` against it if so. |

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
  host-unreachable, db-not-found, driver-missing, timeout, unknown).
  `schema_introspection.py` is the **sole source of truth** for schema
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
- `embeddings/` — `schema_indexer.py`'s `build_index(tables, ...)` takes
  already-introspected `TableSchemaInfo` objects (not a file, not an
  engine) and embeds them into Chroma, keyed by a hash of the introspected
  schema so re-embedding only happens when the schema actually changed.
  `refresh_schema_index(engine, settings, force)` is the single
  introspect → sample → embed pipeline shared by `scripts/build_embeddings.py`
  and `ui/app.py`'s schema initialization, so that three-step sequence is
  defined exactly once. `retriever.py` does top-k similarity search over
  those table-level DDL chunks given a question — unchanged in shape from
  before, just fed by live data now.
- `agent/` — LangGraph nodes live in `nodes.py`, one function per node, each
  taking and returning `AgentState` (defined in `state.py`). `graph.py`
  wires them together and compiles the graph. `sql_validator.py` is the
  security boundary — see below. The sqlglot dialect used for validation is
  resolved from `Settings.db_type` via `db.connection.get_sqlglot_dialect()`
  — never hardcoded to one engine.
- `ui/app.py` — the only file that imports Streamlit. It imports the
  compiled graph from `agent/graph.py` and calls it; it does not contain any
  agent logic itself. Manual "Confirm and Run" button gates *displayed*
  execution — see "SQL is untrusted output, always" below for the nuance
  around the agent's own internal self-correction executions. On startup,
  a failed `test_connection()` shows a setup screen and `st.stop()`s rather
  than letting the app render broken. The sidebar exposes connection status,
  a manual re-test, a manual schema refresh, and a schema browser.
- `scripts/` — standalone entry points: `test_db_connection.py` (verify
  `.env` before booting anything else — prints pass/fail, DB version, table
  count, or a classified readable error), `build_embeddings.py`
  (introspect + embed, with `--force`), `integration_test.py` (manual,
  requires a real DB, not part of the pytest suite — see `tests/` below),
  `run_benchmark.py` (the Text-to-SQL benchmark runner — see `eval/` below;
  also manual/real-DB-required, not part of the pytest suite).
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
  see both files' deprecation notes.
- `tests/` — pytest, all fully mocked, no real DB or Ollama required.
  Mirrors package names (`test_sql_validator.py`, `test_connection.py`,
  `test_schema_introspection.py`, `test_schema_retriever.py`,
  `test_agent_nodes.py`), plus `test_eval_*.py` for the benchmark
  framework's own logic (dataset loading, grading, metrics, regression
  detection — not a live run, which stays manual like `run_benchmark.py`
  itself).

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
