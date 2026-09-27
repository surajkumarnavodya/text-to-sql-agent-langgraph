# Enterprise Scalability & Security Assessment — Final Report

**Date**: 2026-09-27
**Scope**: Full architecture assessment of the Text-to-SQL agentic AI platform against a 39-phase enterprise scalability/security assessment prompt, plus implementation of safe, in-place P0/P1 fixes.
**Rule followed throughout**: no rewrite of the application, no microservices introduced for their own sake, no capacity claim without either a real measurement or an explicit "estimated"/"not tested" label.

This report **synthesizes** three bodies of prior, already-verified work in this repository rather than re-deriving them — [`docs/SCALE_OUT_PROMPT.md`](SCALE_OUT_PROMPT.md) (an 11-phase scale-out program, Phase 0 complete), [`docs/SCALE_BASELINE.md`](SCALE_BASELINE.md) (real k6-measured numbers from that Phase 0), and [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) (a full STRIDE analysis) — and adds new findings/fixes specifically in the areas those documents had not yet covered: identity-aware rate limiting, trusted-proxy IP handling, structured logging, liveness/readiness separation, graceful shutdown, and DB-pool-vs-concurrency sizing.

---

## 1. Executive Summary

This is a single-process FastAPI application (LangGraph SQL-generation agent, local Ollama inference, SQLAlchemy against a customer-supplied database, ChromaDB for schema/RAG retrieval) that already has real, tested security controls (RBAC, OIDC/local-auth, AST-based SQL validation, per-IP/per-caller rate limiting, a documented STRIDE threat model) and a first, honest load-test baseline (20 concurrent authenticated users served end-to-end with zero server errors; a 100-VU burst degrades to slow/429, never a crash — see [`docs/SCALE_BASELINE.md`](SCALE_BASELINE.md)).

**This pass's job was not to re-architect the system.** It was to (a) verify the existing assessment's conclusions still hold, (b) find any gap those documents hadn't already named, and (c) fix what could be fixed safely, in place, without touching the request/response contract. Six such gaps were found and fixed (§13). None required new infrastructure (no Redis, no message queue, no second service) — every fix is a change to existing modules, all covered by new or existing automated tests, and all verified live against the real running application (§14).

**No claim of "million-user" or any specific concurrent-user capacity is made anywhere in this report.** The only real load-test evidence this codebase has is [`docs/SCALE_BASELINE.md`](SCALE_BASELINE.md)'s Phase 0 numbers, reproduced honestly in §5 below, never extrapolated. Everything beyond that is explicitly labeled **Estimated** or **Not Tested**.

The single largest remaining structural gap, unchanged by this pass and already disclosed in `docs/SCALE_OUT_PROMPT.md`'s own bottleneck list: **this application has no multi-tenancy concept at all** (§8) — every configured database, every document collection, every golden-example store is process-global, not scoped to an organization/tenant. A deployment serving more than one customer organization from one instance today would have zero data isolation between them at the database-connection level (there is no `tenant_id` anywhere in `identity/models.py`). This is named, not hidden, in every section below where it's relevant.

---

## 2. Current-State Architecture

```
                              ┌─────────────────────────────────────────┐
                              │         Single Docker container          │
                              │        (docker-compose.yml, 1 replica)   │
                              │                                          │
  Browser (React SPA) ──HTTPS──▶  FastAPI (uvicorn, sync handlers)      │
   (or a bare REST client)    │   ├─ Starlette AnyIO thread pool         │
                              │   │    (dispatches every sync route)     │
                              │   ├─ ThreadPoolExecutor (_get_ask_executor)│
                              │   │    bounded, sized to                 │
                              │   │    MAX_CONCURRENT_ASK_REQUESTS (50)   │
                              │   ├─ In-process rate limiters             │
                              │   │    (BoundedLimiterCache, threading.Lock)│
                              │   ├─ Process-lifetime singletons          │
                              │   │    (DB engines, Ollama client,        │
                              │   │     compiled LangGraph graphs)        │
                              │   ├─ LangGraph SQL pipeline (12 nodes)    │
                              │   ├─ Orchestrator (router + subgraphs,    │
                              │   │    optional, ENABLE_MULTI_SOURCE_ROUTER)│
                              │   ├─ Heavy local models loaded in-process:│
                              │   │    faster-whisper, Piper TTS,         │
                              │   │    CLIP/sentence-transformers,        │
                              │   │    Tesseract, PySceneDetect/OpenCV    │
                              │   └─ ChromaDB PersistentClient            │
                              │        (local disk, embeddings/.chroma/) │
                              └───────┬──────────────┬───────────────────┘
                                      │              │
                         SQLAlchemy   │              │  Ollama client (HTTP)
                  (pooled connections)│              │
                                      ▼              ▼
                        ┌──────────────────┐   ┌─────────────┐
                        │ Customer DB(s)    │   │   Ollama     │
                        │ (Postgres/MySQL/  │   │ (local LLM,  │
                        │  SQL Server/Oracle)│   │  one host)   │
                        │ — one or more,     │   └─────────────┘
                        │  DB_CONNECTIONS    │
                        └──────────────────┘
                                      │
                        ┌──────────────────┐   ┌──────────────────────┐
                        │ Identity DB        │   │ RAG store (SQL Server │
                        │ (Postgres, optional,│   │ VECTOR, optional)     │
                        │  local_auth_enabled)│   └──────────────────────┘
                        └──────────────────┘
```

**Key structural facts, verified in code:**
- One process, one container, no load balancer in front of it in the shipped `docker-compose.yml`.
- Every route handler is a synchronous `def` (not `async def`) — FastAPI/Starlette dispatches each to its own worker thread from AnyIO's thread pool automatically; `/ask` additionally goes through its own bounded `ThreadPoolExecutor` for admission control (added in the Scale-out program, see `docs/SCALE_OUT_PROMPT.md` bottleneck #1, now partially addressed).
- Rate limiting, concurrency limiting, the compiled LangGraph graphs, the Ollama client, and every DB engine are `functools.cache`/`lru_cache` **process-local singletons** — correct for one instance, silently non-shared the moment a second replica exists (bottleneck #2, #8, unaddressed by this pass — see §12).
- ChromaDB is a local-disk `PersistentClient` (bottleneck #4) — cannot be shared across replicas without a shared volume or a swap to a client/server Chroma deployment.
- No tenant concept anywhere (bottleneck #9) — see §8.

## 3. Target-State Architecture (Directional, Not Implemented This Pass)

This mirrors `docs/SCALE_OUT_PROMPT.md`'s own 11-phase target (Phases 1–10, none started beyond Phase 0's harness) — reproduced here at a high level for completeness, not re-planned:

```
   Clients ──▶ Load Balancer / API Gateway (TLS termination, WAF)
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
   API replica 1            API replica N        (async handlers, stateless)
        │                        │
        ├── Shared rate-limit/quota store (Redis) ──────────────┐
        ├── Shared session/concurrency-budget store (Redis) ────┤
        └── Shared vector store (Chroma server mode / managed) ─┤
                    │                                            │
                    ▼                                            ▼
        Inference gateway/queue (batching, backpressure) ── Ollama pool
                    │                                     (autoscaled, GPU-bound)
                    ▼
        Customer DB(s) — per-tenant connection pools, tenant_id everywhere
                    │
        Identity DB, RAG store — unchanged in shape, tenant-scoped
```

This target is **not built** in this pass, by explicit instruction ("do not rewrite the entire application," "do not introduce microservices merely for architecture's sake"). §16 gives the phased path to it.

## 4. Request Lifecycle (as verified in code)

`POST /ask`, the highest-value/highest-cost route:
1. `_add_security_headers` middleware wraps every response (CSP, HSTS, etc.) — calls `get_settings()` (cheap, cached).
2. `require_permission(Permission.ASK)` dependency resolves `AuthIdentity` from the configured `auth_mode` (`none`/`static_token`/`oidc`/`local`).
3. `_rate_limit_key(identity, request, settings)` resolves a caller key — identity-scoped when a real per-caller identity exists, IP-scoped otherwise (as of this pass, via `security.client_ip.resolve_client_ip`, trusted-proxy-aware).
4. The per-caller question-rate limiter and the global/per-caller `/ask` concurrency limiter (`agent.rate_limit.ConcurrencyLimiter`) are checked — 429 with `Retry-After` before any real work if either is exceeded.
5. The request is submitted to `_get_ask_executor` (bounded `ThreadPoolExecutor`, default 50 workers) and the calling coroutine awaits it with a whole-request timeout (`_run_orchestrated_with_timeout`).
6. Inside the worker thread: `run_orchestrated` (or `run_agent` directly if the multi-source router is off) runs the full LangGraph pipeline — schema retrieval (Chroma), golden-example retrieval (Chroma), business-context retrieval (Chroma), query planning (Ollama call), SQL generation (Ollama call, one or more retries), SQL review (Ollama call), AST validation (`sqlglot`, in-process, no I/O), cost estimation (a DB round-trip), execution (a DB round-trip, row-capped, driver-level or thread-abort timeout), insight generation (Ollama call).
7. On success, `persist_ask_turn` (if locally authenticated) writes to the identity DB — wrapped in a broad `except Exception`, never able to fail the response.
8. Response assembly redacts secrets from every free-text field before serialization.

**Cost concentration**: per `docs/SCALE_BASELINE.md`'s own measured breakdown, the dominant cost is CPU-bound embedding inference and the Ollama round trip(s) — not I/O wait, not FastAPI/Starlette overhead. This is why the real fix for `/ask` throughput is inference-tier scaling (Phase 4 of the scale-out program), not API-tier code changes — a conclusion this pass did not need to re-verify, since it was already measured.

## 5. Capacity Assessment

| Component | Current State | Bottleneck | Scaling Method | Priority |
|---|---|---|---|---|
| API process (FastAPI/uvicorn) | Single container, sync handlers on AnyIO thread pool + a bounded `/ask` executor | Thread-per-request under sustained load; no horizontal replicas in shipped compose file | Async rewrite (Phase 1) + real multi-replica deployment behind a load balancer (Phase 9) | P1 |
| Ollama (LLM inference) | One local host, no batching, no queue, no failover | Single point of failure and the dominant latency cost (93.8–98% of wall-clock per `docs/PERFORMANCE_RESULTS.md`) | Separate, independently-scaled inference tier with a queue/gateway (Phase 4) | **P0** — this is the true throughput ceiling |
| ChromaDB (schema/RAG/golden-example index) | Local-disk `PersistentClient`, one per process | Cannot be shared across replicas | Chroma server mode or a managed vector DB (Phase 5) | P1 |
| Customer DB connections | Pooled (`db_pool_size`=10, `db_max_overflow`=20 default per configured database) | This pass's new startup warning (§13) surfaces when `MAX_CONCURRENT_ASK_REQUESTS` exceeds pool capacity — confirmed to fire live against the real 2-database dev config (`MAX_CONCURRENT_ASK_REQUESTS=50` > pool capacity 30) | Raise pool size (bounded by the DB server's own `max_connections`, multiplied across every replica) or lower concurrency | P1 (config tuning, no code change needed) |
| Rate limiting / concurrency limiting | In-process (`threading.Lock`-protected, correct within one process) | N replicas ⇒ N× the configured limit; no cross-process coordination | Redis-backed shared limiter (Phase 3) | P1 |
| Heavy local ML models (Whisper, Piper, CLIP, Tesseract, PySceneDetect) | Loaded in the same process that serves chat HTTP | One voice/media-search request can starve the chat API's own CPU budget | Split into separate services (Phase 4/5 territory) | P2 |
| Multi-tenancy | None — no `tenant_id` anywhere | A single instance serving multiple customer orgs has zero enforced data isolation beyond whatever `DB_CONNECTIONS` naming discipline an operator maintains by hand | A genuine tenant model (Phase 6) | **P0** if this app is ever sold to more than one organization from a shared instance; N/A for a single-tenant-per-deployment model |
| Deployment topology | Single Docker Compose service, 2 CPU / 4 GB, no orchestrator-level autoscaling | No rolling-deploy story, no health-based autoscaling | Kubernetes/ECS with real liveness/readiness probes (this pass adds the `/live` vs `/health` split needed for this — see §13) | P2 |

**Capacity scenarios, explicitly labeled:**

| Scenario | Status | Evidence |
|---|---|---|
| 20 concurrent authenticated users, full login→ask→execute→history flow, one instance, mocked LLM latency | **Measured** | `docs/SCALE_BASELINE.md` Run 1: 0 server errors, `/ask` p50 6.24s / p95 8.75s |
| 100-VU burst, same stack | **Measured** | `docs/SCALE_BASELINE.md` Run 2: 100% no-5xx, degrades to slow/timeout, never crashes |
| Real-Ollama (not mocked) latency/throughput at any concurrency | **Not Tested** | No run in this codebase's history has used a real LLM under load |
| Multi-replica behavior | **Not Tested** | No multi-instance deployment has ever been run |
| Any number in the thousands-to-millions concurrent-user range | **Not Tested / No Claim Made** | Would require a defined workload, a real inference tier, and a load test at that scale — none exist |

## 6. Authentication Assessment

| Mode | Mechanism | Verified Controls | Gaps |
|---|---|---|---|
| `none` (default) | No auth — full implicit admin | Explicitly fails closed at startup if `ENVIRONMENT=production` (`_require_identity_in_production`) | By design — local/dev only |
| `static_token` | Shared bearer secret, constant-time compared | `SecretStr`, never logged | One shared secret ⇒ no sub-identity; inherent to the mode, documented |
| `oidc` | JWT validated against IdP JWKS, server-side algorithm allowlist, mandatory audience, bounded clock skew | `tests/test_oidc.py` covers forged-signature/alg-confusion | Live end-to-end IdP flow **not verified** in this environment (disclosed in `docs/AUTHENTICATION.md`) |
| `local` | This app's own accounts — Argon2id, JWT access + rotating refresh tokens, lockout, reuse-detection | Full test coverage (`identity/` package); this pass's live smoke test registered two real local users and exercised login end-to-end successfully | None found new in this pass |

**This pass's change**: identity resolution for rate-limiting purposes now correctly distinguishes all four modes (`security.oidc.real_caller_subject`) — a `local`-mode user was, before an earlier session's fix, incorrectly treated as having no real per-caller identity for `/ask`'s own limiter; this pass extends the same correct distinction to every other rate-limited route (`/execute`, `/schema/refresh`, `/generate/confirm`, `/search/media`, document/attachment routes).

## 7. Authorization Assessment

- RBAC: 15 fine-grained `Permission` values, 4 default roles (`viewer`/`user`/`analyst`/`admin`), fail-closed on an unrecognized role (`agent/authz.py`).
- Enforced server-side at every data-touching route **and** inside the orchestrator's own router (`router_node` filters the LLM's source selection through a permission check before any subgraph runs) — never left to the frontend.
- Verified live in this pass: a freshly-registered `user`-role account correctly got `200` from `/execute` and `403` from `/schema/refresh` (an `admin`-only action) — real evidence, not just a unit-test assertion.
- **No per-resource ownership model** beyond what specific features already implement (chat-history `user_id` scoping, attachment `owner_subject` scoping) — an `admin`'s reach is intentionally broad. This is unchanged, pre-existing, and documented in `docs/THREAT_MODEL.md`.

## 8. Multi-Tenancy Assessment

**There is no tenant concept in this codebase.** `identity/models.py`'s 14 tables have no `tenant_id`/`org_id` column anywhere. `Settings.databases` (the multi-database feature) is a *routing* mechanism for one deployment to talk to several named databases based on question content — it is not tenant isolation, and nothing prevents any authenticated user (regardless of role) from being routed to any configured database's schema. Chat history, documents, golden examples, and vector collections are all either global or scoped by `user_id`/`owner_subject` alone, never by an organizational boundary.

**Practical implication**: this application's real deployment model today is **one instance per customer/organization** (the `docker-compose.yml` topology already implies this). Attempting to serve multiple distinct customer organizations from one shared instance would require building a tenant model from scratch — a Phase 6-scale project (`docs/SCALE_OUT_PROMPT.md`'s own Phase 6), not attempted here, and not safe to attempt as a bolt-on without a dedicated design pass given how many modules (`db/connection.py`, `embeddings/schema_indexer.py`, `identity/models.py`, `rag/store.py`, every Chroma collection) would need a consistent tenant-scoping story simultaneously to avoid a partial, false sense of isolation.

## 9. Database Assessment

- Connection pooling: `db_pool_size`=10 / `db_max_overflow`=20 per configured database (SQLAlchemy `QueuePool` defaults, unchanged this pass).
- **New this pass**: a startup-time warning (`_warn_on_ask_concurrency_pool_mismatch`) when `MAX_CONCURRENT_ASK_REQUESTS` exceeds total pool capacity across configured databases — verified firing live against the real dev environment's 2-database config (pool capacity 30 vs. concurrency limit 50). This doesn't change behavior (excess requests correctly queue for a pooled connection, which was already true) — it makes a previously-silent sizing mismatch visible at startup instead of only discoverable under load.
- Read-only enforcement is layered (AST validator + DB-role convention), startup-enforced since an earlier phase (`_enforce_database_write_privileges`) — confirmed still firing correctly in this pass's live smoke test (both configured dev databases logged the expected "appears to have write privileges" warning, since the dev DB roles are not actually read-only — correctly non-fatal since `ENVIRONMENT` is not `production`).
- A real, previously-fixed bug (`docs/SCALE_OUT_PROMPT.md`'s own Phase 0 finding, not this pass's): `get_engine` used to pass a password-masked URL string to `create_engine`, silently breaking every discrete-field password-based connection. Already fixed and regression-tested before this pass began.

## 10. AI / Ollama Assessment

- Single local Ollama host, no batching, no queue, no autoscaling — the single largest, already-disclosed throughput ceiling (`docs/PERFORMANCE_RESULTS.md`: LLM inference is 93.8–98% of wall-clock time per request).
- Configurable model selection (a separate, earlier pass this session — see "Configurable Ollama model selection" above) is request-scoped, not global mutable state, and validates server-side against an allowlist — correctly does not weaken this assessment's conclusions.
- No per-caller/tenant isolation of inference cost beyond the existing process-global LLM-call rate limiter (`docs/THREAT_MODEL.md`'s own disclosed DoS gap, unaddressed by this pass — a genuine Phase 3/4 concern, not safely fixable without the shared-state infrastructure those phases scope).
- Ollama is never exposed publicly in the current architecture (only the API process talks to it, over `OLLAMA_HOST`, typically `localhost`) — confirmed by inspection, not changed by this pass.

## 11. Caching Assessment

- Chroma schema-embedding cache: SHA-256 schema fingerprint, skip-if-unchanged — process-local, correct for one instance.
- Process-lifetime singletons (DB engines, Ollama client, compiled LangGraph graphs, the new `_get_ask_executor`) — all `functools.cache`/`lru_cache`, all process-local. This pass added no new cache; it added `_shutdown_ask_executor` (§13) to correctly drain the existing executor cache on shutdown rather than abandon it.
- No shared/distributed cache exists anywhere (no Redis, no Memcached) — every cache is single-process. This is consistent across the whole system, not a new finding.
- **Caching sensitive data**: no response caching of query results or PII exists at any layer today — nothing in this pass introduces any, per the explicit "never cache sensitive data without authorization-context analysis" rule.

## 12. Queue / Worker Assessment

- No message queue, no background job/task-queue infrastructure exists anywhere in this codebase (confirmed: no Celery/RQ/Redis in `requirements.txt`). The one bounded worker pool that exists (`_get_ask_executor`, and the pre-existing `moderation`/`media` ingestion thread pools) is a same-process `ThreadPoolExecutor`, not a distributed queue.
- This pass did **not** introduce a queue — per the explicit instruction to use queues/workers "only where justified," and no component here has a workload shape (fire-and-forget, retryable, needs independent scaling) that justifies one yet. The nearest candidate (long-running AI generation work) is already synchronous-with-timeout and bounded by the concurrency limiter; a real queue is Phase 1/4 territory in the existing scale-out program, not a safe bolt-on here.

## 13. Observability Assessment

| Signal | Before this pass | After this pass |
|---|---|---|
| Logs | Plain text, correlation-ID-tagged | **New**: opt-in structured JSON (`LOG_FORMAT=json`, `config/settings.py`'s `_JsonLogFormatter`) — one JSON object per line, what a real log aggregator needs; default (`text`) unchanged |
| Liveness vs. readiness | One `/health` endpoint doing both jobs (checks DB/Chroma/Ollama) | **New**: `GET /live` (zero dependency checks) added alongside the unchanged `/health` — an orchestrator's liveness probe should never kill a process just because a downstream dependency is slow |
| Metrics | `GET /metrics/performance` (admin-only, in-process `PerformanceMetrics` singleton, resets on restart) | Unchanged this pass — already disclosed as single-process/in-memory, a real cross-process rollup needs Prometheus/OTel (not attempted) |
| Tracing | None | Not attempted — a genuinely separate, larger initiative |
| Shutdown visibility | The `/ask` thread pool was abandoned on process exit | **New**: `_shutdown_ask_executor` drains it (`.shutdown(wait=True)`) during `lifespan`'s teardown — an in-flight request during a rolling restart is no longer silently cut off |

## 14. Security Threat Model (Delta Since `docs/THREAT_MODEL.md`)

The existing STRIDE table in [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) remains the authoritative full analysis. This pass's own additions/corrections:

| Threat | Current Control (after this pass) | Gap Before This Pass | Severity | Action Taken |
|---|---|---|---|---|
| IP-based rate-limit keying is spoofable/meaningless behind a reverse proxy | `security/client_ip.py`'s exact-hop-count trusted-proxy algorithm, gated by `TRUSTED_PROXY_COUNT` (default 0 = unchanged behavior) | Naively trusting `X-Forwarded-For`'s leftmost entry (or raw `request.client.host` with no proxy awareness at all) | Medium (only relevant once a real reverse proxy is deployed — N/A for direct-exposure dev/single-VM deployments) | **Fixed** — 13 new regression tests, including explicit spoofing-resistance cases |
| Two distinct authenticated users sharing one IP (NAT/VPN) share one rate-limit bucket on `/execute`/`/schema/refresh`/`/generate/confirm`/`/search/media`/document/attachment routes | Identity-aware keying (`api/rate_limit.py`), mirroring `/ask`'s own pre-existing pattern | Only `/ask` was identity-aware; every other rate-limited route was IP-only | Low-Medium | **Fixed** — verified live: two freshly-registered users from the same source IP each got an independent 20/min budget |
| `docs/THREAT_MODEL.md`'s own "No concurrency limiter on in-flight requests" DoS entry | Bounded `/ask` concurrency (`_get_ask_executor` + `ConcurrencyLimiter`) | (Already fixed in an earlier session — the Scale-out program — not this pass) | — | Confirmed still correctly in place; this pass adds graceful shutdown for the same executor |
| Silent DB-pool/concurrency misconfiguration | Startup warning, never fails startup | No signal existed; only discoverable under real load as connection-pool exhaustion | Low (a correctness/operability gap, not a direct exploit) | **Fixed** |
| A killed-but-still-processing `/ask` request during a rolling deploy | Graceful executor shutdown drains in-flight work | Abandoned on process exit | Low | **Fixed** |

No new Spoofing/Tampering/Repudiation/Elevation-of-Privilege findings were identified in this pass — the existing threat model's open items in those categories (SSRF DNS-rebinding TOCTOU, process-global LLM rate limiting, no per-resource ownership model) remain open and are correctly out of scope for a "safe, in-place P0/P1" pass, since none has a fix that doesn't require either new infrastructure or a larger design decision.

## 15. Files Changed

| File | Change |
|---|---|
| `security/client_ip.py` (new) | Trusted-proxy-aware client IP resolution |
| `config/settings.py` | `trusted_proxy_count`, `log_format` fields; `_JsonLogFormatter`; `configure_logging` rewritten |
| `api/main.py` | `/live` endpoint; `_warn_on_ask_concurrency_pool_mismatch`; `_shutdown_ask_executor`; `_rate_limit_key` now trusted-proxy-aware; `execute`/`schema_refresh` param fix |
| `api/rate_limit.py` | `enforce_api_action_rate_limit` now identity-aware |
| `api/identity_auth.py` | `_client_ip` delegates to `security.client_ip.resolve_client_ip` |
| `api/documents.py`, `api/generation.py`, `api/media_search.py`, `api/attachments.py` | Pass `identity=identity` into rate-limit calls; 3 parameter-name fixes (`_identity` → `identity`) |
| `tests/test_client_ip.py`, `tests/test_ask_concurrency_pool_warning.py`, `tests/test_graceful_shutdown.py`, `tests/test_json_logging.py` (new) | 33 new tests |
| `tests/test_api_ask.py`, `tests/test_api_rate_limit.py`, `tests/test_api_health.py`, `tests/conftest.py` | Updated for new signatures; new test classes; new singleton-cache-clear entry |
| `.env.example`, `docs/CONFIGURATION.md`, `docs/DEPLOYMENT.md`, `SECURITY.md`, `CLAUDE.md` | Documentation |

## 16. Tests

- **Added**: 58 new backend tests (see file list above), all passing.
- **Changed**: existing `TestRateLimitKey`/`TestEnforceApiActionRateLimit`/`TestLive`(new class) suites extended for new signatures.
- **Full suite**: **2016 backend tests pass** (`pytest -q`), **216 frontend tests pass** (`vitest run`), `mypy`/`black`/`ruff` clean on every touched file (repo-wide pre-existing drift — 7 ruff / 11 black findings in files this pass never touched — confirmed unrelated by cross-referencing paths).
- **Not executable in this environment**: a real multi-replica deployment test, a real-Ollama load test, a live OIDC IdP end-to-end test — all pre-existing, disclosed gaps (`docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`), unchanged by this pass.

## 17. Load Testing

| Item | Status |
|---|---|
| 20 concurrent users, full flow, mocked LLM | **Measured** — `docs/SCALE_BASELINE.md` |
| 100-VU burst | **Measured** — `docs/SCALE_BASELINE.md` |
| This pass's own 6 changes, live smoke-tested against the real running app (real DB, real Ollama) | **Measured** (functional correctness, not load): `/live` returns JSON with zero dependency calls; startup pool-mismatch warning fired correctly; `/execute` succeeded for an authenticated `user`; `/schema/refresh` correctly 403'd for the same non-admin user; the rate limiter correctly returned `429`+`Retry-After` on the 20th+ call; two distinct freshly-registered users confirmed to have independent rate-limit budgets from the same source IP; `/ask` completed a real end-to-end SQL generation against a live database and live Ollama |
| Any load test of this pass's own changes under concurrency | **Not Tested** — functional correctness was verified live; a dedicated load run re-using `eval/load/` was not performed in this pass |
| Real-Ollama load, multi-replica, tenant-scale numbers | **Not Tested / No Claim Made** |

## 18. P0 / P1 / P2 / P3 Roadmap

- **P0** (blocking for any multi-org or high-load deployment): separate, independently-scalable inference tier for Ollama (queue/batching/failover); a real multi-tenancy model if ever serving more than one organization per instance.
- **P1** (this pass addressed the safely-fixable slice): identity-aware rate limiting everywhere ✅, trusted-proxy IP resolution ✅, DB-pool/concurrency sizing visibility ✅, graceful shutdown ✅, liveness/readiness split ✅, structured logging ✅. Remaining P1: async API tier (Phase 1), distributed rate limiting (Phase 3), shared vector store (Phase 5).
- **P2**: split heavy local ML models (voice/media-search) out of the chat API process; real autoscaling/orchestrator deployment; response streaming.
- **P3**: multi-region, cost/capacity planning tooling, full OTel tracing.

## 19. Migration Roadmap (Phased, Not Attempted Beyond Phase 0)

This is `docs/SCALE_OUT_PROMPT.md`'s own roadmap, referenced not duplicated:

1. **Phase 1 — Harden current monolith**: async handlers, real request-level cancellation. *Complexity: High* (touches every route and the LangGraph invocation boundary).
2. **Phase 2 — Streaming responses**. *Complexity: Medium.*
3. **Phase 3 — Distributed rate limiting/admission control** (Redis). *Complexity: Medium.*
4. **Phase 4 — Independent inference tier** (gateway/queue/batching for Ollama). *Complexity: Very High* (new infrastructure, new failure modes, GPU capacity planning).
5. **Phase 5 — Shared data tier** (Chroma server mode or managed vector DB). *Complexity: Medium-High.*
6. **Phase 6 — Multi-tenancy**. *Complexity: Very High* (touches `identity/`, every DB/vector-store access path, and every authorization check simultaneously).

No cost estimates are invented for any of these — none exist in this codebase's history, and inventing one here would violate this assessment's own evidence-only rule.

## 20. Remaining Risks (Explicit)

1. Ollama remains a single point of failure and the dominant latency cost — unaddressed by this or any prior pass (correctly scoped as Phase 4, a large, separate initiative).
2. No multi-tenancy — a shared instance across customer organizations has no enforced isolation today.
3. Rate limiting and every process-lifetime singleton remain per-process — a second replica today would not share limits, caches, or the vector index.
4. The process-global LLM-call rate limiter (`docs/THREAT_MODEL.md`) means one caller can still degrade service for every other concurrent user — not fixed by this pass's per-action/per-identity limiting, which governs *frequency* of calls, not the shared inference budget itself.
5. SSRF DNS-rebinding TOCTOU window (media download) — pre-existing, disclosed, unaddressed.
6. Live OIDC end-to-end and DAST have never been run in any environment this project has had access to (`docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`) — this pass did not change that verdict.
7. This pass's own changes were smoke-tested live for correctness but not load-tested under concurrency — a reasonable, disclosed follow-up before relying on the new identity-aware rate limiter's behavior under real contention.
