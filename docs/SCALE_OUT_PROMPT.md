# Scale-Out, Multi-Tenant, Production Hardening Program

> Committed verbatim (per its own instruction #0) so every future session
> can re-read the full 11-phase program without it being re-pasted. This is
> the source-of-truth prompt driving `docs/SCALE_BASELINE.md`,
> `docs/CAPACITY_PLANNING.md`, `eval/load/`, and every phase's own PR.
> **Status tracker**: Phase 0 — done (see `docs/SCALE_BASELINE.md`). Phases
> 1-10 — not started. Re-read this file at the start of every new phase's
> session before planning it.

---

> **How to use this:** open this codebase in your IDE's AI agent and paste everything below the line.
> It is written as a **phased program**, not a single task. Run one phase per session
> (or per PR). Each phase ends with a hard verification gate — do not start the next
> phase until the gate passes. Commit this file to `docs/SCALE_OUT_PROMPT.md` so every
> session can re-read it.

---

## ROLE

You are a principal engineer taking this Text-to-SQL LangGraph platform from a
**single-instance, single-user-oriented** application to a **horizontally scalable,
multi-tenant, production AI service** — the way real hosted AI products (ChatGPT-,
Claude-, Perplexity-style services) are built: stateless API tier, queued/streamed
inference, a separate model-serving tier, distributed rate limiting and quotas,
tenant isolation, and full observability.

Before writing any code, read `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md`,
`docs/PRODUCTION_READINESS_REPORT.md`, `docs/PERFORMANCE_BASELINE.md`, and `SECURITY.md`.
Follow this repo's existing conventions: pinned dependencies with a comment explaining
each, pydantic-settings for all config, docstrings that explain *why*, idempotent
migrations, and a test for every behavior change. Do not remove any existing security
control (SQL validator, input guard, moderation gate, sensitive-column blocking,
read-only enforcement, prompt-injection defences). Every one of them must keep working
identically after each phase — the existing test suite and
`eval/security_benchmark` must stay green.

## THE HONEST TARGET

"Billions of simultaneous users" is not a real engineering target — no AI service runs
that concurrently, and LLM inference cost, not code, is the binding constraint. The real
goal is an architecture where **capacity scales linearly by adding nodes and regions,
with no single-process or single-machine component in the request path**, plus a
capacity model that says exactly how many nodes a given load needs.

Design targets to build and test against:

| Metric | Target |
|---|---|
| API tier | Stateless; any replica serves any request; scale 3 → N pods with zero code change |
| Concurrent connected users (validated by load test) | 10k on a small cluster; documented linear path to 1M+ |
| Time to first token (streamed) | p50 < 800 ms, p95 < 2.5 s (excluding DB execution) |
| Non-LLM API endpoints (auth, history, schema list) | p95 < 150 ms |
| Availability | 99.9% per region; no single point of failure |
| Overload behavior | Graceful: queue → 429 with `Retry-After` → degraded mode. Never OOM, never hang |
| Isolation | Zero cross-tenant data access, proven by automated tests |

## CURRENT BOTTLENECKS (verified in the code — fix all of these)

1. **Blocking sync handlers + thread-per-request.** `api/main.py::ask` is a sync `def`,
   and `_run_orchestrated_with_timeout` spawns a raw `threading.Thread` per request and
   abandons it on timeout (the work keeps running, uncancelled). Under load this
   exhausts the AnyIO thread pool (~40 threads) and piles up orphaned inference.
2. **In-memory rate limiting.** `agent/rate_limit.py` and `api/rate_limit.py` are
   per-process (`BoundedLimiterCache`, `SlidingWindowRateLimiter`). With N replicas every
   limit becomes N× the configured value. The LLM-call limiter is process-global, not
   per user or tenant.
3. **Rate limits keyed by client IP** (`request.client.host`). Behind a load balancer this
   is the LB's IP (everyone shares one bucket); behind carrier NAT, thousands of users
   share one IP. Limits must key on authenticated principal + tenant.
4. **Local-file vector store.** ChromaDB `PersistentClient` on a container volume
   (`embeddings/.chroma`) cannot be shared across replicas.
5. **Single local Ollama.** One inference host, no batching, no queue, no failover —
   this is the real throughput ceiling.
6. **Heavy models in the API process.** faster-whisper, Piper TTS, CLIP
   (sentence-transformers + torch), Tesseract, and scenedetect/OpenCV are loaded inside
   the same process that serves HTTP. One voice request can starve the chat API.
7. **No response streaming.** `/ask` returns only when the whole graph finishes
   (retrieve → plan → generate → review → validate → cost → execute → insight). Users
   wait for the slowest node with nothing on screen.
8. **Per-process caches.** `functools.cache` / `lru_cache` engines and schema data are
   warmed per process; there is no shared cache, and nothing caches repeat questions.
9. **No tenancy.** `identity/models.py` has users/roles/sessions but no
   `tenant_id`/`org_id`. Database connections, golden examples, chat history, documents
   and vector collections are global.
10. **Single-container deployment.** `docker-compose.yml` is one service, 2 CPU / 4 GB,
    no Kubernetes, no autoscaling, no multi-replica story.

---

## PHASE 0 — Baseline and load harness (do this first; no behavior changes)

1. Add a load-test suite in `eval/load/` using **k6** (preferred) or **Locust**, with
   scenarios: login → ask (streamed) → execute → history; a burst scenario; a soak
   (60 min) scenario. Mock-LLM mode (deterministic fake with configurable latency) so
   the API tier can be tested independently of inference cost.
2. Record current p50/p95/p99, error rate and max sustainable RPS in
   `docs/SCALE_BASELINE.md`. Every later phase must re-run this and append its numbers.
3. Add `make load-test` and a CI job that runs a short smoke load test (mock LLM).

**Gate:** baseline documented; harness runs in CI.

## PHASE 1 — Async, stateless API tier

1. Convert request paths to `async def` end to end. Blocking work (DB drivers without
   async support, sqlglot parsing, pandas) goes through bounded executors
   (`anyio.to_thread.run_sync` with an explicit `CapacityLimiter`), never unbounded threads.
2. Remove `_run_orchestrated_with_timeout`'s abandon-the-thread pattern. Use
   `asyncio.timeout` / `anyio.fail_after` with **real cancellation** that propagates into
   the LLM HTTP call (cancel the httpx request) and into the DB (cancel the query /
   close the connection — reuse `db/execution.py`'s timeout logic).
3. Use LangGraph's async API (`ainvoke` / `astream_events`) and make graph nodes async.
   Use async clients: `ollama.AsyncClient` or an OpenAI-compatible async client,
   SQLAlchemy 2.0 `create_async_engine` for PostgreSQL/MySQL (`asyncpg`, `aiomysql`);
   keep sync drivers (pyodbc, oracledb) behind a bounded executor with their own pool.
4. Remove all per-process request state. Anything that must survive between requests
   (sessions, conversation state, rate-limit counters, caches) moves to Redis or Postgres.
5. Run with multiple workers (gunicorn + uvicorn workers, or uvicorn `--workers`) and add
   a test proving two workers produce identical behavior.
6. Graceful shutdown: on SIGTERM stop accepting, drain in-flight streams (configurable
   grace period), then exit. Separate `/livez` and `/readyz` (readyz checks Redis,
   DB pool, inference gateway reachability); keep `/health` as an alias.

**Gate:** load test shows no thread-pool exhaustion at 10× baseline concurrency;
cancelled requests verifiably stop LLM and DB work; full test suite green.

## PHASE 2 — Streaming responses

1. Add `POST /ask/stream` returning **Server-Sent Events** (keep `/ask` for compatibility;
   implement it on top of the same stream by collecting events).
2. Event types: `status` (node started/finished — retrieving schema, generating SQL,
   validating, estimating cost, executing), `sql` (final validated SQL), `token` (insight
   text streamed token by token), `rows` (result page), `chart`, `error`, `done`.
   Include `request_id` and a monotonically increasing `seq` on every event.
3. **Never stream unvalidated SQL to the client as final.** Draft SQL tokens may be shown
   only if clearly labelled as draft; the `sql` event is emitted only after the validator
   passes. The Confirm-and-Run flow stays exactly as it is.
4. Heartbeat every 15 s; client disconnect cancels the server-side graph run.
5. Frontend (`frontend/`): consume the stream with `fetch` + `ReadableStream` (not
   `EventSource`, so the bearer header works), render node progress, stream insight text,
   support Stop. Handle reconnect with `Last-Event-ID` where a job is resumable (Phase 4).

**Gate:** time-to-first-event < 300 ms p95; Stop cancels server work; UI tests added.

## PHASE 3 — Distributed rate limiting, quotas, and admission control

1. Move every limiter to **Redis** using atomic Lua scripts (sliding-window log or
   token bucket / GCRA). Keep the existing `SlidingWindowRateLimiter` interface so call
   sites barely change; keep the in-memory implementation as the local-dev backend
   selected by config.
2. Key limits by `tenant_id` + `user_id` (+ API key id for machine clients). Use client
   IP only for unauthenticated routes (login, register, password reset), and derive it
   from `X-Forwarded-For` **only** when the request comes from a configured trusted-proxy CIDR.
3. Layered limits: per-user request rate, per-user concurrent in-flight asks (e.g. 2),
   per-tenant request rate, per-tenant **token budget** (daily/monthly, counting prompt +
   completion tokens), and a global concurrency ceiling on the inference gateway.
4. Plan tiers (free / pro / enterprise) as data, not code — stored in the DB, cached in Redis.
5. **Admission control / load shedding:** when the inference queue depth or p95 wait
   exceeds a threshold, reject new low-priority work fast with 429 + `Retry-After`,
   before doing any expensive work. Prioritise paid tiers and interactive over batch.
6. Standard headers on every response: `RateLimit-Limit`, `RateLimit-Remaining`,
   `RateLimit-Reset`, `Retry-After` on 429.
7. Fail-mode decision, documented and tested: if Redis is down, auth-related limiters
   **fail closed**; ask limiters fall back to a conservative per-process limit.

**Gate:** with 3 replicas the effective limit equals the configured limit (± 1 window);
tests for each layer; chaos test with Redis killed.

## PHASE 4 — Inference tier: gateway, queue, batching, caching

1. Create an **LLM gateway** abstraction in `agent/llm_client.py` behind a provider
   interface: `OllamaProvider` (dev), `OpenAICompatibleProvider` (for **vLLM** / **TGI**
   in production — continuous batching and paged attention are what make many concurrent
   users affordable), and optionally a hosted API provider. Config chooses the provider
   per task (SQL generation, planning, review, insight, routing) so a small fast model can
   handle routing/classification while a stronger model generates SQL.
2. Gateway responsibilities: connection pooling, per-backend concurrency limits,
   timeouts, retries with jittered exponential backoff (only on idempotent/transient
   errors), **circuit breaker** per backend, **fallback model** chain, and token accounting
   (feeds Phase 3 budgets).
3. Long or expensive work (schema refresh, document ingestion, media generation, video
   keyframing, batch evaluation) moves to a **job queue** (Redis Streams + a worker pool,
   or Celery/Arq/Dramatiq — choose one, justify it in the docstring). API returns
   `202 Accepted` + job id; client polls or subscribes to SSE for progress. Jobs must be
   idempotent (idempotency key), have retries with a dead-letter queue, and be cancellable.
4. **Caching layers** (all tenant-scoped keys, never shared across tenants):
   - Schema/DDL + retrieval results: Redis, invalidated on schema refresh (versioned key).
   - **Semantic cache** for question → validated SQL: embedding similarity over prior
     approved answers *within the same tenant + database + schema version + role set*.
     A cache hit still goes through the validator and authorization before execution.
   - Result cache for identical SQL on the same database with a short TTL, opt-in per
     database (data freshness matters), and never for queries touching sensitive columns.
   - Prompt/prefix caching at the inference server where supported.
5. Split heavy models into **separate services** with their own autoscaling: `stt-service`
   (faster-whisper), `tts-service` (Piper), `embedding-service` (text + CLIP), `ocr-media-worker`.
   The API process must no longer import torch, ctranslate2, or opencv. Add a CI check
   that fails if it does.

**Gate:** API image size and memory drop measurably; load test with a real vLLM/Ollama
backend shows queueing (not failure) under overload; circuit breaker verified by
fault injection.

## PHASE 5 — Shared data tier

1. Replace ChromaDB `PersistentClient` with a **networked vector store** behind the
   existing retriever interface: **pgvector** (preferred — one fewer system, same Postgres
   as identity) or Qdrant/Chroma server mode. Collections/partitions per tenant (or a
   `tenant_id` filter enforced inside the repository layer, never by callers).
2. Identity, chat history, golden examples, moderation store, audit log → **PostgreSQL**
   with a primary + read replicas. Route reads (history listing, search) to replicas.
   Put **PgBouncer** (transaction pooling) in front; size pools from a formula
   documented in `docs/CAPACITY_PLANNING.md` (replicas × workers × pool_size ≤ DB max).
3. Customer target databases stay read-only as today. Add per-tenant, per-database
   connection pool limits and a statement timeout set at the session level, so one
   tenant's slow warehouse cannot exhaust shared workers.
4. Uploaded files, media, thumbnails → **object storage** (S3-compatible; MinIO for local)
   with signed, short-lived URLs. Nothing written to container local disk except temp.
   Once done, enable `read_only: true` on the container root filesystem.
5. Chat history: partition large tables by time (monthly) and index by
   `(tenant_id, user_id, updated_at)`. Add retention policies per tenant.

**Gate:** kill any single API pod mid-conversation — the user's next request succeeds on
another pod with full history and context.

## PHASE 6 — Multi-tenancy and authorization

1. Add `tenants` (organizations) and `memberships` via an **Alembic migration** in
   `identity/migrations/`. Every tenant-owned table gets `tenant_id NOT NULL` + index.
   Write a data migration that places existing data into a `default` tenant.
2. Put `tenant_id` into the access token claims and into a request-scoped context
   (`contextvars`). Repository functions take tenant from context, not from parameters
   callers can forget.
3. Enable **PostgreSQL Row-Level Security** on every tenant-owned table as defense in
   depth (`SET app.tenant_id` per transaction via the pooled connection).
4. Database connections become tenant-owned resources, configured via an admin API
   instead of only `.env` (keep `.env` as the bootstrap/default tenant path). Connection
   secrets go to a **secrets manager** (Vault / AWS Secrets Manager / Azure Key Vault,
   pluggable), encrypted at rest with envelope encryption — never in the app DB in plaintext.
5. Authorization model: extend the existing DB-backed RBAC (`identity/rbac.py`,
   `agent/authz.py`) with tenant-scoped roles (`tenant_admin`, `analyst`, `viewer`, …)
   plus **per-database and per-table/column grants**, so retrieval only surfaces tables the
   caller may see and the validator rejects SQL referencing anything else. Keep
   sensitive-column blocking. Consider row-level policies pushed down as mandatory
   `WHERE` predicates injected at the AST level (sqlglot) for tenants that need them.
6. **Cross-tenant isolation test suite** (`tests/security/test_tenant_isolation.py`):
   for every endpoint and every repository function, user of tenant A must get 404 (not
   403, to avoid existence leaks) for tenant B's resources. Include vector search, semantic
   cache, result cache, job status, media URLs, and SSE streams. This suite is a CI gate.

**Gate:** isolation suite passes; RLS verified by a test that deliberately omits the
tenant filter at the ORM level and still gets zero rows.

## PHASE 7 — Authentication and security hardening

1. **Tokens:** production default `RS256`/`ES256` (asymmetric) instead of HS256, keys
   loaded from the secrets manager, published via a JWKS endpoint, with `kid`-based key
   rotation and overlap. Short-lived access tokens (5–15 min); keep the existing
   rotating, hashed refresh tokens with reuse detection.
2. **SSO:** OIDC (already partly present in `security/oidc.py`) per tenant — Entra ID,
   Okta, Google Workspace — plus SAML via a broker if needed; SCIM provisioning for
   enterprise tenants; enforce MFA (TOTP + WebAuthn/passkeys) per tenant policy.
3. **Machine access:** tenant-scoped API keys (hashed at rest, prefix-identifiable,
   scoped permissions, expiry, last-used tracking, revocation).
4. **Session revocation at scale:** revoked token/session ids in Redis with TTL =
   remaining token lifetime, checked on every request; "sign out everywhere" works across pods.
5. Brute-force protection: per-account and per-IP progressive delay/lockout on login,
   CAPTCHA hook after N failures (pluggable), breached-password check (k-anonymity HIBP)
   in `identity/password_policy.py`.
6. Transport and edge: TLS 1.2+ everywhere including internal service calls (mTLS via
   service mesh or at least TLS to Redis/Postgres), HSTS, strict CORS allowlist per
   environment, request body size limits, WAF + DDoS protection at the edge (Cloudflare
   / AWS WAF / Azure Front Door — document, don't hard-code).
7. LLM-specific security (extend, don't replace, the existing guards):
   keep the SQL AST allowlist as the hard boundary; add output guarding for insight text
   (no system prompt / secret echo, PII redaction via `observability/redaction.py`);
   ensure retrieved documents/web content are always wrapped as untrusted data in prompts;
   per-tenant model/tool allowlists; re-run `eval/security_benchmark` in CI on every PR
   touching `agent/`, `rag/`, `search/`, or prompts.
8. **Audit logging:** append-only, tenant-scoped audit events (login, role change, SQL
   executed with hash, data export, admin actions), shipped to a separate store; tenant
   admins can export their own audit trail.
9. Supply chain: keep pinned deps; add SBOM generation per build (already started in
   `docs/security/sbom`), image signing (cosign), vulnerability scanning (Trivy/Grype) as
   a CI gate, Dependabot/Renovate.
10. Privacy/compliance: data retention and deletion per tenant (right-to-erasure job that
    covers DB rows, vectors, caches, object storage, and logs), data-residency setting per
    tenant that pins storage and inference region.

**Gate:** OWASP ASVS L2 checklist in `docs/SECURITY_ASVS.md` with evidence per item;
ZAP baseline scan in CI; token rotation and revocation proven across pods.

## PHASE 8 — Observability and SLOs

1. **OpenTelemetry** traces across API → graph nodes → LLM gateway → vector store →
   target DB → workers, with `tenant_id` (not user PII) as an attribute. Propagate the
   existing correlation id as the trace id.
2. Metrics (Prometheus): request rate/latency/errors per route; per-node graph latency;
   LLM time-to-first-token, tokens/sec, queue wait, tokens per tenant; cache hit ratios;
   rate-limit rejections; DB pool saturation; job queue depth and age. Extend the existing
   `observability/metrics.py` and `/metrics/performance` rather than duplicating.
3. Structured JSON logs with redaction (reuse `observability/redaction.py`), sampled at
   high volume.
4. SLOs with error budgets and burn-rate alerts (multi-window). Grafana dashboards as code
   in `deploy/observability/`.
5. LLM quality telemetry: SQL validation pass rate, retry count distribution, execution
   success rate, thumbs-up/down, all per model version — so a model swap can be
   evaluated against the existing `eval/` harness before full rollout.

**Gate:** a single request can be followed end to end in one trace; alerts fire in a
staged fault-injection test.

## PHASE 9 — Deployment, autoscaling, resilience

1. **Kubernetes** manifests via Helm in `deploy/helm/`: `api`, `worker`, `stt`, `tts`,
   `embedding`, `llm-inference` (vLLM on GPU nodes), Redis (or managed), Postgres (or
   managed), object storage. Keep `docker-compose.yml` working for local dev with all
   services (add Redis, Postgres+pgvector, MinIO, and a mock LLM).
2. Autoscaling: HPA on the API by CPU **and** in-flight requests; **KEDA** on workers by
   queue depth; GPU inference scaled on queue wait / KV-cache utilisation, with min
   replicas to avoid cold starts. PodDisruptionBudgets, resource requests/limits,
   topology spread across zones.
3. Resilience patterns everywhere: timeouts on every network call, bulkheads (separate
   pools for interactive vs batch), circuit breakers, retries only where idempotent.
4. Multi-region (document + stub, implement if budget allows): regional stateless tiers,
   GeoDNS/anycast routing, tenants pinned to a home region for data residency, Postgres
   cross-region replica for DR. Define RPO/RTO.
5. Deployments: rolling + canary (Argo Rollouts or Flagger) gated on SLO metrics;
   feature flags for risky paths; database migrations follow expand → migrate → contract.
6. Chaos tests: kill pods, kill Redis, slow the DB, fail the LLM backend — confirm
   graceful degradation (cached/queued/429, never 500 storms or hangs).

**Gate:** load test at 10× Phase 0 on the Helm deployment with autoscaling; chaos
scenarios documented with results.

## PHASE 10 — Capacity planning and cost

Write `docs/CAPACITY_PLANNING.md` with a spreadsheet-style model:
tokens per request (measured, per graph path), GPU throughput per model (measured on
vLLM), cache hit ratio, → GPUs, API pods, DB connections, and monthly cost per 1k / 100k /
1M daily active users. State plainly where the next bottleneck appears at each scale step
and what fixes it. Include per-tenant cost attribution so pricing tiers can be set.

---

## WORKING RULES FOR EVERY PHASE

- Start each phase in plan mode: list files to change, risks, and the migration path.
  Wait for my approval before large refactors.
- Small, reviewable commits; one PR per phase (or sub-phase if > ~1,500 lines).
- Backward compatibility: `/ask`, `/execute`, and existing auth flows keep working; new
  behavior behind config flags that default to today's behavior in local dev.
- Every new setting goes in `config/settings.py` with a docstring and in `.env.example`.
- Every phase updates `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/CONFIGURATION.md`,
  and appends measured results to `docs/SCALE_BASELINE.md`. No claims without numbers.
- Never weaken a security test to make it pass. If a security control conflicts with
  performance, stop and ask me.
- Don't claim something works until you've run it. If you can't run it (e.g. no GPU,
  no cloud), say so explicitly and mark it UNVERIFIED in the docs.

Start with **Phase 0**. First, give me a short summary of what you found in the code that
confirms or contradicts the bottleneck list above, then the Phase 0 plan.
