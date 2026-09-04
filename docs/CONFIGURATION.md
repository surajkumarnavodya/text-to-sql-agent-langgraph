# Configuration Reference

Every environment variable this project reads, in one browsable table.
`.env.example` remains the source of truth for defaults and inline
guidance (copy it to `.env` and edit) — this document exists to make the
full set scannable at once, grouped by concern, with which module actually
reads each one. All parsing/validation happens in `config/settings.py`;
malformed values (not missing ones) fail fast at startup with a
`ConfigurationError` — see that module's docstring.

## Ollama (local LLM)

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Base URL of the Ollama server. `agent/llm_client.py`, `api/main.py`'s health check. |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model name for SQL generation/insight. Swap to try `sqlcoder`, `duckdb-nsql`, etc. |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `60` | Per-request timeout for Ollama calls. |

## Database connection

| Variable | Default | Purpose |
|---|---|---|
| `DB_TYPE` | *(required)* | `postgresql` \| `mysql` \| `mssql` \| `oracle`. Selects the SQLAlchemy driver + sqlglot dialect (`db/connection.py::SUPPORTED_DB_TYPES`). |
| `DB_HOST` | *(required unless `DB_CONNECTION_STRING` set)* | Database host. |
| `DB_PORT` | DB_TYPE's default | Database port. |
| `DB_NAME` | *(required unless `DB_CONNECTION_STRING` set)* | Database/catalog name. |
| `DB_USER` | — | Login username. **Use a dedicated read-only account** — see `SECURITY.md`. |
| `DB_PASSWORD` | — | Login password. Wrapped in `SecretStr`, never logged in plaintext. |
| `DB_SCHEMA` | *(database default)* | Restrict introspection (and what the LLM sees) to one schema. |
| `DB_CONNECTION_STRING` | — | Full SQLAlchemy connection string, used as-is instead of the discrete fields above if set. Also `SecretStr`-wrapped. |
| `DB_ODBC_DRIVER` | `ODBC Driver 17 for SQL Server` | Only used for `DB_TYPE=mssql` — must match a driver actually installed (`odbcinst -j` / Windows ODBC Data Sources). |

### Multiple databases (optional)

| Variable | Default | Purpose |
|---|---|---|
| `DB_CONNECTIONS` | *(unset)* | Comma-separated list of connection names, e.g. `sales,hr`. Unset means a plain single-database setup — the `DB_*` block above is used as-is, internally named `"default"`. When set, the agent auto-routes each question to whichever configured database looks relevant (`embeddings/retriever.py::select_database`) — there is no manual database picker. |
| `DB_<NAME>_TYPE`, `DB_<NAME>_HOST`, `DB_<NAME>_PORT`, `DB_<NAME>_NAME`, `DB_<NAME>_USER`, `DB_<NAME>_PASSWORD`, `DB_<NAME>_SCHEMA`, `DB_<NAME>_CONNECTION_STRING`, `DB_<NAME>_ODBC_DRIVER` | — | Per-connection fields, one full `DB_*` set per name listed in `DB_CONNECTIONS` (`<NAME>` = the name uppercased, non-alphanumeric characters replaced with `_`). Same meaning as the unprefixed fields above. See `.env.example` for a worked two-database example. |

Each configured database gets its own Chroma collection and schema-index
cache (`embeddings/schema_indexer.py`), so `python scripts/build_embeddings.py`
builds/refreshes all of them in one run; `python scripts/test_db_connection.py`
checks all of them too.

## ChromaDB (schema retrieval index)

| Variable | Default | Purpose |
|---|---|---|
| `CHROMA_PERSIST_DIR` | `./embeddings/.chroma` | Where the Chroma index persists to disk. |
| `CHROMA_COLLECTION_NAME` | `schema_ddl` | Name of the Chroma collection holding schema DDL. |
| `EMBEDDING_MODEL_NAME` | `all-MiniLM-L6-v2` | Embedding model for schema DDL and questions. |
| `SCHEMA_TOP_K` | `4` | Number of most-relevant tables retrieved per question. |

## Agent limits

| Variable | Default | Purpose |
|---|---|---|
| `MAX_RETRIES` | `3` | Max self-correction retries in the LangGraph loop. |
| `MAX_RESULT_ROWS` | `1000` | Row cap applied to every executed query (enforced two independent ways — see `SECURITY.md`). |
| `QUERY_TIMEOUT_SECONDS` | `15` | Wall-clock timeout for query execution. |
| `LLM_MAX_TOKENS` | `1024` | Max tokens the LLM may generate per SQL-generation call. |
| `INSIGHT_MAX_TOKENS` | `120` | Max tokens for the post-query plain-English insight sentence. |
| `MAX_QUESTION_LENGTH` | `500` | Max accepted character length of a typed question (`agent/input_guard.py`). |

## Rate limiting

Basic in-memory safeguards, appropriate for local/single-user use — not a
distributed multi-tenant rate limiter. See `SECURITY.md`,
`docs/RISK_REGISTER.md`'s R-001.

| Variable | Default | Purpose |
|---|---|---|
| `QUESTION_RATE_LIMIT_PER_MINUTE` | `10` | Max question submissions/minute — per Streamlit session in the UI, per client IP in the API (`api/main.py`). |
| `LLM_CALL_RATE_LIMIT_PER_MINUTE` | `20` | Max LLM *generation* calls/minute, process-wide — stricter, since retries can multiply calls. |

## Query cost estimation

| Variable | Default | Purpose |
|---|---|---|
| `COST_ESTIMATION_ENABLED` | `true` | Whether `db/query_cost.py` runs a non-executing EXPLAIN/SHOWPLAN before a validated query. Fails open regardless. |
| `COST_ESTIMATION_TIMEOUT_SECONDS` | `3` | Timeout for the plan-only estimate call itself. |
| `COST_MODERATE_ROW_THRESHOLD` | `50000` | Estimated rows above which a query still runs, with a "this may take a moment" notice first. |
| `COST_HIGH_ROW_THRESHOLD` | `1000000` | Estimated rows above which a query is not run at all (retryable, fed back to generation). Must be strictly greater than the moderate threshold. |

## Logging

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Root logging level. |
| `LOG_REDACTION_LEVEL` | `standard` | `standard` (row/column counts + column names) or `strict` (counts only) — how much result-set shape gets logged. Never cell values, at either level. |

## REST API (`api/`)

| Variable | Default | Purpose |
|---|---|---|
| `API_AUTH_TOKEN` | *(unset)* | Optional shared bearer token required on `/ask`/`/schema/tables`. A lightweight hook, not real auth — see `docs/API.md`. |

## Validation behavior worth knowing

- **Missing vs. malformed are treated differently.** A missing `DB_HOST`
  is fine at import time (some non-DB functionality, like linting, doesn't
  need it) but raises `ConfigurationError` the moment something actually
  tries to connect (`db/connection.py::build_connection_url`). A
  *malformed* value (e.g. `DB_PORT=notanumber`) raises immediately at
  `Settings` construction — see `config/settings.py::_env_optional_int_strict`.
- **Security-relevant values are validated for sanity, not just type.**
  `MAX_RETRIES`, `MAX_RESULT_ROWS`, both rate limits, both cost thresholds,
  etc. must be positive; `COST_MODERATE_ROW_THRESHOLD` must be strictly
  less than `COST_HIGH_ROW_THRESHOLD`; `LOG_REDACTION_LEVEL` must be
  `standard` or `strict` — all enforced in
  `Settings._validate_security_settings()`, with regression coverage in
  `tests/test_settings_validation.py`.
- **`Settings` is a process-wide, cached singleton** (`get_settings()`,
  `@lru_cache`) — changing `.env` requires a process restart to take
  effect, same as `db.connection`'s cached engine and
  `agent.rate_limit`'s process-wide limiter.
