# Production-Readiness Report

**Date:** 2026-09-01 · **Scope:** Full-repository audit + hardening pass,
Text-to-SQL Dashboard (LangGraph + Ollama + SQLAlchemy + sqlglot + ChromaDB
+ Streamlit + FastAPI) · **Method:** Verified against the running code, a
live database (SQL Server / AdventureWorksDW2025) and live Ollama instance,
and a real captured benchmark run — not design review alone. Every finding
below cites a file, a test, or a measured number.

This report does not assume anything documented elsewhere in the repo
(`CLAUDE.md`, prior commit messages) is accurate — several claims were
found to be inaccurate during this audit and are called out explicitly
below.

---

## Score: 69 / 100

| Category | Score | Notes |
|---|---|---|
| Architecture | 12 / 15 | Excellent modularity/config discipline; scalability capped by embedded Chroma + per-process rate limiting. |
| Security | 15 / 20 | Unusually mature technical controls (validator, secrets, audit log); no auth/authz by design is the ceiling. |
| AI Accuracy | 6 / 15 | 35% final accuracy / 30% result-set accuracy measured live; retry loop observed not reliably self-correcting. |
| Testing | 12 / 15 | 503 passing tests, broad coverage; 3 named modules with real gaps; quality gate was broken at audit start. |
| Reliability | 7 / 10 | Solid timeout/row-cap/retry/fail-open design; no circuit breaker or connection backoff (deliberately deferred). |
| Performance | 6 / 10 | Efficient retrieval/caching engineering; end-to-end latency dominated by local LLM (p95 ≈ 80s), outside this pass's scope. |
| Observability | 3 / 5 | Real structured logging + correlation IDs + health checks + a monitoring script; no metrics/tracing export. |
| Deployment | 3 / 5 | Dockerfile/Compose added, well-designed and documented; **build itself unverified** — no Docker daemon in this environment. |
| Documentation | 5 / 5 | Full doc suite now covers every requested area, cross-linked, grounded in real numbers. |
| **Total** | **69 / 100** | |

**Read this score as:** a well-engineered prototype with unusually mature
security fundamentals, now genuinely equipped for a small-team/internal
production deployment (auth via reverse proxy, Docker, full docs) — but
**not** ready for a use case where wrong SQL answers are costly (AI
accuracy) or where more than a handful of trusted users need independent
identity (no real authn/authz). Both are named, scoped, honest gaps, not
oversights.

---

## What was verified this pass (quality gate)

| Check | Result |
|---|---|
| `pytest` | **503 passed** (491 pre-existing + 12 new API tests), 0 failed |
| `ruff check .` | **Clean** (was 1 error at audit start — unused import) |
| `black --check .` | **Clean** (was clean already) |
| `mypy .` | **Clean, 89 files** (was **141 errors / 20 files** at audit start) |
| Secrets scan (tracked files) | **Clean** — no hardcoded credentials found in source; `.env` correctly gitignored, never committed |
| `docker build` | **Not verified** — Docker daemon unavailable in this sandboxed environment. Dockerfile/Compose syntax validated (`docker compose config` succeeds); the build itself was not executed. Report this as unverified, not passing, when deciding to deploy. |
| Benchmark harness smoke test | `python scripts/run_benchmark.py --limit 5 --check-regression` → **"No regression detected"** — confirms the harness, baseline comparison, and regression detector work end-to-end. **Not** a full accuracy re-measurement (5 fast adversarial cases, no new LLM-generation signal) — see below for why a full re-run wasn't repeated. |
| Full 57-case benchmark | **Not re-run this pass** (no agent/validator logic changed — see `docs/EVALUATION.md`). The existing captured run (`eval/results/run_20260901T151533Z.json`, today, live DB + live Ollama) remains the accurate current measurement: 35.0% final accuracy, 29.6% result-set accuracy, 92.3% execution accuracy, 100% security-rejection accuracy. |
| DB connection failure behavior | Verified by code inspection (unchanged this pass): `db/connection.py`'s `ConnectionErrorCategory` classification, `ConnectionTestResult`, never raises. Live-tested at audit start against the real dev database. |
| LLM (Ollama) failure behavior | Verified by code inspection (unchanged this pass): `agent/llm_client.py`'s `OllamaUnavailableError` wraps connection/timeout/response errors; `GET /health`'s new Ollama check was live-tested (both reachable and simulated-unreachable paths, via mocked test and a live smoke test). |
| Invalid SQL rejection | Verified: `agent/sql_validator.py`'s allowlist + `tests/test_sql_validator.py`/`test_sql_validator_hardening.py` (unchanged, already passing). Live-smoke-tested via the new `/ask` endpoint with a prompt-injection payload — correctly rejected. |
| Oversized/expensive query protection | Verified by code inspection (unchanged this pass): `db/query_cost.py`'s pre-execution EXPLAIN/SHOWPLAN gate + the dual row-cap enforcement in `db/execution.py`. Not re-exercised live this pass (would require a deliberately expensive query against the live reference DB, judged unnecessary given the code and its existing test coverage were both unchanged). |

**One incidental finding during verification, disclosed for transparency:**
running `docker compose config` to validate the new Compose file's syntax
printed the real local `.env` values (including the dev database password)
into this session's tool output — expected Compose behavior (it renders
interpolated config), not a bug in this project, but worth knowing before
running that command in a shared/logged context. Documented in
`docs/DEPLOYMENT.md`'s and `docs/TROUBLESHOOTING.md`'s Docker sections so
it isn't rediscovered the hard way. No secret was written to any file or
committed.

---

## What changed this pass

1. **Fixed the quality gate.** 1 ruff error + 141 mypy errors across 20
   files resolved — a mix of genuine bugs (a loop-variable type-narrowing
   hazard in `eval/runner.py`/`eval/dataset_loader.py`, a real dict-vs-
   dataclass access bug masked by a second bug in `ui/app.py`, an invalid
   `sqlglot.find_all()` call, a `.find_ancestor()` None-safety gap in
   `eval/evaluators.py`) and test-fixture typing noise (fixed via a
   consistent `dataclasses.replace`-based pattern, not scattered
   `type: ignore`s).
2. **Built the REST API that was claimed but never existed.**
   `requirements.txt` and a prior commit message referenced a FastAPI
   `api/` layer with no corresponding code anywhere in the repository —
   this was a real integrity gap, not just an oversight. `api/` now exists,
   reuses `agent.graph.run_agent` and `db.connection.test_connection`
   directly (no duplicated safety logic), has a real health check, an
   optional auth hook, and 12 passing tests (`docs/API.md`).
3. **Made the governance framework real.** `docs/GOVERNANCE.md` and
   `docs/security-changelog.md` referenced `docs/COMPLIANCE.md`,
   `docs/RESPONSIBLE_AI.md`, `docs/RISK_REGISTER.md`, and
   `scripts/monitoring_summary.py` — none existed. All four now do, seeded
   with real findings from this audit, not placeholders.
4. **Wired up dead correlation-ID scaffolding.** `security/audit_log.py`
   had an unused `ContextVar` clearly intended to back per-request
   correlation IDs for the (nonexistent) API layer. Now implemented for
   real and bound by `api/main.py`'s middleware — every security-audit
   event during an API request is now traceable to that request.
5. **Added Docker/Compose** — non-root, pinned, health-checked, documented
   (`docs/DEPLOYMENT.md`), no Kubernetes.
6. **Wrote the full documentation suite** — `docs/API.md`,
   `docs/DEPLOYMENT.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`,
   `docs/EVALUATION.md`, `docs/PRODUCTION_CHECKLIST.md`, plus updates to
   `README.md`, `docs/ARCHITECTURE.md`, `SECURITY.md`.
7. **Left the verified-good core alone.** `agent/`, `db/`, `embeddings/`,
   `security/` core logic (the SQL validator, retry loop, retrieval
   pipeline, secret redaction) was found to already be verifiably
   implemented as documented and was **not** rewritten — the diff for
   those areas is limited to the specific mypy bug fixes named above.

## What was deliberately not done this pass, and why

- **No full authentication/authorization system.** Would turn this from a
  single-user-oriented tool into a genuinely multi-user one, cascading
  into the rate limiter, audit log, and sensitive-column authorization
  design — a real architectural project, not a hardening task. A
  documented reverse-proxy pattern + optional shared-token hook was built
  instead (`docs/RISK_REGISTER.md`'s R-001).
- **No circuit breaker or DB/Ollama connection retry-with-backoff.** Every
  failure mode is already caught and classified (never an unhandled
  crash); the gap is efficiency under *sustained* partial outage, not
  correctness or safety, for a single-process/single-user deployment
  profile. Speculative resilience code without a measured current problem
  to justify it was judged worse than documenting the gap (R-006).
- **No AI-accuracy fixes** (model swap, prompt tuning, retry-diversity).
  Would change agent behavior and require re-validating the entire
  benchmark — explicitly out of this pass's "fix + Docker + docs, skip
  deep refactors" scope. The gap is measured and documented
  (`docs/EVALUATION.md`, `docs/RISK_REGISTER.md`'s R-003) rather than
  silently worked around.
- **No Kubernetes.** Not justified by this project's current shape — see
  `docs/DEPLOYMENT.md`'s "If you outgrow this."

---

## Critical issues

1. **No authentication or per-user authorization on either interface.**
   `docs/RISK_REGISTER.md` R-001. Blocking for any deployment reachable
   beyond a trusted local network without a reverse-proxy auth layer in
   front.
2. **Sensitive-column classification is unpopulated.**
   `config/sensitive_columns.yaml` ships `tables: []` — the enforced
   blocking mechanism protects zero columns until a human classifies the
   connected database's real schema. Blocking for any database carrying
   real PII. R-002.
3. **Measured Text-to-SQL accuracy is low for non-trivial questions.** 25%
   pass rate on both `hard` and `real_world` difficulty tiers in the
   latest 57-case run. Blocking for any use case where a wrong answer
   presented as correct is costly. R-003.

## High-priority issues

4. **`docker build` is unverified.** The Dockerfile/Compose were written
   and syntax-validated but never actually built in this environment —
   confirm it builds and the containers actually start before relying on
   them.
5. **The reference environment's own DB role violates its own
   least-privilege guidance.** `check_write_privileges()` fired a real
   warning against this project's dev database during the audit — a
   concrete instance of exactly the misconfiguration `docs/PRODUCTION_CHECKLIST.md`
   now exists to catch, found in the project's own environment, not a
   hypothetical.
6. **Dependency pins verified against Python 3.14, not the 3.11 this
   project targets and CI runs.** Self-disclosed in `requirements.txt`'s
   own comment; CI passing is reassuring but not the same as a driver-by-
   driver verification. R-004.
7. **No independent security review has ever been performed.** Long-
   standing, already disclosed in `SECURITY.md`; restated here because a
   risk register is more discoverable. R-005.

## Medium-priority issues

8. **The self-correction retry loop doesn't reliably self-correct.** One
   captured trace regenerated byte-identical wrong SQL across 3 of 4
   retries (`temperature=0.0`, insufficiently differentiated retry
   prompt) — a real efficiency/effectiveness gap in the architecture's
   headline feature, not just an accuracy statistic.
9. **`relevant_table_precision` is low (25%).** FK-adjacency bridge
   expansion casts a wide net, inflating prompt size/cost without
   proportional accuracy benefit — already a documented sharp edge in
   `docs/ARCHITECTURE.md`, now quantified.
10. **No circuit breaker / connection retry-backoff.** Real gap, low
    severity at today's single-process scale; would matter more under
    sustained partial outage or multi-replica deployment. R-006.
11. **Rate limiting is per-process, not distributed.** Multiple
    replicas each enforce independent limits — fine for one replica, needs
    a shared store (Redis) before it means what its numbers say across a
    multi-replica deployment. Documented in `docs/DEPLOYMENT.md`.
12. **No metrics/tracing export** (Prometheus, OpenTelemetry). Structured
    logging + correlation IDs + health checks exist and are genuinely
    useful, but there's no numeric metrics endpoint or distributed trace
    for a real ops dashboard to consume beyond log scraping.

## Low-priority improvements

13. `.env.example` was missing `LOG_REDACTION_LEVEL` despite it being a
    real, validated setting — fixed this pass, worth double-checking no
    other settings drift out of sync with `config/settings.py` over time.
14. The `Dockerfile`'s base image isn't digest-pinned (floating
    `python:3.11-slim` tag) — fine for now, worth tightening for a
    stricter reproducibility guarantee later.
15. `docs/User_Guide.pdf` wasn't reviewed as part of this text-focused
    audit (binary format) — worth a pass to confirm it doesn't repeat any
    of the stale/aspirational claims this audit found and corrected
    elsewhere (the phantom API layer, the missing governance docs).

## Technical debt

- **Test-coverage gaps** in `agent/llm_client.py` (prompt construction and
  the injection-resistance framing have no dedicated unit test — only
  indirect coverage via mocked node tests), `agent/error_classification.py`
  (no isolated test of the TIMEOUT/MISSING_REFERENCE/SYNTAX/UNKNOWN
  classification that drives materially different retry routing), and
  `ui/app.py` (zero automated coverage — common for Streamlit but means
  the "Confirm and Run" re-validation safeguard has no regression test).
- **`docs/User_Guide.pdf`** exists alongside the now-much-larger markdown
  doc suite — worth deciding whether it stays as a separate general-
  audience document or gets superseded/merged, so the two don't drift.
- **The eval framework's own `eval/results/` directory accumulates run
  artifacts** (`live_run_output.log`, timestamped JSON pairs) without a
  retention/cleanup policy — not urgent, but will grow unbounded over time.

## Production blockers (do not deploy without addressing)

Directly mirrored in `docs/PRODUCTION_CHECKLIST.md`'s "Blocking" section —
repeated here for the report's own completeness:

- Read-only `DB_USER` verified for the *target* deployment's database (not
  assumed from this audit's findings about the dev environment).
- Sensitive columns classified, if the target database carries real PII.
- `.env`/secrets never committed, baked into an image layer, or exposed
  via a shared `docker compose config` output.
- A real authenticating reverse proxy in front of anything reachable
  beyond a trusted local network.
- `docker build` actually verified to succeed in your real environment.

---

## Recommended V1 roadmap (next production-hardening pass)

1. Classify sensitive columns for the actual target database
   (`config/sensitive_columns.yaml`) — a per-deployment action, not a code
   change, but the single highest-leverage item on this list.
2. Verify `docker build` and a real container run end-to-end; add the
   `msodbcsql` layer if `DB_TYPE=mssql` is in scope.
3. Stand up the reverse-proxy-auth pattern for real (pick a concrete tool
   — `oauth2-proxy` is a reasonable default) rather than leaving it as
   documentation.
4. Verify the pinned DB driver versions against Python 3.11 specifically
   (not just "CI passes"), for whichever `DB_TYPE` is actually in use.
5. Add a dedicated unit test for `agent/llm_client.py`'s prompt
   construction/extraction and `agent/error_classification.py`'s
   classification logic — the two highest-value coverage gaps given their
   role in the security/retry architecture.
6. Try a larger or SQL-specialized model (`sqlcoder`, `duckdb-nsql`) against
   the same benchmark and compare against the 35%/30% baseline — the most
   direct lever on the accuracy problem, and cheap to test given the
   harness already exists.

## Recommended V2 roadmap (larger architectural investments)

1. **Real multi-user authentication/authorization**, if this ever needs to
   serve more than a handful of trusted users — cascades into rate
   limiting (move to a shared store), audit logging (real per-user
   identity), and sensitive-column enforcement (per-user authorization,
   not just app-wide blocking).
2. **Move off the embedded Chroma persist-directory model** to Chroma's
   client-server mode or a hosted vector DB, to unblock true multi-replica
   horizontal scaling of the app/API layer.
3. **Circuit breaker + retry/backoff around Ollama and DB connections**,
   once running as a longer-lived, multi-replica service where a stuck
   dependency's blast radius actually matters.
4. **Retry-loop redesign** to address the observed identical-retry failure
   mode — e.g. a small temperature bump specifically on retry attempts, or
   explicit "try a structurally different approach" prompting.
5. **Metrics/tracing export** (Prometheus + OpenTelemetry) once there's a
   real ops team/dashboard to consume it — not urgent at today's scale
   where structured logs + `scripts/monitoring_summary.py` are sufficient.
6. **If genuinely multi-region/high-scale**, Kubernetes — see
   `docs/DEPLOYMENT.md`'s explicit "If you outgrow this" migration notes.

---

## Cross-references

`SECURITY.md` · `docs/GOVERNANCE.md` · `docs/COMPLIANCE.md` ·
`docs/RESPONSIBLE_AI.md` · `docs/RISK_REGISTER.md` ·
`docs/security-changelog.md` · `docs/API.md` · `docs/DEPLOYMENT.md` ·
`docs/CONFIGURATION.md` · `docs/TROUBLESHOOTING.md` · `docs/EVALUATION.md` ·
`docs/PRODUCTION_CHECKLIST.md` · `docs/ARCHITECTURE.md`
