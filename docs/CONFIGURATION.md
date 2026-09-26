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

Real, in-process safeguards — not yet distributed across replicas (see
`docs/SCALE_OUT_PROMPT.md`'s Phase 3). See `SECURITY.md`,
`docs/RISK_REGISTER.md`'s R-001.

| Variable | Default | Purpose |
|---|---|---|
| `QUESTION_RATE_LIMIT_PER_MINUTE` | `10` | Max question submissions/minute, per caller — an authenticated subject when local/OIDC auth is on, else client IP (`api/main.py`'s `_rate_limit_key`). |
| `LLM_CALL_RATE_LIMIT_PER_MINUTE` | `20` | Max LLM *generation* calls/minute, process-wide — stricter, since retries can multiply calls. |
| `MAX_CONCURRENT_ASK_REQUESTS` | `50` | Max `POST /ask` executions running at once, process-wide — an *in-flight* concurrency cap (distinct from the per-minute limit above), also what sizes the bounded ask-worker thread pool. A caller past this gets an immediate 429, not a queued wait. |
| `MAX_CONCURRENT_ASK_REQUESTS_PER_CALLER` | `2` | Max `POST /ask` executions one caller may have in flight at once (same caller key as `QUESTION_RATE_LIMIT_PER_MINUTE`) — a fairness bound, not a throughput control. |

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
| `API_AUTH_TOKEN` | *(unset)* | Optional shared bearer token, checked when no OIDC identity is present. Grants a fixed admin-equivalent identity — see `docs/AUTHENTICATION.md`. Can be set alongside OIDC below ("Combining modes"). |
| `ENVIRONMENT` | `development` | `development` or `production`. Only consequence today: `production` refuses to start at all if neither `API_AUTH_TOKEN` nor `OIDC_ISSUER` is configured, and refuses to start if any configured database's role appears to hold write privileges — see `docs/AUTHENTICATION.md`. |

### Authentication (OIDC/JWT) and authorization (see [`docs/AUTHENTICATION.md`](AUTHENTICATION.md) / [`docs/AUTHORIZATION.md`](AUTHORIZATION.md))

Off entirely unless `OIDC_ISSUER` is set — leaving it blank (the default)
is a complete no-op, identical behavior to before this feature existed.

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | *(unset)* | Your identity provider's issuer URL. Setting this turns OIDC mode on. |
| `OIDC_AUDIENCE` | *(unset)* | Required whenever `OIDC_ISSUER` is set (refuses to start otherwise) — the API identifier your IdP mints access tokens for. |
| `OIDC_JWKS_URL` | *(blank = discovered)* | Only needed if your provider doesn't support standard `/.well-known/openid-configuration` discovery. |
| `OIDC_ALGORITHMS` | `RS256` | Comma-separated server-side algorithm allowlist — never read from the token's own `alg` header. Must never include `none` (refuses to start if it does). |
| `OIDC_CLOCK_SKEW_SECONDS` | `60` | Leeway for ordinary clock drift when checking token expiration/not-before. |
| `OIDC_ROLE_CLAIM` | `roles` | Which JWT claim carries the caller's role(s) — see `docs/AUTHORIZATION.md` for the role → permission mapping (`agent/authz.py`). |
| `ENABLE_SECURITY_HEADERS` | `true` | HSTS/CSP/X-Frame-Options/X-Content-Type-Options/Referrer-Policy/Permissions-Policy on every response — see `SECURITY.md`. |
| `CONTENT_SECURITY_POLICY` | *(unset = built-in default)* | Overrides the built-in CSP entirely (set to an empty string to omit the CSP header while keeping the others). |
| `HSTS_MAX_AGE_SECONDS` | `31536000` (1 year) | `Strict-Transport-Security` header's `max-age`. |
| `CORS_ALLOWED_ORIGINS` | *(unset)* | Comma-separated allowed origins for cross-origin requests (e.g. a separate Vite dev server). Must never contain `*` — refuses to start if it does, since this app's CORS middleware always sets `allow_credentials=True`. |

### Frontend OIDC login (`frontend/`, build-time Vite env vars — see [`frontend/src/lib/auth.ts`](../frontend/src/lib/auth.ts))

**Not read by the Python backend** — these are baked into the built JS at
`npm run build`/`npm run dev`, and are not secrets (a public OIDC client,
Authorization Code + PKCE, no `client_secret`, is designed to be embedded
in a public SPA). Leaving `VITE_OIDC_AUTHORITY`/`VITE_OIDC_CLIENT_ID`
unset (the default) leaves the dashboard's login screen off entirely — it
falls back to `VITE_API_AUTH_TOKEN` or no auth, exactly as before this
feature existed.

| Variable | Default | Purpose |
|---|---|---|
| `VITE_OIDC_AUTHORITY` | *(unset)* | Your identity provider's issuer URL. Must be registered as a separate public/SPA OIDC client there. |
| `VITE_OIDC_CLIENT_ID` | *(unset)* | The SPA's own client id at the identity provider. |
| `VITE_OIDC_SCOPE` | `openid profile` | OAuth scopes requested. |
| `VITE_OIDC_REDIRECT_URI` | `<origin>/auth/callback` | Must be registered as an allowed redirect URI at the identity provider. |
| `VITE_OIDC_POST_LOGOUT_REDIRECT_URI` | `<origin>` | Where the IdP sends the user after sign-out. |
| `VITE_API_AUTH_TOKEN` | *(unset)* | A build-time copy of `API_AUTH_TOKEN` above, used as a fallback when OIDC isn't configured (or as the sole credential for a trusted-network/single-operator deployment). |

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

### Media search (optional, off by default)

Content-based search over an untagged local image/video library. Unlike
voice mode, this stays off by default: it needs a real
`MEDIA_LIBRARY_PATH` configured, and its default local-CLIP embedding
path pulls in `torch`/`opencv-python` — a real, meaningfully larger
dependency footprint than this project's other optional features. See
`CLAUDE.md`'s "Media search" section for the full design.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_MEDIA_SEARCH` | `false` | Whether the "media_search" orchestrator source and `POST /search/media` are offered at all. Also requires `MEDIA_LIBRARY_PATH`. |
| `MEDIA_LIBRARY_PATH` | *(unset)* | Root folder `scripts/build_media_index.py` walks to ingest images/videos. |
| `MEDIA_EMBEDDING_PROVIDER` | `local_clip` | Only `local_clip` (on-device, via `sentence-transformers`) is implemented today — see `media/embedding.py`. |
| `MEDIA_CLIP_MODEL_NAME` | `clip-ViT-B-32` | The CLIP checkpoint used to embed both images and query text. |
| `MEDIA_VISION_MODEL` | *(unset)* | An Ollama vision-capable model name (e.g. `llava`) for per-video-segment captioning — pull it once with `ollama pull llava`. Blank skips captioning; segments are still indexed via ASR transcript + OCR text alone. |
| `MEDIA_MAX_FILE_MB` | `200` | Max size of one file `media/ingest.py` will process. |
| `MEDIA_SEARCH_TOP_K` | `5` | Max hits returned per query, across images and video segments combined. |
| `MEDIA_SCENE_DETECT_THRESHOLD` | `27.0` | `PySceneDetect`'s `ContentDetector` sensitivity — lower detects more (subtler) scene changes. |

### Content moderation gate (mandatory, not a feature flag)

Runs before anything is embedded/stored by **either** media search
(`ENABLE_MEDIA_SEARCH`) or document/policy RAG (`ENABLE_DOCUMENT_RAG`/
`ENABLE_POLICY_RAG`) — there is deliberately no flag to disable this while
either pipeline is on; missing configuration below fails ingestion closed
with a clear error instead. See `moderation/taxonomy.py`'s module
docstring for the full category table and `SECURITY.md`'s "Content
moderation gate" section for the design rationale.

| Variable | Default | Purpose |
|---|---|---|
| `MODERATION_PROVIDER` | `azure_content_safety` | Selects the provider (`moderation/provider.py::SUPPORTED_MODERATION_PROVIDERS`). Only Azure AI Content Safety is implemented today. |
| `AZURE_CONTENT_SAFETY_ENDPOINT` | *(unset)* | Your Content Safety resource's REST endpoint. |
| `AZURE_CONTENT_SAFETY_KEY` | *(unset)* | Subscription key for the endpoint above. `SecretStr`-wrapped. |
| `MODERATION_SEVERITY_THRESHOLD` | `4` | Minimum Azure severity (0/2/4/6) that hard-rejects a Hate/SelfHarm/Sexual/Violence category. Verify against your Content Safety API version. |
| `MODERATION_BLOCKLIST_PATH` | *(blank = `config/moderation_blocklist.yaml`)* | Override for the weapons/drugs text-term blocklist — Azure has no dedicated category for either. |
| `MODERATION_STORE_CONNECTION_STRING` | *(unset)* | Dedicated SQL Server connection for moderation decisions/dedupe metadata (`moderation.media_assets`) — separate from `DB_CONNECTIONS` **and** `RAG_STORE_CONNECTION_STRING` (point both at the same database if you want one shared store). `SecretStr`-wrapped. |
| `MODERATION_STORE_ODBC_DRIVER` | `ODBC Driver 17 for SQL Server` | Same meaning as `DB_ODBC_DRIVER`, for this connection. |
| `MODERATION_STORE_POOL_SIZE` | `10` | `QueuePool` size — explicit here (unlike `DB_CONNECTIONS`/`RAG_STORE_CONNECTION_STRING`, both on SQLAlchemy's default of 5) since ingestion can run many concurrent DB writes. |
| `MODERATION_STORE_MAX_OVERFLOW` | `20` | `QueuePool` burst ceiling above the pool size. |
| `MODERATION_STORE_POOL_RECYCLE_SECONDS` | `1800` | Discard/replace a pooled connection after this long, regardless of use — set below your SQL Server/network's idle-connection timeout. |
| `MEDIA_INGEST_WORKERS` | `4` | Concurrent worker threads `scripts/build_media_index.py` uses (this project's first bounded thread pool — previously a sequential loop). |
| `MEDIA_IMAGE_TILE_THRESHOLD_PX` | `2048` | An ingested image larger than this (either dimension) is tiled before moderation, so a classifier's own downsampling can't hide a small region of concern. |

## Chat attachments (images, PDF, DOCX, XLSX, PPTX, TXT, MD, CSV, JSON)

On by default — no cost, no outbound network call. `POST /attachments/upload`
validates, malware-scans (see below), stores, and eagerly processes each
file (`attachments/pipeline.py`); `POST /ask`'s `attachment_ids` gives the
model actual access via a dedicated LangGraph subgraph
(`attachments/graph.py`). Works even with `ENABLE_MULTI_SOURCE_ROUTER=false`
— attaching a file is a per-request opt-in, not a standing routing
decision. See `CLAUDE.md`'s "Chat attachments" and "Explicit image
actions" sections for the full design.

**Image understanding needs a vision-capable model.** Set
`MEDIA_VISION_MODEL` (above, in the Media search section) to a
vision-capable Ollama model — check with `ollama list` and look for
`"vision"` in that model's own capabilities, since not every locally
pulled model can see images (e.g. this project's own default
`OLLAMA_MODEL=llama3.1:8b` cannot). Without it, an attached image still
uploads and stores normally, but a question about its visual content
degrades to on-screen-text OCR only (needs the system Tesseract binary —
see `CLAUDE.md`'s Windows-specific notes) and returns a specific
"image understanding is not configured" message rather than a generic
failure — never a false claim that the image was analyzed.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_CHAT_ATTACHMENTS` | `true` | Whether the composer's attachment upload/`POST /ask` attachment path is offered at all. |
| `MAX_ATTACHMENT_IMAGE_BYTES` | `10485760` (10MB) | Per-image upload size cap. |
| `MAX_ATTACHMENT_DOCUMENT_BYTES` | `26214400` (25MB) | Per-document (PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/JSON) upload size cap. |
| `MAX_ATTACHMENTS_PER_MESSAGE` | `5` | Max files one `/ask` call may attach at once. |
| `MAX_TOTAL_ATTACHMENT_BYTES` | `52428800` (50MB) | Combined size cap across every attachment on one message. |
| `MAX_ATTACHMENT_TEXT_CHARS` | `80000` | Ceiling on combined extracted-text characters injected into one generation prompt, across all attachments. |
| `MAX_ATTACHMENT_DOCUMENT_PAGES` | `200` | Max pages read from one attached PDF. |
| `MAX_ATTACHMENT_SPREADSHEET_ROWS` | `500` | Max rows read per sheet from one attached XLSX/CSV. |
| `MAX_ATTACHMENT_IMAGE_DIMENSION_PX` | `1568` | An attached image wider/taller than this is downscaled before being sent to the vision model (never affects the stored original). |
| `ATTACHMENT_STORAGE_DIR` | `./data/attachments` | Where attachment bytes are stored, one file per generated `attachment_id` (never the caller's filename). Gitignored, like every other runtime-data directory. |
| `ATTACHMENT_RETENTION_HOURS` | `24` | How long a stored attachment is kept before opportunistic cleanup considers it eligible for deletion (this project has no background scheduler — cleanup runs on each new upload, not on a timer). |
| `MAX_ATTACHMENT_RESIZE_DIMENSION_PX` | `4096` | Upper bound on a requested `POST /attachments/{id}/resize` target width/height, and the working size OCR/text-removal downscale an oversized source image to first. |
| `ATTACHMENT_OCR_TIMEOUT_SECONDS` | `20.0` | Per-call timeout for a Tesseract OCR pass (`POST /attachments/{id}/extract-text`, and the auto-detect step of `remove-text`). |
| `MAX_TEXT_REMOVAL_REGIONS` | `20` | Max text regions one `POST /attachments/{id}/remove-text` call will mask and inpaint at once. |
| `MAX_ATTACHMENT_ZIP_UNCOMPRESSED_BYTES` | `209715200` (200MB) | Decompression-bomb guard for DOCX/XLSX/PPTX (all plain ZIP archives) — rejected if the archive's own central-directory metadata reports more than this total uncompressed size, checked before any parser decompresses a single entry. |
| `MAX_ATTACHMENT_ZIP_ENTRIES` | `2000` | Companion entry-count cap for the same ZIP-container guard. |
| `ATTACHMENT_PROCESSING_TIMEOUT_SECONDS` | `30.0` | Hard wall-clock bound on one attachment's processor call — a pathological file can't hang a request thread past this. |

**Explicit image actions** — `POST /attachments/{id}/extract-text` (real
Tesseract OCR, distinct from vision-model description), `POST
/attachments/{id}/resize` (deterministic Pillow work, no model call),
`GET /attachments/{id}/detect-text-regions` + `POST
/attachments/{id}/remove-text` (classical OpenCV inpainting — explicitly
**not** generative AI, and never a solid rectangle) all reuse the settings
above; see `docs/API.md`'s "Attachments" section for the request/response
shapes and `GET /attachments/capabilities` for a live, per-deployment
capability report.

## Malware scanning

Binary-signature scan of an upload's raw bytes (`security/malware_scanner.py`),
before any parser (`pypdf`/`pymupdf`/Pillow/`python-docx`/`openpyxl`/
`python-pptx`) ever touches them — shared by chat attachments
(`attachments/pipeline.py`), document/policy RAG PDF uploads
(`rag/ingestion.py`), and media-library ingestion (`media/ingest.py`).

Off by default (no scanning capability existed in this codebase before it
was added — defaulting it "on" would break every existing deployment with
no ClamAV daemon reachable), but **fail-closed once configured**: an
infected result and a scanner error/timeout/unreachable daemon are both
treated as a rejection, never silently treated as clean. A production
deployment (`ENVIRONMENT=production`) with chat attachments, document RAG,
policy RAG, or media search enabled **must** set this to `clamav` —
`Settings` refuses to start otherwise (see
`config/settings.py::_require_malware_scanning_in_production`).

| Variable | Default | Purpose |
|---|---|---|
| `MALWARE_SCAN_PROVIDER` | `disabled` | `disabled` \| `clamav` (`security/malware_scanner.py::SUPPORTED_MALWARE_SCAN_PROVIDERS`). |
| `CLAMAV_HOST` | `localhost` | Hostname/IP of the `clamd` daemon. Only read when `MALWARE_SCAN_PROVIDER=clamav`. |
| `CLAMAV_PORT` | `3310` | TCP port `clamd` listens on (ClamAV's own documented default). |
| `MALWARE_SCAN_TIMEOUT_SECONDS` | `15.0` | Socket timeout for one `clamd` `INSTREAM` scan call. |

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
