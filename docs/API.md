# API

A minimal REST API (`api/`, FastAPI) over the same LangGraph agent the
Streamlit UI drives. **Not a replacement for `ui/app.py`** — the UI remains
the primary, human-facing surface (SQL review, retry timeline, schema
browser, charts). This exists for programmatic/scripted access and as the
interface `docs/DEPLOYMENT.md`'s reverse-proxy/container guidance sits in
front of.

## Why this is safe: no second implementation

`POST /ask` calls `agent.graph.run_agent` directly — the exact function
`ui/app.py` calls. Every safety layer described in `SECURITY.md` (input
guard, SQL validator's SELECT-only allowlist, row cap, query timeout, the
process-wide LLM-call rate limiter, sensitive-column blocking) governs this
endpoint identically, because it's the same graph execution, not a
parallel code path that could drift out of sync or be weaker. `GET
/schema/tables` similarly reuses `db.schema_introspection.introspect_schema`
— the same metadata-only introspection the UI's schema browser and
`scripts/build_embeddings.py` use.

## Running it

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Or via Docker Compose — see `docs/DEPLOYMENT.md` (`docker compose up api`).
It reads the same `.env` as the Streamlit UI (same `config/settings.py`,
same database/Ollama/Chroma configuration) — there is nothing API-specific
to configure beyond the optional `API_AUTH_TOKEN` described below.

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

Runs one question through the full agent graph — schema retrieval, SQL
generation, validation, cost estimation, execution, self-correction — and
returns the outcome. Requires auth if `API_AUTH_TOKEN` is set (see below).

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
it server-side from `ui/session_history.py`'s session state), must be
resent by the caller each request — the API has no server-side session of
its own. `enable_insight` defaults to `true`.

Response (mirrors what `ui/app.py` renders — see `agent.state.AgentState`):

```json
{
  "status": "succeeded",
  "sql": "SELECT SUM(SalesAmount) FROM FactInternetSales WHERE ...",
  "result_columns": ["TotalSales"],
  "result_rows": [[1234567.89]],
  "row_count": 1,
  "retry_count": 0,
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
trusting `sql`/`result_rows`, exactly as the UI does.

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

## Auth: a lightweight hook, not a full auth system

`API_AUTH_TOKEN` (`.env`, unset by default) is an optional shared bearer
token: when set, `/ask` and `/schema/tables` require a matching
`Authorization: Bearer <token>` header (checked with a constant-time
comparison — see `api/auth.py`); `/health` never requires it. This is
**one shared secret, not per-user identity** — there is no login, no
token issuance, no session, no authorization model beyond "has the
token or doesn't." It exists so this isn't wide open by default the
moment it's reachable from more than localhost, not as a substitute for
real auth.

**For anything beyond local/trusted-network use, put this behind a real
authenticating reverse proxy** (e.g. `oauth2-proxy`, your platform's
managed auth) regardless of whether `API_AUTH_TOKEN` is set — see
`docs/DEPLOYMENT.md`. This mirrors the same posture `SECURITY.md` already
states for the Streamlit UI: this project is not designed for multi-user
authorization, and adding a shared token doesn't change that.

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

- Not a multi-tenant API — no per-user identity, no per-user data
  isolation, no authorization model beyond the single shared token above.
- Not a stable, versioned public API contract — it's young and scoped to
  this project's own needs; expect it to evolve alongside the agent.
- Not a replacement for reading `SECURITY.md` before pointing either
  interface (UI or API) at a real/sensitive database.
