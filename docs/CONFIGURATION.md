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
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `300` | Per-request timeout for Ollama calls. Raise further if you see `httpx.ReadTimeout`/`ConnectTimeout` on slower hardware. |

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
| `COMPLEX_QUERY_MAX_RETRY_BONUS` | `2` | Extra retries (on top of `MAX_RETRIES`) for questions `agent/complexity.py` detects as likely needing more self-correction (top-N-per-group phrasing, YoY/period-over-period comparisons, several metrics requested at once) — one extra retry per distinct signal matched, capped at this value. `0` disables the adaptive bonus. |
| `MAX_RESULT_ROWS` | `1000` | Row cap applied to every executed query (enforced two independent ways — see `SECURITY.md`). |
| `QUERY_TIMEOUT_SECONDS` | `15` | Wall-clock timeout for query execution. |
| `LLM_MAX_TOKENS` | `1024` | Max tokens the LLM may generate per SQL-generation call. |
| `INSIGHT_MAX_TOKENS` | `120` | Max tokens for the post-query plain-English insight sentence. |
| `MAX_QUESTION_LENGTH` | `500` | Max accepted character length of a typed question (`agent/input_guard.py`). |

## Agentic query planning + plan-conformance self-correction

`agent/nodes.py::plan_query_node`/`review_sql_node` -- only triggers for a
question `agent/complexity.py` judges non-trivial (the same signals that
drive `COMPLEX_QUERY_MAX_RETRY_BONUS` above); an ordinary question makes
zero extra LLM calls regardless of `ENABLE_QUERY_PLANNING`.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_QUERY_PLANNING` | `true` | Master switch for the planning + plan-review LLM calls. Off means every question behaves exactly as it did before this feature existed. |
| `QUERY_PLAN_MAX_TOKENS` | `300` | Max tokens for the up-front query plan (a short JSON array of step strings). |
| `SQL_REVIEW_MAX_TOKENS` | `200` | Max tokens for the plan-conformance review verdict ("PASS" or a one-sentence "FAIL: ..."). |

## Rate limiting

Basic in-memory safeguards, appropriate for local/single-user use — not a
distributed multi-tenant rate limiter. See `SECURITY.md`,
`docs/RISK_REGISTER.md`'s R-001.

| Variable | Default | Purpose |
|---|---|---|
| `QUESTION_RATE_LIMIT_PER_MINUTE` | `10` | Max question submissions/minute, per client IP (`api/main.py`). |
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

## Multi-source router (optional, off by default)

See [`docs/MULTI_SOURCE_GUIDE.md`](MULTI_SOURCE_GUIDE.md) for a walkthrough
of turning each of these on; [`docs/ARCHITECTURE.md`](ARCHITECTURE.md#4-multi-source-orchestration)
for how the router/subgraphs work internally.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_MULTI_SOURCE_ROUTER` | `false` | Routes questions through `agent.orchestrator.graph.run_orchestrated` instead of calling `agent.graph.run_agent` directly. Off means the orchestrator graph is never even constructed — a pure pass-through. |

### Document/policy RAG store

| Variable | Default | Purpose |
|---|---|---|
| `RAG_STORE_CONNECTION_STRING` | *(unset)* | SQLAlchemy connection string for a **dedicated** SQL Server 2025+/Azure SQL database (native `VECTOR` column type — `rag/store.py`). Never reuse a `DB_CONNECTIONS` business database. Required for `ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG` to actually work; `SecretStr`-wrapped. |
| `RAG_STORE_ODBC_DRIVER` | `ODBC Driver 17 for SQL Server` | Same meaning as `DB_ODBC_DRIVER`, for the RAG store connection. |
| `ENABLE_DOCUMENT_RAG` | `false` | Offers the general "documents" collection to the router. Needs `RAG_STORE_CONNECTION_STRING` too. |
| `ENABLE_POLICY_RAG` | `false` | Offers the separate, more access-sensitive "policies" collection. Independent of `ENABLE_DOCUMENT_RAG`. |
| `RAG_TOP_K` | `4` | Chunks retrieved per question, per collection. |
| `RAG_MAX_RETRIES` | `2` | Query-rewrite retries in the agentic RAG subgraph before falling back to "insufficient information." |
| `RAG_CHUNK_SIZE` | `1200` | Target chunk length (characters) when splitting an ingested PDF's text. |
| `RAG_CHUNK_OVERLAP` | `150` | Character overlap between consecutive chunks. |
| `RAG_EMBEDDING_MODEL_NAME` | *(blank = reuse `EMBEDDING_MODEL_NAME`)* | Embedding model for document/policy chunks, if it needs to differ from schema retrieval's. |

### Live web search

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_WEB_SEARCH` | `false` | Offers the web_search node to the router. Needs `WEB_SEARCH_API_KEY` too. |
| `WEB_SEARCH_PROVIDER` | `tavily` | Selects the provider (`search/web_search.py::SUPPORTED_SEARCH_PROVIDERS`). Only `tavily` is implemented today. |
| `WEB_SEARCH_API_KEY` | *(unset)* | API key for the configured provider. Get a Tavily key at [tavily.com](https://tavily.com). `SecretStr`-wrapped. |
| `WEB_SEARCH_MAX_RESULTS` | `5` | Max results requested per search call. |

### Voice mode (optional, on by default)

Local speech-to-text/text-to-speech — no cloud API, same posture as
Ollama; unlike media generation, this feature spends no money and calls
no external API once its one-time local model downloads are done, so it
ships enabled. See `CLAUDE.md`'s "Voice mode" section for the full
design.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_VOICE_MODE` | `true` | Whether the mic button/settings toggle appear at all (`GET /health`'s `voice_enabled`). |
| `STT_MODEL_SIZE` | `base` | `faster-whisper` model size (`tiny`/`base`/`small`/`medium`/`large-v3`). Auto-downloaded from Hugging Face Hub on first use. |
| `STT_DEVICE` | `cpu` | `cpu` or `cuda`. Only set `cuda` on a machine with a confirmed working CUDA + cuDNN setup — nothing in this project's Docker image assumes a GPU. |
| `STT_VOCABULARY_MAX_CHARS` | `200` | Caps the schema-derived vocabulary hint fed to Whisper's `initial_prompt` (`voice/stt.py::_build_vocabulary_hint`). |
| `VOICE_MAX_UPLOAD_MB` | `10` | Max size of one recorded-question upload to `POST /voice/transcribe`. |
| `VOICE_MAX_DURATION_SECONDS` | `30` | Max recording duration, checked before transcription runs. |
| `TTS_VOICE` | `en_US-lessac-medium` | Piper voice name — download it once with `python scripts/download_voice_model.py`. |
| `TTS_VOICE_MODEL_PATH` | *(unset)* | Override for the `.onnx`/`.onnx.json` location, if not the default `voice/models/<TTS_VOICE>.onnx`. |

## Validation behavior worth knowing

- **Missing vs. malformed are treated differently.** A missing `DB_HOST`
  is fine at import time (some non-DB functionality, like linting, doesn't
  need it) but raises `ConfigurationError` the moment something actually
  tries to connect (`db/connection.py::build_connection_url`). A
  *malformed* value (e.g. `DB_PORT=notanumber`) raises immediately at
  `Settings` construction — see `config/settings.py::_env_optional_int_strict`.
- **Security-relevant values are validated for sanity, not just type.**
  `MAX_RETRIES`, `MAX_RESULT_ROWS`, both rate limits, both cost thresholds,
  `RAG_TOP_K`, `RAG_MAX_RETRIES`, `RAG_CHUNK_SIZE`, `WEB_SEARCH_MAX_RESULTS`,
  etc. must be positive (`COMPLEX_QUERY_MAX_RETRY_BONUS` is the one
  exception -- `0` is a valid, deliberate "disable this" value, so only
  negative is rejected); `COST_MODERATE_ROW_THRESHOLD` must be strictly
  less than `COST_HIGH_ROW_THRESHOLD`; `LOG_REDACTION_LEVEL` must be
  `standard` or `strict` — all enforced in
  `Settings._validate_security_settings()`, with regression coverage in
  `tests/test_settings_validation.py`.
- **`Settings` is a process-wide, cached singleton** (`get_settings()`,
  `@lru_cache`) — changing `.env` requires a process restart to take
  effect, same as `db.connection`'s cached engine and
  `agent.rate_limit`'s process-wide limiter.
