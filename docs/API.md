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

Real, non-cached reachability check of every external dependency: every
configured database (`db.connection.test_connection`) plus its schema
index, and Ollama (a cheap `list()` call — no generation). Returns HTTP
`200` with `{"status": "ok", ...}` when everything is reachable, `503` with
`{"status": "degraded", ...}` otherwise, so container/orchestrator
health-check tooling that checks the status code works correctly. Never
requires auth (a health check consumed by infrastructure tooling, not a
data-exposing endpoint).

Also reports live chat-image-vision diagnostics
(`vision_enabled`/`vision_model`/`vision_model_available`/`ocr_enabled`) —
`vision_model_available` is a real, live lookup against Ollama's own
pulled-model list (reusing the same `list()` call above, not a second
round-trip), so a configured-but-never-pulled model name is caught here
rather than only discovered the first time a user attaches an image.

```json
{
  "status": "ok",
  "databases": [
    {
      "name": "default",
      "connection": {"ok": true, "detail": "Connection successful."},
      "schema_index": {"ok": true, "detail": "31 table(s) indexed."}
    }
  ],
  "ollama": {"ok": true, "detail": "Reachable at http://localhost:11434."},
  "voice_enabled": true,
  "media_search_enabled": false,
  "local_auth_enabled": false,
  "google_signin_enabled": false,
  "google_client_id": null,
  "vision_enabled": true,
  "vision_provider": "ollama",
  "vision_model": "llava",
  "vision_model_available": true,
  "ocr_enabled": true
}
```

`google_signin_enabled`/`google_client_id` reflect whether "Continue with
Google" is available on this deployment (`GOOGLE_OAUTH_CLIENT_ID` set *and*
`LOCAL_AUTH_ENABLED=true`) — `google_client_id` is the one deliberately
public value served this way rather than baked into the frontend build, so
it can be rotated with a process restart alone. See
`docs/AUTHENTICATION.md`'s "Google sign-in" section.

### `GET /live`

Pure liveness — `{"status": "alive"}`, always, with no dependency check at
all (never touches the database, Chroma, or Ollama). Added alongside
`/health` above (2026-09-27) so a container orchestrator's liveness probe
and readiness probe can point at different things: a slow/degraded
dependency should never cause a healthy process to be killed and
restarted (liveness → `/live`), while a process that genuinely can't reach
its database shouldn't receive new traffic (readiness → `/health`). Never
requires auth, same reasoning as `/health`.

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
  "enable_insight": true,
  "model": "qwen2.5:7b"
}
```

`model` is optional (from a prior `GET /models` response's `models[].id`) —
omit it to use the server-configured default (`Settings.ollama_model`);
every existing caller from before this field existed keeps working
unchanged. If provided, it's validated against `Settings.ollama_allowed_models`
*before* any LLM/DB work starts — an unrecognized or disallowed value
returns `400 Bad Request` naming the model, never a silent fallback or a
raw string passed through to Ollama unchecked. See
[`docs/CONFIGURATION.md`](CONFIGURATION.md#model-selection) for the full
online-catalog/configured/installed/selectable distinction and how to add
or remove a model.

`conversation_history` is optional and, unlike the UI (which reconstructs
it client-side from `frontend/src/store/chatStore.ts`'s `queryHistory`),
must be resent by the caller each request — the API has no server-side
session of its own. `enable_insight` defaults to `true`.

Response (mirrors what the React dashboard renders — see `agent.state.AgentState`):

```json
{
  "status": "succeeded",
  "model": "qwen2.5:7b",
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
class of question. `model` echoes back whichever model actually answered
this question (the request's own `model`, once validated, or the server
default).

Rate limiting: a per-client-IP question-submission limiter
(`Settings.question_rate_limit_per_minute`, mirroring the UI's
per-session limiter) returns `429` with a `Retry-After` header when
tripped. The stricter, process-wide LLM-*call* limiter
(`Settings.llm_call_rate_limit_per_minute`) applies automatically inside
the agent graph itself, same as it does for the UI.

`attachment_ids` (optional) references file(s) already uploaded via
`POST /attachments/upload` below — see "Attachments" for the full upload
flow. Attaching a file is never a reason SQL gets generated: an
attachment-only question ("extract text from this image," "summarize this
PDF") is deterministically routed away from schema retrieval/SQL
generation entirely (`agent/orchestrator/nodes.py::_looks_like_attachment_only_question`),
regardless of how many databases are configured. A question that
genuinely names a database keyword alongside an attachment ("use this
file to query the database") still reaches the normal SQL path, with the
attachment folded in as grounding context, never as privileged
instructions — the existing read-only SQL validator and schema
authorization are unchanged either way.

### Attachments

`POST /attachments/upload` (`api/attachments.py`) — multipart upload.
Validates (extension + magic-byte signature, size limits), malware-scans
(if `MALWARE_SCAN_PROVIDER` is configured), stores under a generated id,
and eagerly processes each file independently — one bad file in a batch
never fails the others. Requires `Permission.ASK` (the same permission
asking a question needs).

```json
// Response
{
  "attachments": [
    {"attachment_id": "att_...", "filename": "invoice.png", "media_type": "image/png", "size_bytes": 48213, "processing_status": "succeeded", "processing_error": null}
  ],
  "errors": []
}
```

`attachment_id` is what gets passed as `POST /ask`'s `attachment_ids`.
`DELETE /attachments/{id}` removes it (ownership-scoped — a wrong or
another caller's id resolves to a `404`, never confirming it exists).

`GET /attachments/capabilities` — live, per-deployment capability report
(no request body). Reflects actual configuration, not a static claim:

```json
{
  "enabled": true,
  "vision_input": true,
  "vision_model": "llava",
  "ocr": true,
  "image_resize": true,
  "image_blur": true,
  "image_text_removal": true,
  "image_text_removal_method": "opencv_telea_inpaint",
  "image_ai_editing": false,
  "image_ai_editing_provider": null,
  "native_pdf_input": false,
  "max_image_bytes": 10485760,
  "max_document_bytes": 26214400,
  "max_attachments_per_message": 5,
  "max_total_attachment_bytes": 52428800,
  "max_resize_dimension_px": 4096,
  "max_text_removal_regions": 20,
  "max_ai_edit_prompt_length": 500,
  "supported_image_extensions": [".gif", ".jpeg", ".jpg", ".png", ".webp"],
  "supported_document_extensions": [".csv", ".docx", ".json", ".md", ".pdf", ".pptx", ".txt", ".xlsx"],
  "resize_presets": [{"name": "medium_800", "width": 800, "height": 800}]
}
```

`ocr`/`image_text_removal` reflect whether the `pytesseract` *package* is
importable, not whether the system Tesseract *binary* is installed — a
missing binary degrades one OCR/remove-text request to an honest warning
at call time, it doesn't flip this flag.

**Explicit image actions** (image attachments only — each returns `404`
for a document attachment or an id the caller doesn't own):

| Route | What it does |
|---|---|
| `POST /attachments/{id}/extract-text` | Real Tesseract OCR — returns `raw_text`/`cleaned_text` (both exact recognized text, never a model paraphrase) plus per-word bounding boxes/confidence. Distinct from asking a natural-language vision question via `/ask`. |
| `POST /attachments/{id}/resize` | Deterministic Pillow resize — `{"width": 800, "height": null, "fit": "contain", "output_format": null}`. No model call. Always returns a brand-new attachment (the original is never mutated). |
| `GET /attachments/{id}/detect-text-regions` | OCR-proposed text-line regions, for the "Remove text" workflow's confirm/adjust step. Returns `[]` (not an error) if OCR is unavailable or finds nothing — the caller falls back to manual region selection. |
| `POST /attachments/{id}/remove-text` | Real pixel editing via classical OpenCV inpainting (Telea's algorithm) — `{"regions": [{"left": 10, "top": 10, "width": 80, "height": 20}]}`, either OCR-proposed (caller-confirmed) or manually drawn. **Not** generative AI and never a solid-color rectangle; the response's `warnings` field says so explicitly. Always returns a brand-new attachment. |
| `POST /attachments/{id}/blur-region` | Deterministic, local Pillow Gaussian blur over a painted mask — `{"mask_data_url": "data:image/png;base64,...", "radius": 18}`. No model call; works even when AI-guided editing (`image_ai_editing`) is off. Always returns a brand-new attachment. |
| `POST /attachments/{id}/ai-edit` | **Real, generative** AI-guided editing (IMA Studio `image_to_image`) — `{"operation": "remove_object", "prompt": "Remove the selected object.", "mask_data_url": "data:image/png;base64,..."}` (`operation` is one of `remove_object`/`replace_background`/`replace_sky`/`region_edit`/`enhance`; `mask_data_url` is optional). Returns `404`-shaped `{"status": "failed", "error_code": "not_configured", ...}` when `image_ai_editing` is `false` — never a raw exception. IMA has no native mask channel, so a provided mask is conveyed as a translucent overlay + text instruction, disclosed via the response's `warnings`, never presented as pixel-exact. Shares this app's existing media-generation rate limit and a per-caller idempotency key (`idempotency_key`, optional) so a duplicate click never pays for a second generation. See [`docs/image-editing-architecture.md`](image-editing-architecture.md) for the full design. |

The middle three (`remove-text`/`blur-region`/`ai-edit`'s success case)
share the `ImageEditResultResponse`-shaped fields (`attachment_id`,
`source_attachment_id`, `operation`, `image_data_url`, `media_type`,
`width`, `height`, `size_bytes`, `warnings`) — `ai-edit`'s own response
additionally carries `status`, `mask_provided`, `provider`, `model`,
`error_code`, `error_message` since an edit can fail in ways local
resize/remove-text/blur cannot (no credentials, content-policy rejection,
provider timeout).

### `GET /models`

The Ollama Text-to-SQL model registry — every model
`Settings.ollama_allowed_models` configures, enriched with a live
"is it actually installed on the connected Ollama instance right now"
check. Same `Permission.ASK` gate as everything else asking-adjacent.
Never calls the online Ollama Library — see
[`docs/CONFIGURATION.md`](CONFIGURATION.md#model-selection).

```json
{
  "provider": "ollama",
  "default_model": "llama3.1:8b",
  "selection_enabled": true,
  "models": [
    {
      "id": "llama3.1:8b",
      "display_name": "Llama 3.1 8B",
      "is_default": true,
      "enabled": true,
      "installed": true,
      "available": true,
      "recommended": true,
      "parameter_size": "8B",
      "context_length": 128000,
      "resource_level": "medium",
      "capabilities": ["text", "sql", "reasoning", "tools"],
      "description": "This application's own default model. ..."
    },
    {
      "id": "qwen2.5:7b",
      "display_name": "Qwen 2.5 7B",
      "is_default": false,
      "enabled": true,
      "installed": false,
      "available": false,
      "recommended": true,
      "parameter_size": "7B",
      "context_length": 32000,
      "resource_level": "medium",
      "capabilities": ["text", "sql", "reasoning", "tools"],
      "description": "..."
    }
  ]
}
```

A model that's configured but not yet `ollama pull`ed on the server's own
machine still appears here (`installed: false`) rather than being hidden —
the frontend disables selecting it and shows "Not installed" instead. Never
exposes `OLLAMA_HOST`, secrets, or any other environment/connection detail.
Cache this response client-side (the React dashboard does, 30 seconds,
matching `GET /health`) — this endpoint is never called from `/ask` itself.

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

**Google sign-in** (same router, additionally requires `GOOGLE_OAUTH_CLIENT_ID`
set — 404s otherwise): `GET /auth/google/nonce` (issues a single-use
sign-in nonce), `POST /auth/google` (verifies a Google ID token, signs in
or signs up), `GET`/`POST`/`DELETE /auth/google/link` (view/link/unlink a
Google identity on the caller's *own* authenticated account). See
`docs/AUTHENTICATION.md`'s "Google sign-in" section for the full token
-verification contract and identity-linking rules.

**Server-side chat history** (`api/chat_history.py`, same
`LOCAL_AUTH_ENABLED` gate): `GET`/`POST /conversations`, `GET`/`PATCH`/
`DELETE /conversations/{id}`, `GET`/`POST /conversations/{id}/messages`,
`GET /chat/search?q=`. Every route requires a local account and is
ownership-scoped to the caller — see `docs/chat-history-architecture.md`
and `docs/chat-history-search.md`.

**Secure conversation sharing** (`api/shares.py`, requires
`LOCAL_AUTH_ENABLED=true` and `ENABLE_CONVERSATION_SHARING=true`, the
latter on by default): owner-only management —
`POST`/`GET`/`PATCH /conversations/{id}/share`, `POST
/conversations/{id}/share/revoke`, `POST
/conversations/{id}/share/link/regenerate`, `POST
/conversations/{id}/share/members/invite`, `DELETE
/conversations/{id}/share/members/{memberId}` — plus a viewer surface
reachable **without authentication** for an "anyone with the link" share
(`SHARE_PUBLIC_LINKS_ENABLED`, off by default): `GET /share-view/{ref}`,
`GET /share-view/{ref}/attachments/{attachmentId}`, and `POST
/share-invitations/{token}/accept` (redeems an invitation for the caller's
own authenticated account). A shared conversation is always a read-only,
server-filtered snapshot — never a live query channel, and never a way to
reach `/ask`/`/execute` or any other AI/SQL route. See
`docs/SHARING_SECURITY.md` for the full RBAC/ABAC model, token lifecycle,
and cache/referrer protections.

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
   `docs/chat-history-architecture.md`). "Continue with Google"
   (`GOOGLE_OAUTH_CLIENT_ID` set) is a second way to *reach* this same
   mode — it produces the identical session a password login does, never
   a separate `auth_mode` value of its own.
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
