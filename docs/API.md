# API

A REST API (`api/`, FastAPI) over the LangGraph agent. This same process
also serves the React dashboard (`frontend/`), the primary human-facing
surface (SQL review, retry timeline, schema browser, charts) — see
`api/main.py`'s `StaticFiles` mount. The API remains fully usable on its
own for programmatic/scripted access and as the
interface `docs/DEPLOYMENT.md`'s reverse-proxy/container guidance sits in
front of.

## Why this is safe: no second implementation

`POST /ask` calls `agent.orchestrator.graph.run_orchestrated` directly —
which itself is a pure pass-through to `agent.graph.run_agent` unless
`ENABLE_MULTI_SOURCE_ROUTER` is set. Every safety layer described in
`SECURITY.md` (input guard, SQL validator's SELECT-only allowlist, row
cap, query timeout, the process-wide LLM-call rate limiter,
sensitive-column blocking) governs this endpoint identically, because it's
the same graph execution, not a parallel code path that could drift out of
sync or be weaker. `GET /schema/tables` similarly reuses
`db.schema_introspection.introspect_schema` — the same metadata-only
introspection the dashboard's schema browser and
`scripts/build_embeddings.py` use.

## Running it

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Or via Docker Compose — see `docs/DEPLOYMENT.md` (`docker compose up api`).
It reads the same `.env` the React dashboard's backend calls into (same
`config/settings.py`, same database/Ollama/Chroma configuration) — there
is nothing API-specific to configure beyond the optional `API_AUTH_TOKEN`
described below.

## Endpoints

### `GET /health`

Real, non-cached reachability check of every external dependency: database
(`db.connection.test_connection`), Ollama (a cheap `list()` call — no
generation), and the Chroma schema index (collection reachable and
non-empty). Returns HTTP `200` with `{"status": "ok", ...}` when everything
is reachable, `503` with `{"status": "degraded", ...}` otherwise, so
container/orchestrator health-check tooling that checks the status code
works correctly. Never requires auth (a health check consumed by
infrastructure tooling, not a data-exposing endpoint).

```json
{
  "status": "ok",
  "database": {"ok": true, "detail": "Connection successful."},
  "ollama": {"ok": true, "detail": "Reachable at http://localhost:11434."},
  "schema_index": {"ok": true, "detail": "31 table(s) indexed."}
}
```

### `POST /ask`

Runs one question through the full agent graph — schema retrieval,
(for a question that reads as non-trivial — top-N-per-group, year-over-
year growth, several metrics at once) an up-front query plan, SQL
generation, plan-conformance review, validation, cost estimation,
execution, self-correction — and returns the outcome. Requires auth if
`API_AUTH_TOKEN` is set (see below). The planning/review steps are a
zero-cost pass-through for an ordinary question and add one or two extra
LLM round-trips (so extra latency) for a question they do trigger — see
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md#2-retry--self-correction-semantics).

Request:

```json
{
  "question": "What were total internet sales in 2012?",
  "conversation_history": [
    {"question": "prior question", "sql": "SELECT ...", "tables": ["FactInternetSales"], "status": "succeeded"}
  ],
  "enable_insight": true
}
```

`conversation_history` is optional and, unlike the UI (which reconstructs
it client-side from `frontend/src/store/chatStore.ts`'s `queryHistory`),
must be resent by the caller each request — the API has no server-side
session of its own. `enable_insight` defaults to `true`.

Response (mirrors what the React dashboard renders — see `agent.state.AgentState`):

```json
{
  "status": "succeeded",
  "sql": "SELECT SUM(SalesAmount) FROM FactInternetSales WHERE ...",
  "result_columns": ["TotalSales"],
  "result_rows": [[1234567.89]],
  "row_count": 1,
  "retry_count": 0,
  "max_retries": 3,
  "query_plan": null,
  "attempt_history": [{"attempt": 1, "sql": "...", "outcome": "succeeded", "error": null, "will_retry": false}],
  "insight": "Total internet sales in 2012 were $1,234,567.89.",
  "cost_notice": null,
  "rejection_reason": null,
  "rejection_message": null,
  "rate_limit_message": null,
  "clarification_message": null,
  "failure_explanation": null,
  "error_history": []
}
```

`status` is one of `AgentState`'s values (`succeeded`, `failed`,
`rejected`, `needs_clarification`, `rate_limited`, ...) — check it before
trusting `sql`/`result_rows`, exactly as the UI does. `query_plan` is
non-null only for a question `agent/complexity.py` judged non-trivial
(see `docs/ARCHITECTURE.md`) — the ordered plan steps the SQL above was
generated and checked against; `max_retries` is this question's actual
retry budget, which can exceed the configured `MAX_RETRIES` for that same
class of question.

Rate limiting: a per-client-IP question-submission limiter
(`Settings.question_rate_limit_per_minute`, mirroring the UI's
per-session limiter) returns `429` with a `Retry-After` header when
tripped. The stricter, process-wide LLM-*call* limiter
(`Settings.llm_call_rate_limit_per_minute`) applies automatically inside
the agent graph itself, same as it does for the UI.

### `GET /schema/tables`

Live table/column listing (metadata-only, no data queries) via the same
introspection the UI's sidebar schema browser uses. Requires auth if
`API_AUTH_TOKEN` is set.

```json
{"tables": [{"table_name": "DimCustomer", "columns": [{"name": "CustomerKey", "type": "INT", "nullable": false, "is_primary_key": true}, ...]}]}
```

### `POST /generate/confirm`

The human-approval confirmation step for media generation (`api/generation.py`)
— only meaningful when `ENABLE_MULTI_SOURCE_ROUTER` and
`ENABLE_MEDIA_GENERATION` are both on. A prior `/ask` response's
`generation_result.status == "pending_approval"` means the router picked
the "generation" source but nothing has been generated or charged yet
(`Settings.require_generation_approval`, default `true` — see
`SECURITY.md`'s "Media generation" section). This endpoint is what
actually calls the provider:

```json
// Request
{"question": "generate an image of monthly spend by category"}

// Response
{"answer": "Image generated successfully.", "status": "succeeded", "media_id": "a1b2c3...", "media_type": "image", "model": "seedream-4.5"}
```

`question` is typically the exact text from the proposal (possibly
hand-edited first, same as `/execute`'s SQL text can be) — the media kind
(image vs. video) is always re-inferred from it server-side, never
accepted from the caller. Fetch the actual bytes via `GET /media/{media_id}`.
Rate-limited per client IP (`API_ACTION_RATE_LIMIT_PER_MINUTE`) in addition
to the existing process-wide `MEDIA_GEN_RATE_LIMIT`. Requires auth if
`API_AUTH_TOKEN` is set.

### `POST /voice/transcribe`

Transcribes a recorded question to text (`api/voice.py`), locally via
`faster-whisper` — never routed through `POST /ask` itself, so the caller
is responsible for submitting the returned text through that endpoint
next (`agent.input_guard.check_input` applies there, identically to a
typed question). 404 when `ENABLE_VOICE_MODE` is off (on by default).
Multipart upload (`audio`, any format `faster-whisper`/`ffmpeg` can
decode), capped at `Settings.voice_max_upload_mb` (default 10MB) and
`Settings.voice_max_duration_seconds` (default 30s, checked before the
comparatively expensive transcription call). Rate-limited per client IP
(`API_ACTION_RATE_LIMIT_PER_MINUTE`).

```json
{"text": "what were total sales last quarter", "stt_duration_ms": 812.4}
```

### `POST /voice/synthesize`

Synthesizes text to speech (`api/voice.py`), locally via Piper — returns
raw `audio/wav` bytes, not JSON. 404 when `ENABLE_VOICE_MODE` is off.
`text` is capped at `Settings.max_question_length` (the same bound
`AskRequest.question` uses). Returns `503` if the configured Piper voice
model hasn't been downloaded yet (`python scripts/download_voice_model.py`
is a one-time setup step, separate from the app itself). Rate-limited per
client IP.

### `POST /search/media`

Direct media-library search (`api/media_search.py`), independent of the
conversational `/ask` flow — returns raw ranked hits with a templated
summary, not an LLM-composed answer (ask through `/ask` instead for that,
which routes through `agent.orchestrator.nodes.media_search_node`). 404
when `ENABLE_MEDIA_SEARCH` is off (off by default). Rate-limited per
client IP, sharing `API_ACTION_RATE_LIMIT_PER_MINUTE` with other
lightweight actions rather than a dedicated limiter.

```json
// Request
{"query": "the photo of the site inspection", "media_type": null}

// Response
{
  "answer": "Found 2 result(s).",
  "status": "succeeded",
  "hits": [
    {"media_id": "a1b2...", "media_type": "image", "caption": "...", "timestamp_start": null, "timestamp_end": null}
  ]
}
```

`media_type` is optional (`"image"`, `"video"`, or omitted/`null` for
both). A video hit's `timestamp_start`/`timestamp_end` mark the matched
scene-detected segment; fetch its representative keyframe (or an image
hit's original file) via `GET /media/library/{media_id}` below — a full
video clip is never streamed.

### `GET /media/library/{media_id}`

Streams one media-library asset's bytes (`api/media_library.py`) — the
original file for an image hit, or the representative keyframe thumbnail
for a video-segment hit. Distinct from `GET /media/{media_id}`
(`api/media.py`), which serves freshly *generated* media from an
in-memory, 100-entry cache — this route instead resolves `media_id`
against the persistent Chroma-backed media library and re-validates the
resolved path stays inside the configured library root before opening it.
404 when media search is disabled, `media_id` is unknown, or the
underlying file has moved/been deleted.

### Other routes

`POST /execute` (SQL "Confirm and Run" equivalent), `POST /schema/refresh`,
`GET`/`POST`/`DELETE /documents`, `GET /documents/{id}/download`, and
`GET /media/{media_id}` (serving freshly *generated* media — see
`GET /media/library/{media_id}` above for the persistent-library
equivalent) also exist (`api/main.py`, `api/documents.py`, `api/media.py`)
— not yet given their own subsection here; see each module's own
docstrings for the authoritative contract in the meantime.

`GET /metrics/performance` (`Permission.ADMIN_CONFIG`, same gate as
`POST /schema/refresh`) returns a live rollup of per-LangGraph-stage
timing across recent `/ask` requests — `observability.metrics`, see
`CLAUDE.md`'s "Observability — live performance rollup" section for the
full design and its single-process/resets-on-restart limits.

**Local-account auth** (`api/identity_auth.py`, only reachable when
`LOCAL_AUTH_ENABLED=true` — 404s otherwise, not 503, see that router's own
docstring): `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`,
`POST /auth/logout`(`-all`), `GET`/`PATCH /auth/me`, `POST
/auth/change-password`, `POST /auth/forgot-password`, `POST
/auth/reset-password`, `POST /auth/verify-email`, `POST
/auth/resend-verification`, `GET /auth/sessions`, `DELETE
/auth/sessions/{id}`. See `docs/AUTHENTICATION.md` and
`docs/authentication-and-password-policy.md`.

**Server-side chat history** (`api/chat_history.py`, same
`LOCAL_AUTH_ENABLED` gate): `GET`/`POST /conversations`, `GET`/`PATCH`/
`DELETE /conversations/{id}`, `GET`/`POST /conversations/{id}/messages`,
`GET /chat/search?q=`. Every route requires a local account and is
ownership-scoped to the caller — see `docs/chat-history-architecture.md`
and `docs/chat-history-search.md`.

## Auth: from a lightweight hook to real per-user identity, depending on configuration

`Settings.auth_mode` resolves to one of four values, in priority order —
see `api/auth.py::verify_api_key` and `docs/AUTHENTICATION.md` for the
full picture:

1. **`local`** (`LOCAL_AUTH_ENABLED=true`) — this app's own accounts
   (`identity/`): real login/registration, per-user sessions (rotating
   refresh tokens), and RBAC roles that feed into the same permission
   checks every route below already enforces. **This is real per-user
   identity and per-user data isolation** — chat history
   (`api/chat_history.py`) is strictly scoped to the authenticated
   caller's own `user_id`, never trusting a client-supplied value (see
   `docs/chat-history-architecture.md`).
2. **`oidc`** (`OIDC_ISSUER` set) — validates a JWT against any
   standard-compliant external identity provider. See
   `docs/AUTHENTICATION.md`.
3. **`static_token`** (`API_AUTH_TOKEN` set, `local`/`oidc` both unset) —
   the original lightweight hook: one shared bearer secret, checked with a
   constant-time comparison, granting a fixed admin-equivalent identity
   with **no per-user identity, no session, no chat-history persistence**
   (there's no stable per-caller `user_id` to attach a conversation to).
4. **`none`** (nothing configured, the default) — no auth at all,
   suitable for local/trusted-network use only.

`GET /health` never requires auth regardless of mode (health checks
typically need to be reachable by an orchestrator with no credential).

**For anything beyond local/trusted-network use, either configure `local`
or `oidc` auth, or put the API behind a real authenticating reverse proxy**
(e.g. `oauth2-proxy`, your platform's managed auth) — see
`docs/DEPLOYMENT.md`. Modes 3 and 4 above are still appropriate only for a
single-operator/trusted-network deployment, per `SECURITY.md`'s posture;
mode 1 or 2 is what a genuinely multi-user deployment should configure.

## Correlation IDs

Every request is bound to a correlation ID (from an incoming
`X-Correlation-ID` header, or a generated UUID) for the duration of the
request, echoed back in the response's `X-Correlation-ID` header. Every
`security.audit_log.log_security_event` call made during that request
(input rejections, validator safety violations, rate-limit trips,
sensitive-column blocks) includes it automatically — so a caller can hand
you a correlation ID and you can grep the audit log for exactly what
happened on that request, without this app needing per-user identity to do
it. See `security/audit_log.py`.

## What this is not

- Not a multi-tenant API in the "isolated customer workspaces" sense —
  every authenticated user shares the same configured business
  database(s) and schema/business-context retrieval index. Per-user
  identity, session management, RBAC, and per-user chat-history isolation
  *do* exist when `local`/`oidc` auth is configured (see above) — this
  project outgrew the "no per-user identity at all" characterization this
  section used to have as more of `identity/`/`agent/authz.py` was built;
  what's still true is that it has no concept of separate tenants/
  organizations sharing one deployment.
- Not a stable, versioned public API contract — it's young and scoped to
  this project's own needs; expect it to evolve alongside the agent.
- Not a replacement for reading `SECURITY.md` before pointing either
  interface (UI or API) at a real/sensitive database.
