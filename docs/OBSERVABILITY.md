# Observability

Phase 3 Step 4: AUDIT → COMPARE → GAP ANALYSIS → IMPLEMENT ONLY WHAT WAS MISSING. This
document describes what this application actually emits today (logs, correlation IDs,
per-stage timing, health checks) verified against the running code, not aspirational.
**No commercial/hosted monitoring dependency exists or is proposed** (no Datadog, New
Relic, Prometheus/Grafana stack) — consistent with this project's "local-first,
single-user/local-dev-oriented" identity (`README.md`'s own Limitations section);
everything below is either the Python standard library's `logging` module or a
standalone script that reads its output.

## 1. Correlation ID propagation

**API layer.** `api/main.py::_correlation_id_middleware` binds one correlation ID per
HTTP request — the caller's own `X-Correlation-ID` header if supplied, else a fresh
`uuid4()` — into a `contextvars.ContextVar` (`security/audit_log.py`), and echoes it
back on every response's `X-Correlation-ID` header, success or failure. `contextvars`
(not `threading.local`) is required specifically because Starlette dispatches a sync
endpoint to a worker thread via `anyio.to_thread.run_sync`, which copies the calling
context into that thread; a thread-local would not see the value the middleware set in
the event loop's own async context.

**Into every log line, not just audit events (Phase 3 fix).** Before this pass,
the correlation ID was readable by exactly one thing: `security.audit_log
.log_security_event`, which explicitly reads the contextvar. Every *ordinary*
`logging.getLogger(__name__)` call anywhere else — `agent/nodes.py`'s eleven node
functions, `agent/orchestrator/nodes.py`, `db/`, `rag/`, `media_gen/`, `search/` — used
a plain per-module logger with no correlation ID at all. That made it impossible to
grep one live request's full trace across the API → LangGraph → RAG/SQL/external-call
boundary from ordinary application logs, only from the narrower `security.audit`
stream — a real, named gap, not a hypothetical one.

Fixed with one small, additive piece: `security.audit_log.CorrelationIdLogFilter`, a
`logging.Filter` that stamps `record.correlation_id` (the current contextvar value, or
`"-"` outside a request) onto every record. `config.settings.configure_logging`
attaches it to every handler on the root logger and adds `correlation_id=%(correlation_id)s`
to the format string. Because `logging.Filter` runs before formatting on every record
that reaches a handler, **every existing call site in every module gets this for free**
— zero changes needed to `agent/nodes.py`, `rag/`, `db/`, etc. Verified by
`tests/test_audit_log.py::TestCorrelationIdLogFilter` (including an end-to-end test
against a plain, non-`security.audit` module logger).

**LangGraph → RAG/SQL/external calls.** Once attached at the root logger, this covers
every stage a question passes through in one process, since LangGraph nodes,
`db/execution.py`, `rag/graph.py`, `media_gen/`, and `search/web_search.py` all log via
plain module-level `logging.getLogger(__name__)` calls that flow to the same root
handler — none of them run in a separate process or a detached thread that would lose
the contextvar. The one exception: a background `ThreadPoolExecutor` worker (e.g.
`scripts/build_media_index.py`'s `MEDIA_INGEST_WORKERS`) does **not** inherit the
submitting thread's contextvar by default (unlike `anyio.to_thread.run_sync`, plain
`concurrent.futures.ThreadPoolExecutor.submit` does not copy `contextvars.copy_context()`
automatically) — logs from that pool correctly show `correlation_id=-`, since batch
ingestion isn't a per-request operation in the first place and has no single request to
attribute to.

**What already existed and was correct, not touched:** the middleware itself, the
response header, and `log_security_event`'s own correlation-ID inclusion — Phase 2
built all of that already; this pass only extended the *same* mechanism to ordinary
logs instead of duplicating a second one.

## 2. Structured logging

**Security/audit events** (`security/audit_log.py::log_security_event`) are the one
genuinely structured stream: a stable `event_type`, a `severity` (`info`/`warning`/
`critical`), a human `detail`, and arbitrary `**context` key=value pairs, rendered as
one line: `event=<type> severity=<level> detail='...' correlation_id=<id> key=value ...`.
The full event taxonomy actually emitted today (verified by grep, not aspirational):

| event_type | Where | Meaning |
|---|---|---|
| `input_rejected` | `agent/nodes.py` | `sanitize_input_node` blocked a question (length/injection pattern) |
| `conversation_history_injection_detected` | `agent/input_guard.py` | A prior-turn history entry looked like an injection attempt (logged, not rejected) |
| `possible_rag_poisoning` | `agent/nodes.py` | Retrieved schema/golden-example content looked adversarial |
| `rate_limit_tripped` | `agent/nodes.py` | Per-session LLM-call rate limit hit |
| `sql_safety_violation` | `agent/nodes.py` | `sql_validator` rejected generated/edited SQL |
| `sensitive_column_blocked` | `agent/nodes.py` | A restricted column was referenced without `VIEW_RESTRICTED_COLUMNS` |
| `auth_failed` | `api/auth.py` | Any authentication rejection (missing/invalid/expired credential) |
| `oidc_token_rejected` | `security/oidc.py` | JWT-specific rejection reason (bad signature, issuer, audience, expiry) |
| `authz_denied` | `api/authz.py`, `api/documents.py` | A permission check failed (RBAC or sensitive-document gate) |
| `session_expensive_source_limit_tripped` | `agent/orchestrator/nodes.py` | Per-session cost ceiling hit (generation/web/policy) |
| `orchestrator_source_denied` | `agent/orchestrator/nodes.py` | Router picked a source the caller's role can't use |
| `generation_routed` / `_rejected` / `_rate_limited` / `_provider_failed` / `_download_failed` / `_succeeded` / `_pending_approval` | `agent/orchestrator/nodes.py` | Media generation lifecycle events |
| `content_moderation_rejected` / `_soft_flagged` | `moderation/gate.py` | Ingestion-time moderation outcomes |

This list is exactly what `scripts/monitoring_summary.py --log-file <captured output>`
already parses and tallies (`_AUDIT_EVENT_RE`) — the no-commercial-dependency
"monitoring" story for this app: redirect stdout/stderr to a file (or capture a
container's logs) and run that script.

**Ordinary application logs** (everything else — `[timing] stage=... duration_ms=...`,
`[retrieve_schema] question=...`, `[generate_sql] attempt N: generated SQL: ...`, etc.)
are prose, not key=value pairs, though each carries a consistent `[node_name]` prefix
making them greppable. **Named limitation, not fixed in this pass**: these are not
JSON and a log aggregator (Splunk, ELK, Loki) would need a grok/regex pattern, not
out-of-the-box JSON parsing, to structure them. Migrating every `logger.info`/`.warning`
call site across the codebase to a JSON formatter would be a sweeping, high-blast-radius
change (every one of ~150 call sites, every existing log-scraping test assertion in
`tests/`) for a single-user/local-dev-oriented app that has no log aggregator deployed
today — out of scope for this pass per "no large rewrites unless absolutely necessary."
If this app is ever deployed somewhere a log pipeline expects JSON, wrapping
`logging.Formatter` with a JSON-emitting one in `configure_logging` (the fields are
already there on each `LogRecord` — level, name, message, and now `correlation_id`) is
a small, contained follow-up, not a rewrite.

## 3. Per-stage latency instrumentation (already existed, verified working)

`agent/nodes.py::_timed_node` wraps every one of the graph's eleven nodes, timing each
call (including every retry attempt) and both (a) logging `[timing] stage=<name>
attempt=<n> duration_ms=<ms>` at INFO, and (b) appending a `StageTiming` entry to
`AgentState["stage_timings"]`, which accumulates into `stage_timings_ms` in the
benchmark's own result records. This is what `docs/PERFORMANCE_BASELINE.md`'s entire
per-stage breakdown was built from — real, already-wired instrumentation, not something
this pass had to add. Combined with fix #1 above, every `[timing]` log line now also
carries the request's `correlation_id`, so an operator can now reconstruct one live
request's full per-stage latency breakdown directly from logs (`grep correlation_id=<id>
... | grep '\[timing\]'`) — previously only possible by re-running the benchmark
harness, which captures the same data into a JSON file instead of relying on log
scraping.

`observability/llm_timing_capture.py::capture_llm_timings` separately captures
Ollama's own reported `total_ms`/`load_ms`/`prompt_eval_ms`/`generation_ms` and token
counts by attaching a `logging.Handler` to `agent.llm_client`'s logger for the scope of
one question — this is what distinguishes model-load time from prompt-processing time
from generation time (wall-clock alone can't, per that module's own docstring), used by
both `eval/runner.py`'s benchmark and available to any future API-layer caller that
wants the same breakdown for a live request.

## 4. What's never logged

**Secrets.** `security/redaction.py::redact_secrets` scrubs a configured DB password
(exact-value match) and any connection-string-shaped `password=`/`pwd=`/`://user:pass@`
text (regex fallback) out of any driver/Chroma exception text before it reaches a log
or an API response — applied at every site that surfaces raw third-party exception text
(`db/connection.py`'s connection-test failures, `agent/nodes.py::execute_sql_node`,
`embeddings/retriever.py`, and `GET /health`, the one unauthenticated endpoint, where a
leak would be public rather than merely internal). `.env`/API tokens/OIDC secrets are
`pydantic.SecretStr` (`security/secrets.py`) — `str()`/`repr()`/`%r`-formatting all
render `**********`; a call site needs `.get_secret_value()` explicitly to get the
real value, so an accidental `logger.info("token=%s", settings.api_auth_token)` is
already safe by construction, not by discipline alone.

**Query result content.** `observability/redaction.py::summarize_result_for_log` turns
an executed query's result into row/column *counts* only, never cell values — this is
the enforcement mechanism behind `CLAUDE.md`'s coding-standard promise ("Never log ...
full result rows"). `Settings.log_redaction_level` (`"standard"` default / `"strict"`)
additionally controls whether *column names* are included, since a column name itself
can be sensitive in some schemas (`ssn`, `salary`, `date_of_birth`) even with no row
data logged — `"strict"` drops those too, leaving only counts.

**Named limitation, disclosed rather than hidden: free-text question/SQL/insight
content is logged in full at INFO**, unredacted (`agent/nodes.py`'s
`[retrieve_schema] question=%r`, `[generate_sql] ... generated SQL: %s`,
`[generate_insight] generated: %r`, and similar). This is intentional for the stated
debuggability goal (`CLAUDE.md`: "the terminal shows the agent's reasoning steps
live") and none of it is a *credential* — but nothing prevents a user from typing
incidental PII into a free-text question (an email address, a name), and that text
will appear in plaintext logs exactly as typed. No PII-detection/scrubbing exists for
this, and building a reliable one is out of scope for this pass (a regex-based PII
filter would be unreliable enough to give false confidence, and a real NLP-based one is
a separate, much larger project). This is a real, standing tradeoff — teams deploying
this somewhere query text may contain regulated PII should either lower `LOG_LEVEL`
above `INFO` for `agent.nodes`/`agent.orchestrator.nodes` in that environment, or treat
the log destination itself as within the same compliance boundary as the database being
queried.

## 5. Health / readiness

`GET /health` (unauthenticated, by design — needs to be reachable by an orchestrator
with no API key) performs a real, non-cached reachability check of every configured
database (`test_connection` — a live `SELECT`-equivalent round trip) plus its Chroma
schema-index population, and Ollama (`.list()`), returning HTTP 200 only when every
component is reachable, 503 (`status: "degraded"`) otherwise — verified already correct
in Phase 1/2, unchanged this pass. Any leaked exception text in a component's `detail`
field is redacted (see §4) since this is the one endpoint where that leak would be
public.

## 6. Known gaps, named rather than silently left

- **No metrics/tracing backend** (Prometheus counters/histograms, OpenTelemetry spans).
  Deliberate, not an oversight — this app has no commercial monitoring dependency by
  design, and `_timed_node`'s log-based per-stage timing (§3) plus the benchmark
  harness already answer "where does latency go" without one. If this is ever deployed
  at a scale where dashboards/alerting on live traffic (not just periodic benchmark
  runs) are needed, that's the point to introduce OpenTelemetry — a real, larger
  addition, not attempted here.
- **Ordinary logs are prose/key=value, not JSON** (§2) — a log-pipeline integration
  gap, not a data-loss one; the data is all present and greppable today.
- **Free-text question/SQL/insight content is unredacted in logs** (§4) — a disclosed,
  accepted tradeoff for debuggability, not a defect.
- **The `security.audit` log stream is not tamper-evident** — already flagged as a
  Phase 2 P1 item (`docs/PHASE2_FINAL_REPORT.md`); unchanged by this pass, since making
  an append-only/signed audit trail is a security-architecture decision (a dedicated
  write-once store, or forwarding to an external SIEM) rather than an observability
  fix, and remains open for a future pass.
- **uvicorn's own access log (method/path/status/duration per HTTP request) does not
  carry this app's `correlation_id`** — it's emitted by uvicorn's own logger before
  this app's middleware runs, a separate log line from this app's own request-scoped
  logs. Not fixed here: correlating the two requires either a custom ASGI access-log
  middleware (replacing uvicorn's default, a real behavior change) or accepting the two
  streams as separately greppable (by timestamp/path), which is what this app does today.
