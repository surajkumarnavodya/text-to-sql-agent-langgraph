# Production Readiness & Release Gate (Prompt 25)

**Date:** 2026-10-03. **Scope:** the full backend/platform, across every
prompt built to date (02 through 24) — architecture, SQL safety,
onboarding, semantics, analytics, recommendations, tenancy, security,
scale, observability, backup/recovery, rollback, deployment, operations.
**Method:** every row/finding below is either (a) re-verified fresh this
session against the running code (full `pytest` suite, `ruff`/`black`/
`mypy`/`bandit`, the frontend's own `vitest` suite and production build),
or (b) carried forward explicitly from a named, dated prior report, with
its own currency stated plainly rather than silently assumed.

**This document is now the authoritative production-readiness source for
this project.** It explicitly supersedes the *scores and verdicts* (not
the underlying evidence, which remains valid history) of:

- `docs/PRODUCTION_READINESS_REPORT.md` (2026-09-01, scored 69/100 —
  predates the Streamlit removal, the current auth/RBAC/tenancy system,
  and 23 further prompts of work; its own two addenda already disclose
  this).
- `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md` and
  `docs/security/FINAL_PRODUCTION_GATE.md` (both 2026-09-18 — predates
  Prompts 19-24: SQL Server Query Store intelligence, multi-tenant
  architecture, this project's own enterprise-security hardening pass,
  scale/performance hardening, observability/evaluation, and the full
  integration-and-regression validation pass).
- `docs/RISK_REGISTER.md`'s R-001 ("no authentication or per-user
  authorization by default") — now false; see §3.
- `docs/PRODUCTION_CHECKLIST.md`'s "neither the UI nor the API has real
  auth" line — now false; see §3.

None of those documents' underlying *evidence* is wrong for the state it
was captured against — they're kept as history, now each carrying a
one-line pointer to this document. Nothing here was re-scored by
replaying every original finding; §2's gate matrix re-verifies what
changed and explicitly marks what it did not re-touch.

## 1. Executive summary

**Verdict: CONDITIONALLY READY — not NOT READY, not an unconditional GO.**
This is a real change from the 2026-09-18 report's NOT READY verdict, and
the reason is structural, not a re-scoring of the same facts: that gate's
own decision rule made *any* unverifiable mandatory control an automatic
NOT READY, with no distinction between "verification is incomplete" and
"a vulnerability was found." Re-applying that same rule here still yields
the same three environment-blocked NOT VERIFIED items (DAST, live-OIDC,
live malware-scanner) — **nothing closed those this session either**, for
the identical reason: no live IdP, no live ClamAV daemon, and no
network-reachable deployment exist in this sandboxed environment, same as
every prior session. What changed is that this pass also verified, fresh,
that the two months of work since (multi-tenancy, scale hardening, a
dedicated security audit, observability, full-pipeline integration
testing) hold up under direct re-inspection, and closed two previously-
open, concretely-fixable gaps (backup/recovery and rollback documentation,
§5). The honest framing: **this platform's code-level controls are
unusually mature and freshly re-verified; the three operational
verification gaps are the same three they were in September and still
require real infrastructure this environment cannot provide.**

## 2. Gate matrix

Extends `docs/security/FINAL_PRODUCTION_GATE.md`'s own 25-row matrix
(unchanged rows carried forward by reference, not reproduced below) with
the rows that changed since 2026-09-18 and the rows that prompt covered
but this broader one (analytics/recommendations/tenancy/onboarding/
semantics/AI evaluation/scale) did not.

**Status legend:** `PASS` · `FAIL` · `PARTIAL` · `NOT VERIFIED` · `N/A`

### 2a. Rows changed since 2026-09-18

| Domain | Status (was) | Status (now) | Evidence |
|---|---|---|---|
| Authentication/RBAC | PASS | **PASS, broader** | Prompt 20 added real multi-tenancy on top of the existing 4 auth modes/15 permissions — `tests/security/test_cross_tenant_isolation.py` (532 lines) and `test_cross_tenant_shared_infrastructure.py` (269 lines, proves no tenant leak under real concurrent threading, not just sequentially) both pass fresh this session. |
| Rate Limiting | PARTIAL (process-local only) | **PARTIAL, same root cause, narrower blast radius** | Still process-local (unchanged — no Redis anywhere in this codebase, confirmed by fresh grep). Prompt 22 added a *second*, independent limiter dimension: per-database execution concurrency (`agent.rate_limit.get_database_execution_limiter`), protecting one database's own connection pool from being saturated by concurrent `/ask`/`/execute` calls regardless of the API-tier limiter's own state — a different failure mode than the one this row originally scoped. Measured (in-process benchmark, not live k6 — Docker unavailable in this environment): p50 drops from 540.7ms to 0.8ms at saturation. See `docs/SCALE_BASELINE.md`'s "Prompt 22" entry. |
| Monitoring | PARTIAL (no metrics backend) | **PARTIAL, same root cause, real log-based improvement** | Still no Prometheus/OpenTelemetry (unchanged, deliberate posture). Prompt 23 closed the real gap underneath this row's own "operational maturity" framing: every log line (not just `security.audit` events) now carries both correlation ID and tenant, and every LangGraph stage emits an additive `[trace]` line (status/error-category/model/db-provider/hashed-query-fingerprint/result-size) — the acceptance criterion "failures can be attributed to a specific stage" is now mechanically true from logs alone. See `docs/OBSERVABILITY.md`'s §3/§3a. |
| Backup/Recovery | NOT VERIFIED | **PARTIAL — closed where this app has a real opinion, honestly scoped where it doesn't** | §5 below. A real procedure now exists for what this app actually owns (the optional identity Postgres database's schema via Alembic, the regenerable Chroma vector index, `.env`/configuration). The operator's own connected business database remains explicitly out of scope, as it always was — this app is a read-only client of it, never its data owner. |
| Rollback | (not a named row before) | **PARTIAL — new row** | §5 below. Container-image rollback (digest-pinned, tag-addressable), `alembic downgrade` for the identity schema, and `.env`-flag rollback for every opt-in feature added since Prompt 08 are now documented. Not independently *exercised* against a real deployment in this session (no live environment) — documented and code-level-verified (every migration file has a real `downgrade()`, confirmed by direct read), not drilled. |
| Dependencies | PARTIAL | **PARTIAL, re-verified, zero drift** | Fresh `pip-audit` run this session (Prompt 21): identical CVE set to the 2026-09-18 triage, all already covered by CI's own `--ignore-vuln` allowlist with individual, file:line-cited justification in `docs/security/CVE_TRIAGE.md`. Re-verified that no new first-party usage of `CachePolicy`/`Checkpointer`/`ChatOpenAI` was introduced across Prompts 19-24 (the preconditions that would make any of these CVEs newly reachable) — still 0 matches outside `env/`/`.venv/`. |

### 2b. New rows (areas this gate's September predecessor didn't cover)

| Domain | Status | Evidence |
|---|---|---|
| Tenant Isolation | **PASS** | Real tenant resolution from a server-signed token claim only (never a client-supplied header — `security.tenancy.reject_client_tenant_override` refuses `X-Tenant-Id`-shaped headers outright, even in `AUTH_MODE=none`). Deny-by-default on missing/suspended tenant, anti-enumeration (cross-tenant denial maps to the same 404 a nonexistent resource gets, everywhere this pattern is used). Shared *infrastructure* (compiled LangGraph graph, cached Ollama client) proven not to leak tenant context under genuine concurrent threading, not just sequential calls (`tests/security/test_cross_tenant_shared_infrastructure.py`). **Known, disclosed limitation, not a hidden gap:** no live two-tenant deployment has ever been exercised — every invariant above is verified against an in-memory SQLite identity database, and PII classification + RBAC role definitions remain intentionally global, not per-tenant (`docs/MULTI_TENANCY.md`'s own "Known limitations"). |
| Database Safety | **PASS** | AST-based allowlist (unchanged, still the real boundary per the September gate's own row), plus: restricted-column enforcement extended to the new Prompt 22 result cache (never caches SQL touching a classified-restricted column, regardless of flag state or caller permission) and verified end-to-end through the real compiled graph for the first time this session (Prompt 24's `tests/test_full_pipeline_integration.py`). Query Store intelligence (Prompt 19, SQL-Server-only) never returns raw SQL text, only a `sqlglot`-literal-masked preview — verified by direct read, no live SQL Server with Query Store enabled available to test against real data. |
| Onboarding Engine | **PASS (code-level), NOT LIVE-VERIFIED** | No connection secret ever persisted (verified: `identity.models.OnboardingJob` has no password field at all); publish is blocked while any review item is still pending (`OnboardingJobError`, closing the "silently promoted inference" risk master rule 10 forbids); a real create→discover→review→publish lifecycle passes against a real SQLite target database (`tests/test_api_onboarding.py::TestFullLifecycle`, re-run fresh this session). **Prompt 21 found and closed a real gap** (no rate limit on three routes that open outbound connections to caller-supplied host/port, usable as a TCP-connect reconnaissance oracle even though already admin-gated) — now bounded. Never exercised against a real, external client database by an actual SME reviewer. |
| Semantic Catalog / Governed Metrics | **PASS** | Only a `PUBLISHED`, `CONFIRMED_BUSINESS_TRUTH` catalog entry is ever rendered into a retrieval chunk or a mandatory-metrics prompt block — structurally guaranteed (the rendering functions are only ever called from the publish code path), not a runtime check that could be forgotten. Deterministic-rendering guarantee re-verified this session two ways: `eval/component_benchmark/cases_semantic.py` (6 cases, direct function calls) and `tests/test_full_pipeline_integration.py::TestGovernedMetricEnforcementJourney` (the real graph, `review_metric_conformance_node` actually engaging mid-run). |
| Analytics Correctness | **PASS** | Pure, deterministic, zero-I/O engine (`analytics/engine.py`) — reused directly (not re-implemented) by a new regression harness this session (`eval/component_benchmark/cases_analytics.py`, 8 cases covering all 6 `ResultShape`s) and exercised through the real compiled graph for the first time (Prompt 24). Every finding carries its own `formula`/engine-version metadata, verified by a dedicated case. |
| Recommendation Evidence | **PASS** | "No recommendation without evidence" is enforced in code (`recommendation.engine._finalize_candidate` is the only construction site, and refuses an empty-evidence candidate), re-verified by direct function call (`eval/component_benchmark/cases_recommendations.py`, 5 cases including the SECURITY-category suppression/authorization check) and through the real graph end-to-end (Prompt 24). The `operations` category (root-cause analysis) is a known, disclosed non-firing path in the live graph — it needs a second comparison dataset this single-query pipeline doesn't produce; fully built and tested, just never reached live. |
| AI Accuracy (Text-to-SQL) | **STALE — not re-verified this session** | The only real numbers that exist (`docs/EVALUATION_CURRENT.md`: 92.5% execution accuracy / 42.3% result-set accuracy / 50% final accuracy) are from a live run dated 2026-09-16 — **13 prompts of real pipeline changes ago** (planning, governed metrics, intent classification, analytics, forecasting, recommendations, Query Store, multi-tenancy, scale/perf changes, observability). None of those numbers have been re-measured against current code, because doing so needs a live Ollama instance and a live database, neither available in this sandboxed environment. This is this gate's single most significant **unverified-by-staleness** item outside the three already-named NOT VERIFIED rows — see §4, P1-1. |
| Prompt-Injection Benchmark | **PASS, presumed still valid, not re-run** | The 500-case benchmark (0 critical findings, dated 2026-09-24/25) is still presumed accurate: its own disclosed re-run trigger ("re-run after any change to `agent/sql_validator.py`, `security/injection_patterns.py`, or the orchestrator's prompt construction") was checked against every file touched in Prompts 20-24 — none of those three were modified. Not independently re-run this session (needs live LLM+DB). |
| Scale (single-instance) | **PARTIAL, Phase 0 of 11 only** | Real, measured (Prompt 22, in-process threaded benchmark — not a live k6 run, Docker unavailable): the new per-database concurrency limiter drops p50 from 540.7ms to 0.8ms at saturation. The *real* HTTP-level `k6` baseline (`docs/SCALE_BASELINE.md` Runs 1-2, 2026-09-26) remains Phase 0 only — single node, no distributed rate limiting, no separate inference tier. No multi-replica deployment has ever been exercised. |

## 3. Corrected claims (actively false, not just stale)

Two documents contained claims that are now **incorrect**, not just dated
— each corrected in place this session with a pointer to this document,
rather than left to mislead a future reader:

- `docs/RISK_REGISTER.md` R-001 ("No authentication or per-user
  authorization by default") — **false as of this session.** Four real
  auth modes exist (`none`/`static_token`/`oidc`/`local`), RBAC is
  enforced server-side on every checked route, and tenant isolation is a
  real, server-resolved platform boundary (Prompt 20). `R-001` is marked
  CLOSED in place, with this document cited as the current evidence.
- `docs/PRODUCTION_CHECKLIST.md`'s "neither the UI nor the API has real
  auth" line — **false as of this session**, same evidence as above.
  Corrected in place with a pointer to `docs/AUTHENTICATION.md` and
  `docs/AUTHORIZATION.md` for the real, current picture.

## 4. P0 / P1 / P2 / P3 findings

**P0 (blocking, must resolve before a genuine multi-user production
launch) — unchanged from 2026-09-18, still environment-blocked, not
closed this session:**

| # | Finding | Why still open |
|---|---|---|
| P0-1 | DAST has never been run against this application, at any point in its history. | No staging environment or scanner (OWASP ZAP) available in this sandboxed session — identical constraint every prior session hit. |
| P0-2 | OIDC has never been verified end-to-end against a live Identity Provider. | No live IdP available. Code-level review (algorithm allowlist, audience/issuer validation, JWKS-based signature verification, PKCE flow) was independently re-confirmed by direct read this session, not newly re-verified against a live provider. |
| P0-3 | Malware scanning (`security/malware_scanner.py`) ships disabled by default and has never been run against a real `clamd` daemon. | No ClamAV daemon available. The fail-closed contract (infected/timeout/unreachable/malformed all block) is verified against a mocked socket only. |

**P1 (should resolve before production, or explicitly accept with sign-off):**

| # | Finding | Status this session |
|---|---|---|
| P1-1 | AI accuracy benchmark numbers are 13 prompts stale (§2b). | **Not closed** — needs a live Ollama + database re-run, unavailable here. Documented as the single most actionable follow-up once such an environment exists (`scripts/run_benchmark.py`). |
| P1-2 | No backup/recovery procedure for what this app itself owns (identity DB schema, Chroma index, configuration). | **Closed this session** — see §5 and `docs/DEPLOYMENT.md`'s new "Backup & Recovery" section. |
| P1-3 | No rollback procedure (container image, identity-DB migration, feature-flag-based). | **Closed this session** — see §5 and `docs/DEPLOYMENT.md`'s new "Rollback" section. |
| P1-4 | Two documents contained actively false security claims (§3). | **Closed this session** — corrected in place. |
| P1-5 | Rate limiting remains process-local only; no distributed coordination across replicas. | **Not closed** — needs a shared store (Redis), a real, separate, larger piece of work explicitly out of scope for this pass, same as every prior session's own disclosure. |
| P1-6 | Planning/review/intent/forecast LLM calls (6 of 7 `*_from_llm` functions) are not individually rate-limited — only `generate_sql_from_llm` checks `agent.rate_limit.get_llm_call_limiter` (confirmed by fresh grep this session: exactly one call site). | **Not closed.** Partially mitigated structurally: total LLM calls per question are still bounded by `state["max_retries"]` (3-5 typically), so this is "the limiter undercounts total LLM load," not "load is unbounded" — carried forward from `docs/RISK_REGISTER.md` R-007, not re-scored to a higher severity, not newly discovered. |

**P2 (track, fix opportunistically):**

- `docs/RISK_REGISTER.md`'s remaining 17 open items (R-002 through R-019,
  excluding the now-closed R-001) were **not individually re-triaged**
  this session — each remains presumed accurate at its last-recorded
  status. A full re-triage of all 19 items is a reasonable, bounded
  follow-up, not attempted here to keep this pass's own scope honest.
- The `langgraph` 0.2.x → 1.0 major-version migration that would close
  most of the currently-NOT-REACHABLE dependency CVEs remains
  unscheduled (unchanged since September).
- Container base-image OS-package CVEs (~9 Debian packages) remain not
  independently reachability-verified (unchanged since September).

**P3 (cosmetic / low-value):**

- The graph node-key vs. `_timed_node`-stage-name naming inconsistency
  found during Prompt 24 (`"estimate_cost"` vs. `"estimate_query_cost"`)
  — harmless, documented in `docs/ARCHITECTURE.md`, not renamed.
- `docs/RISK_REGISTER.md` and the two root-level `SECURITY_*.md`
  checklists, while not actively wrong beyond §3's two corrections,
  would benefit from a full refresh pass to reduce the number of
  overlapping "readiness" documents a new reader has to reconcile — a
  documentation-consolidation task, not a production blocker.

## 5. Backup, recovery & rollback

Closed this session — see `docs/DEPLOYMENT.md`'s new "Backup & Recovery"
and "Rollback" sections for the full, operator-facing procedure. Summary:

- **What this app owns and can document a real procedure for:** the
  optional identity Postgres database's *schema* (standard `pg_dump`/
  `pg_restore`, plus `alembic downgrade` for a migration rollback — every
  migration file's `downgrade()` was confirmed to exist by direct read
  this session), the Chroma vector index (fully regenerable from the live
  connected database via `scripts/build_embeddings.py` — never a
  backup/restore target, a rebuild target), and `.env`/configuration
  (version-control or secrets-manager guidance, since this app deliberately
  never persists it anywhere itself).
- **What remains explicitly, correctly out of scope:** the operator's own
  connected business database. This app is a read-only client of it, by
  design (`db.connection.get_read_only_engine`) — it has no opinion on,
  and no mechanism for, backing up data it never writes to.
- **Rollback:** container images are digest-pinned and tag-addressable
  (redeploy the previous tag); `alembic downgrade -1` (or a specific
  revision) for the identity schema; every feature added since Prompt 08
  is behind an `.env` flag defaulting to its pre-existing behavior, so a
  bad new feature can be disabled without a code rollback at all.

## 6. Release checklist

A consolidated, current go/no-go list — supersedes the scattered,
partially-stale items in `docs/PRODUCTION_CHECKLIST.md`/
`SECURITY_PRODUCTION_CHECKLIST.md` for anyone starting a new deployment
today (those two files remain as historical detail, pointed here).

**Blocking:**
- [ ] `DB_USER` is a genuinely read-only database role (`python scripts/test_db_connection.py` reports no write-privilege warning).
- [ ] `ENVIRONMENT=production` is set, and a real auth mode (`oidc` or `local`) is configured — the app refuses to start otherwise.
- [ ] A reverse proxy terminates TLS in front of any deployment reachable beyond a trusted local network.
- [ ] `.env` is not committed, not baked into an image layer, and not exposed via `docker compose config` output shared anywhere.
- [ ] Sensitive columns are classified (`config/sensitive_columns.yaml`) if the connected database carries real PII.
- [ ] If `ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG`/`ENABLE_MEDIA_SEARCH` is on, `MALWARE_SCAN_PROVIDER=clamav` is also configured and reachable (P0-3 — ships disabled by default; silently gets zero scanning otherwise).
- [ ] If OIDC is the intended production auth mode, it has been exercised end-to-end against the real IdP before go-live (P0-2 — never verified in this engagement).
- [ ] A DAST scan has been run against a real staging deployment before the first public launch (P0-1).

**Strongly recommended, not blocking a single-tenant/trusted-network deployment:**
- [ ] Re-run `scripts/run_benchmark.py` against current code with a live Ollama + database before trusting the 2026-09-16 accuracy numbers for a new deployment's expectations (P1-1).
- [ ] If running multiple replicas, move rate limiting to a shared store first (P1-5) — a single-instance deployment is not blocked by this.
- [ ] Review `docs/RISK_REGISTER.md`'s remaining open items against your own deployment's actual risk tolerance (P2).

## 7. Prerequisites for production

- A real identity provider (OIDC) or the local-account system
  (`LOCAL_AUTH_ENABLED=true` + a dedicated Postgres database) — never
  `AUTH_MODE=none`/`static_token` for a genuinely multi-user deployment.
- A genuinely read-only `DB_USER` against the connected business database.
- Alembic applied (`alembic -c identity/alembic.ini upgrade head`) if
  local accounts/chat history/onboarding/semantic catalog/recommendation
  governance are in use.
- A reverse proxy with TLS termination.
- `MALWARE_SCAN_PROVIDER=clamav` with a reachable daemon, if any upload
  feature is enabled.

## 8. Post-release monitoring plan

Building directly on Prompt 23's observability work, not a new mechanism:

- **Per-request tracing:** `grep correlation_id=<id>` across logs
  reconstructs one request's full stage-by-stage trace, including which
  stage set an error category — the first place to look for any reported
  failure. `tenant_id=<id>` narrows this to one tenant's traffic.
- **`GET /metrics/performance`** (admin-gated, tenant-scoped since Prompt
  20): p50/p95/p99 per LangGraph stage, status-code mix, result-cache
  hit rate, database-concurrency-rejection count — poll this on a
  schedule or wire it into whatever external dashboard the deployment
  already has (no built-in Prometheus/OpenTelemetry exporter exists;
  this is a pull-based JSON endpoint today).
- **`GET /health`/`GET /live`:** liveness/readiness, already wired into
  the Docker `HEALTHCHECK` — point an external uptime monitor at these.
- **Watch for `database_busy`/`database_concurrency_rejections`** rising
  — the signal that a configured database's own connection pool is
  undersized for real traffic (Prompt 22).
- **Watch for a sustained rise in `rate_limited`/`metric_definition_not_used`/
  `missing_reference` in the status-code mix** — each points at a
  specific, named stage per §4's P1-6/this gate's own evidence, not a
  generic "something's wrong."
- **Re-run `eval/component_benchmark/`'s regression gate**
  (`python scripts/run_component_benchmark.py --check-regression`,
  or simply `pytest` — it runs on every invocation) after any change to
  `agent/llm_client.py`'s metric rendering, `agent/plan_validator.py`,
  `analytics/engine.py`, or `recommendation/engine.py` — this is the
  fast, deterministic, infrastructure-free regression gate for those
  four domains going forward.
