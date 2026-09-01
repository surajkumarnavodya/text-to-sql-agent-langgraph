# Text-to-SQL Dashboard

Ask questions about a real database in plain English and get back validated,
read-only SQL, a results table, and an auto-picked chart — powered by a
fully local LLM stack (Ollama) and an explicit, self-correcting
[LangGraph](https://langchain-ai.github.io/langgraph/) state machine rather
than a black-box agent.

**Why I built this:** most "chat with your database" demos either trust the
LLM's SQL blindly or hide the reasoning behind an opaque agent loop. I
wanted to build the version that treats LLM output as untrusted by
construction — every query is parsed and allowlisted before it can run,
every retry is visible and inspectable, and the schema the model sees
scales to a database with hundreds of tables instead of assuming a toy
5-table sample. It's also a fully local stack (Ollama + ChromaDB, no API
keys, no data leaving the machine), which matters for anyone who can't send
a real schema or query results to a hosted API.

## Architecture

```mermaid
flowchart TD
    U[User] --> ENTRY{"Streamlit UI<br/>or REST API"}
    ENTRY --> SI["sanitize_input<br/>length cap, Unicode normalization,<br/>prompt-injection pre-filter"]
    SI -->|rejected| STOP1(["Rejected"])
    SI --> CF["classify_followup<br/>standalone / follow-up / ambiguous"]
    CF -->|ambiguous| STOP2(["Needs clarification"])
    CF --> RS["retrieve_schema<br/>ChromaDB top-k + FK-adjacency<br/>bridge expansion"]
    RS --> GS["generate_sql<br/>Ollama, via LangGraph"]
    GS -->|off-topic / LLM error / rate limit| STOP3(["Rejected / Failed / Rate limited"])
    GS --> VS["validate_sql<br/>sqlglot AST allowlist"]
    VS -->|retryable mistake| GS
    VS -->|safety violation| STOP4(["Failed closed<br/>(security gate, no retry)"])
    VS -->|valid| CE["estimate_cost<br/>non-executing EXPLAIN / SHOWPLAN"]
    CE -->|high cost, retryable| GS
    CE -->|low/moderate| ES["execute_sql<br/>read-only engine, row cap, timeout"]
    ES -->|unknown table/column| RS
    ES -->|other error, retries left| GS
    ES -->|timeout| STOP4
    ES -->|success| GI["generate_insight<br/>optional, grounded summary"]
    GI --> REVIEW["Show SQL + cost notice<br/>for review"]
    REVIEW -->|Confirm and Run| RUN[Re-validate + re-execute]
    RUN --> RESULTS[Results table, chart, insight]
```

Retries are capped (`MAX_RETRIES`, default 3) and every attempt is recorded
and shown in the UI's "Retry timeline" — the self-correction loop is meant
to be inspectable, not a black box. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full technical
walkthrough (all eight LangGraph nodes, retry semantics, schema-retrieval
internals) and [`USER_GUIDE.md`](USER_GUIDE.md) for what this looks like
from inside the app.

## Key features

- **Self-correcting retry loop** — a validation, cost-estimate, or
  execution failure feeds the actual error back into the next generation
  attempt, up to a configurable cap (`MAX_RETRIES`), instead of failing on
  the first mistake. Every attempt is recorded and shown in the UI's
  "Retry timeline," not just logged to a terminal.
- **Read-only SQL validator** — an AST-based allowlist (via `sqlglot`), not
  a regex blocklist: only a single `SELECT`/`UNION`/`EXCEPT`/`INTERSECT`
  statement is ever allowed to execute, in every dialect the project
  supports. Also rejects a write/DDL statement embedded anywhere in the
  parsed tree (e.g. a data-modifying CTE) and a denylist of known-dangerous
  functions (`pg_sleep`, `xp_cmdshell`, `OPENROWSET`, ...) — see
  [`SECURITY.md`](SECURITY.md).
- **Schema-aware retrieval** — the LLM never sees the whole schema. Each
  table's DDL (plus sampled real column values, for disambiguating coded
  columns) is embedded in ChromaDB; only the top-k relevant tables are
  retrieved per question, with FK-adjacency expansion to pull in
  structurally-required tables (like a fact table) that plain similarity
  search tends to miss.
- **Proactive query cost estimation** — before a validated query runs, a
  non-executing `EXPLAIN`/`SHOWPLAN` estimates its row count; a query
  estimated as very expensive is never executed at all and is instead fed
  back to the model as a retryable mistake, the same as a syntax error.
- **Multi-turn follow-up questions** — a cheap heuristic classifies each
  question as standalone, a follow-up to the prior exchange, or ambiguous
  (in which case the agent asks for clarification instead of guessing),
  using the session's recent query history as context.
- **Grounded AI insights, not free-form narration** — an optional
  plain-English summary sentence after a successful query is checked
  against the actual result data before it's ever shown; a sentence that
  contains an unsupported number is silently dropped rather than displayed
  as if verified.
- **Basic rate limiting and query-cost/row/timeout controls** — separate
  per-session question-submission and process-wide LLM-call limits, a row
  cap enforced two independent ways, and a query execution timeout — see
  [`SECURITY.md`](SECURITY.md) for exact scope and limits.
- **Fully configurable database connection** — PostgreSQL, MySQL, SQL
  Server, or Oracle via SQLAlchemy, entirely driven by `.env`; no hardcoded
  connection string, host, or schema anywhere in the codebase.
- **Human-readable output formatting** — surrogate/ID columns are hidden
  from the default results view, and column labels are expanded from
  common abbreviations, without ever touching the underlying query.
- **Text-to-SQL benchmark harness** — a growable, categorized dataset (easy/
  medium/hard/real-world/adversarial, 20+ subcategories) graded by real
  execution-accuracy (comparing the agent's actual result set against gold
  SQL run live, not SQL-text similarity), covering retrieval recall, join/
  aggregation/date/GROUP BY/window-function correctness, follow-up and
  security-rejection accuracy, retry/latency/cost metrics, and regression
  detection against a stored baseline (`scripts/run_benchmark.py`, `eval/`).
  See [`docs/EVALUATION.md`](docs/EVALUATION.md) for the actual latest
  measured numbers, not just the methodology.
- **A minimal REST API** (`api/`, FastAPI) alongside the Streamlit UI, for
  programmatic access — a thin wrapper over the same `agent.graph.run_agent`
  the UI calls, so every safety layer applies identically. See
  [`docs/API.md`](docs/API.md).

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| LLM runtime | [Ollama](https://ollama.com) (`llama3.1:8b` default) | Fully local — no API keys, no data leaves the machine, no per-token cost while iterating. |
| Orchestration | [LangGraph](https://langchain-ai.github.io/langgraph/) | An explicit state machine (not a free-form ReAct agent) — the retry/error-feedback path is a fixed, inspectable graph, not implicit agent reasoning. |
| Schema retrieval | [ChromaDB](https://www.trychroma.com/) | Local vector store; keeps the prompt small and relevant on a schema with hundreds of tables instead of dumping everything into context. |
| Database | [SQLAlchemy](https://www.sqlalchemy.org/) | One engine abstraction across 4 supported databases, config-driven, with a real `Inspector`-based introspection API instead of per-engine catalog queries. |
| SQL validation | [sqlglot](https://github.com/tobymao/sqlglot) | Parses the AST and allowlists the statement *type* — can't be bypassed by a syntax variant the way a keyword blocklist can. |
| UI | [Streamlit](https://streamlit.io) + [Plotly](https://plotly.com/python/) | Fast to build a real reviewable UI (editable SQL box, retry timeline, schema browser) without a separate frontend. |
| API | [FastAPI](https://fastapi.tiangolo.com/) | A thin, optional REST surface (`api/`) over the same agent graph the UI calls — see [`docs/API.md`](docs/API.md). |
| Deployment | [Docker](https://www.docker.com/) + Compose | Non-root, pinned, health-checked containers for the UI and API — see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). |

### Supported LLM models

Ollama is the only supported LLM runtime — there is no hosted-API code
path (no OpenAI/Anthropic/etc. client anywhere in the codebase). Any model
`ollama pull`-able works; `OLLAMA_MODEL` is a plain config string
(`config/settings.py`), not a hardcoded value:

| Model | Notes |
|---|---|
| `llama3.1:8b` (default) | What this project is built and benchmarked against — see [`docs/EVALUATION.md`](docs/EVALUATION.md) for measured accuracy. |
| `sqlcoder`, `duckdb-nsql`, or any other Ollama-hosted model | Untested by this project's own benchmark as of this writing, but supported by the same config knob — swap `OLLAMA_MODEL` and re-run `python scripts/run_benchmark.py` to measure it yourself. |

### Supported databases

| `DB_TYPE` | Driver | Notes |
|---|---|---|
| `postgresql` | `psycopg2-binary` | |
| `mysql` | `pymysql` | |
| `mssql` | `pyodbc` | Requires the Microsoft ODBC Driver for SQL Server installed as a system package — see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md). |
| `oracle` | `oracledb` (thin mode) | No separate Oracle Client install needed. |

Selected via `DB_TYPE` in `.env`; see `db/connection.py::SUPPORTED_DB_TYPES`
for the single source of truth this table is drawn from, and
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for every connection
variable.

## Project structure

```
Gen_AI_Project_TSQL/
├── agent/            # LangGraph nodes, state, SQL validator, LLM client, rate limiting
├── api/               # Optional FastAPI REST layer (thin wrapper over agent.graph.run_agent)
├── config/            # Settings (env-driven), table descriptions, sensitive-column classification
├── db/                 # SQLAlchemy engine, schema introspection, query execution, cost estimation
├── docs/              # Architecture, security, deployment, API, evaluation, governance docs
├── embeddings/        # Chroma index build + top-k/FK-adjacency schema retrieval
├── eval/               # Text-to-SQL benchmark harness: dataset, evaluators, metrics, regression
│   └── benchmark/      # Benchmark case YAML files (easy/medium/hard/real_world/adversarial/...)
├── observability/      # LLM call timing capture, result-log redaction
├── scripts/            # CLI entry points: build_embeddings, test_db_connection, run_benchmark, ...
├── security/           # Secret redaction, SecretStr, audit logging, sanitization, injection patterns
├── tests/              # Fully mocked pytest suite (no live DB/Ollama required)
├── ui/                 # Streamlit app (the primary interface) + session history + column formatting
├── Dockerfile, docker-compose.yml, .dockerignore
├── requirements.txt, pyproject.toml
├── tasks.ps1, Makefile
└── README.md, SECURITY.md, CONTRIBUTING.md, USER_GUIDE.md
```

The two things worth knowing before browsing further: `agent/graph.py`
wires everything in `agent/nodes.py` into the state machine described
above, and `ui/app.py`/`api/main.py` are both thin — neither contains
agent logic, they only call `agent.graph.run_agent`. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for what each module is
responsible for in detail.

## Setup

### Prerequisites
- **Python 3.11** (the project's target; see the version note in
  [`CLAUDE.md`](CLAUDE.md) if you're on a newer/older interpreter)
- **[Ollama](https://ollama.com)**, installed and running, with a model pulled:
  ```bash
  ollama pull llama3.1:8b
  ```
- **A database to connect to** — PostgreSQL, MySQL, SQL Server, or Oracle.
  No sample database is bundled (see "Trying it against a sample database"
  below if you don't have one handy).

### Clone and install

**macOS / Linux / WSL (bash):**
```bash
git clone <this-repo-url>
cd Gen_AI_Project_TSQL
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

**Windows (PowerShell):**
```powershell
git clone <this-repo-url>
cd Gen_AI_Project_TSQL
py -3.11 -m venv .venv      # or: python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Then edit `.env` with your real database connection details (every
variable is documented inline in `.env.example`) — **use a dedicated
read-only database account**, not an admin login (see
[`SECURITY.md`](SECURITY.md)).

Verify the connection before doing anything else:
```bash
python scripts/test_db_connection.py
```

### Trying it against a sample database

This project was built and tested against Microsoft's public
**AdventureWorksDW2025** sample data warehouse (SQL Server). If you don't
have a database handy to point this at, you can download and restore it
from Microsoft's official samples page:
[learn.microsoft.com/en-us/sql/samples/adventureworks-install-configure](https://learn.microsoft.com/en-us/sql/samples/adventureworks-install-configure)
— any edition of SQL Server (including the free LocalDB/Express editions)
works.

## How to run

Build the schema embeddings once (and again any time the schema changes —
this is also available from the UI's "Refresh Schema" button):
```bash
python scripts/build_embeddings.py
```

Run the app:
```bash
streamlit run ui/app.py
```

Run the Text-to-SQL benchmark (a live-DB + live-Ollama check, separate from
the mocked `pytest` suite — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for how
to add cases to it):
```bash
python scripts/run_benchmark.py                   # full dataset
python scripts/run_benchmark.py --limit 20         # a quick, smaller run
python scripts/run_benchmark.py --check-regression # compare against the stored baseline
```

Run the mocked unit test suite + linters:
```bash
pytest
ruff check . && black --check . && mypy .
```

PowerShell equivalents for all of the above are in `tasks.ps1`
(`.\tasks.ps1 run`, `.\tasks.ps1 test`, `.\tasks.ps1 lint`); Make targets for
bash are in the `Makefile`.

### Running with Docker

```bash
cp .env.example .env   # then edit .env as above
docker compose build
docker compose up -d
docker compose exec app python scripts/build_embeddings.py
```

UI at `http://localhost:8501`, API at `http://localhost:8000`. See
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for connecting containers to a
host-run Ollama, an external database, reverse-proxy/auth placement, and
what's deliberately not included (Kubernetes, a bundled database/LLM).

## Screenshots

_TODO: add a screenshot of the chat + generated-SQL view, and one of a
results table with an auto-picked chart._

## Known limitations

- **Accuracy depends heavily on the local model, and the real numbers are
  lower than a feature list alone would suggest.** `llama3.1:8b` handles
  straightforward aggregation/filter/join questions well but is noticeably
  weaker on window-function-heavy, ambiguous, or deeply nested subquery
  questions — the latest full benchmark run measured 35% final accuracy /
  30% result-set accuracy overall (92% *execution* accuracy — the SQL
  runs, but is often wrong). See [`docs/EVALUATION.md`](docs/EVALUATION.md)
  for the full breakdown. Swapping to a SQL-specialized model (e.g.
  `sqlcoder`, `duckdb-nsql`) or a larger model generally helps, at the cost
  of speed/memory.
- **Local inference is slower than a hosted API**, especially across
  several retry attempts on a hard question — this trades latency for
  running entirely offline with no per-call cost.
- **Schema retrieval can still miss a required table** on schemas with long,
  multi-hop hierarchies if more than one hop is missing from the initial
  top-k match; the FK-adjacency bridge expansion catches the common single-hop
  case, not every possible gap.
- **Single-user, local-dev oriented.** This has not been hardened for
  concurrent multi-tenant use or production deployment — see
  [`SECURITY.md`](SECURITY.md) before pointing it at anything sensitive.

## More documentation

- [`USER_GUIDE.md`](USER_GUIDE.md) — **start here if you just want to use
  the app.** Plain-language walkthrough of the full workflow: asking a
  question, reviewing generated SQL, cost warnings, confirming execution,
  reading results/charts/insights, follow-ups, history, and what error
  messages mean.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — detailed technical
  walkthrough of the LangGraph node design, retry/self-correction logic,
  and schema-retrieval pipeline.
- [`docs/User_Guide.pdf`](docs/User_Guide.pdf) — an earlier, PDF-format
  general-audience guide. `USER_GUIDE.md` above is the current,
  markdown-native reference kept in sync with the running app; this PDF
  has not been re-verified as part of this documentation pass and may
  describe an older version of the UI.
- [`SECURITY.md`](SECURITY.md) — this project's security posture.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, coding standards, how
  to add eval questions.
- [`docs/API.md`](docs/API.md) — the REST API's endpoints, auth, and design.
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Docker/Compose, external
  Ollama/DB connectivity, reverse-proxy auth, scaling considerations.
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — every `.env` variable,
  grouped and explained.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common failure
  modes and what to do about them.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — the benchmark's actual
  latest measured accuracy, not just its methodology.
- [`docs/PRODUCTION_CHECKLIST.md`](docs/PRODUCTION_CHECKLIST.md) — a
  concrete go/no-go checklist before deploying this for real users.
- [`docs/PRODUCTION_READINESS_REPORT.md`](docs/PRODUCTION_READINESS_REPORT.md) —
  a full production-readiness audit (architecture, security, AI quality,
  reliability, testing, deployment) with a scored assessment and roadmap.
- [`docs/GOVERNANCE.md`](docs/GOVERNANCE.md),
  [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md),
  [`docs/RESPONSIBLE_AI.md`](docs/RESPONSIBLE_AI.md),
  [`docs/RISK_REGISTER.md`](docs/RISK_REGISTER.md),
  [`docs/security-changelog.md`](docs/security-changelog.md) — this
  project's governance, compliance self-assessment, responsible-AI design
  choices, tracked risks, and dated security-change log.

## License

[MIT](LICENSE) — see `LICENSE` for the full text.
