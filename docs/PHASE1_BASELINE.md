# Phase 1 Baseline — Current-State Audit

**Date:** 2026-09-16
**Scope:** Full repository, inspected against the running code (not just docs/comments), against a live local environment: Ollama (`llama3.1:8b`, confirmed reachable at `http://localhost:11434`) and two live SQL Server databases (`HrAutomationDb`, `AdventureWorksDW2025`, confirmed reachable via `scripts/test_db_connection.py`).
**Method:** Read every module listed in the Phase 1 brief; ran the full pytest suite; ran `pip-audit` and `npm audit`; ran the live Text-to-SQL benchmark (`scripts/run_benchmark.py`) against the current implementation. No code was modified while compiling this document — it captures the state **before** any Step 6/7 fixes in this Phase 1 pass. See `docs/PHASE1_FINAL_REPORT.md` for the before/after delta.

This document is deliberately a *verification* record, not a re-explanation of the architecture — `CLAUDE.md` already documents the design in detail (and was itself spot-checked against the code below, not taken on faith). Where CLAUDE.md/SECURITY.md/`docs/RISK_REGISTER.md` already accurately described something, this document says so and cites the evidence rather than repeating the description.

---

## 1. Existing features — confirmed present, by area

Every item below was located in the running code (file + entry point), not inferred from documentation.

| Area | Status | Entry point |
|---|---|---|
| FastAPI backend | **Present** | `api/main.py` — lifespan-managed singletons (DB engines, Ollama client, compiled graphs), `/ask`, `/execute`, `/schema/*`, `/health`, plus routers in `api/documents.py`, `api/generation.py`, `api/media.py`, `api/media_search.py`, `api/media_library.py`, `api/voice.py` |
| React frontend | **Present** | `frontend/` — Vite+TS+Tailwind, built and served by the same FastAPI process via `StaticFiles` |
| LangGraph agent | **Present** | `agent/graph.py` — 11-node graph: `sanitize_input → classify_followup → retrieve_schema → retrieve_golden_examples → plan_query → generate_sql → review_sql → validate_sql → estimate_cost → execute_sql → generate_insight` |
| SQL generation | **Present** | `agent/llm_client.py::generate_sql_from_llm` — structured system prompt, golden-example/plan/followup blocks, explicit "Security rules" framing untrusted content as data |
| SQL validation | **Present, verified robust** | `agent/sql_validator.py::validate_sql` — sqlglot AST allowlist (see §6 for the specific bypass classes re-tested) |
| Schema retrieval / RAG | **Present** | `embeddings/schema_indexer.py` + `embeddings/retriever.py` — ChromaDB, one collection per configured database, top-k + FK-bridge expansion |
| Golden examples | **Present** | `embeddings/golden_examples.py`, wired via `agent/nodes.py::retrieve_golden_examples_node`, fed by `POST /feedback/golden-example` |
| Query planning | **Present** | `agent/nodes.py::plan_query_node`, gated by `agent/complexity.py`'s signal detection; pass-through (zero LLM calls) for an ordinary question |
| Plan review | **Present** | `agent/nodes.py::review_sql_node` — second LLM call checking generated SQL against the plan |
| Self-correction / retry loop | **Present, verified bounded** | `agent/graph.py`'s conditional edges; capped by `state["max_retries"]` (`compute_max_retries`); a documented, tested `recursion_limit` fix exists for a real prior LangGraph edge case |
| Cost estimation | **Present** | `db/query_cost.py` — non-executing EXPLAIN/SHOWPLAN, fails open on error/timeout |
| Human approval | **Present, verified server-side enforced** | `agent/orchestrator/nodes.py::execute_generation` is the sole call site for the paid IMA API; both the auto path and `POST /generate/confirm` funnel through it and it re-runs its own safety/rate-limit checks regardless of caller |
| Multi-source orchestration | **Present** | `agent/orchestrator/graph.py::run_orchestrated` — a true two-path function (pass-through to `agent.graph.run_agent` when the router is off; a real router→fan-out→synthesis graph when on) |
| Web search | **Present** | `search/web_search.py` — Tavily, fixed non-user-controlled endpoint |
| Media generation | **Present** | `media_gen/` — IMA Studio client, SSRF-hardened download (see §6) |
| Authentication | **Present, optional** | `api/auth.py::verify_api_key` — timing-safe bearer-token check, no-ops when `API_AUTH_TOKEN` is unset (the default) |
| Authorization | **Partially present** | Data-classification gate (`config/sensitive_columns.yaml` + `agent/sql_validator.py::find_restricted_column_references`) exists but had two bypasses — see §6. No RBAC/tenant model exists at all (by design, per SECURITY.md) |
| Rate limiting | **Present** | `agent/rate_limit.py` (LLM-call + session-scoped expensive-source limiters), `api/rate_limit.py` (per-IP-per-action), `api/main.py` (per-IP question-submission) — all real, tested sliding-window implementations, not stubs |
| Uploads | **Present** | `api/documents.py` (PDF, magic-byte validated, bounded read), `api/voice.py` (audio, size+duration capped), `media/ingest.py` (image/video, magic-byte allowlist) |
| SSRF protection | **Present, verified real** | `media_gen/download.py::_validate_download_url` — resolves hostname, checks the actual IP against a private/reserved-range blocklist, HTTPS-only (see §6 for the one disclosed residual gap) |
| Secret management | **Present, verified consistent** | Every secret field in `config/settings.py` is `pydantic.SecretStr`; no `str(secret)` leak found anywhere in the codebase (grepped) |
| Audit logging | **Present, partial coverage** | `security/audit_log.py::log_security_event` — real, structured, correlation-ID-aware, but historically missing from auth failures (fixed in this pass, §7) and several other event classes (still open, see §5) |
| Observability | **Present, minimal** | Correlation-ID middleware (`api/main.py`) is real and works end-to-end; no metrics/tracing (OTel/Prometheus) exists |
| Docker | **Present, well hardened** | `Dockerfile` — non-root user, digest-pinned base images, healthcheck, `docker-compose.yml` binds to `127.0.0.1` by default |
| CI/CD | **Present** | `.github/workflows/ci.yml` — lint (ruff/black/mypy), full pytest suite, report-only `pip-audit`, frontend lint/typecheck/build |
| Tests | **Present, extensive** | 63 test files, 917 tests, all passing pre-Phase-1 (see §2) |
| Benchmark | **Present** | `eval/` framework + `scripts/run_benchmark.py`, `eval/baselines/latest.json` (last recorded 2026-09-01) |
| Documentation | **Present, extensive** | `CLAUDE.md` (95KB), `SECURITY.md` (42KB), `docs/RISK_REGISTER.md`, `docs/GOVERNANCE.md`, `docs/PRODUCTION_READINESS_REPORT.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/API.md` |

**Conclusion for Step 1's "do not assume missing" instruction:** every capability named in the Phase 1 brief already exists in the codebase. Nothing required building from scratch. The gaps found (below) are all either (a) a partial/incomplete implementation of an existing control, (b) a control that exists but is optional/off by default, or (c) a documented, self-disclosed limitation with no code-level mitigation yet.

---

## 2. Existing tests — baseline run

```
$ pytest -q
917 passed, 0 failed, 0 skipped, 0 errors, 39 warnings, 24.68s
```

No coverage plugin is configured (`pytest-cov` is not in `requirements.txt`); no coverage percentage is available. All 63 test files passed with **zero genuine failures** — the baseline test suite was fully green before this Phase 1 pass touched anything. One uncommitted, pre-existing working-tree change was found (`tests/test_orchestrator.py`, +6 lines) that keeps a test fixture's "SQL-only available" baseline in sync with `enable_media_search`'s actual default (`true` — see §4); this predates this session and was left as-is (correct, not something to revert).

`ruff check .`: 16 errors (mostly import-order / unused-import in two test files: `tests/test_rag_ingestion.py`, `tests/test_media_ingest.py`), 15 auto-fixable. Pre-existing, not security-relevant, not touched in this pass (out of scope for "no large rewrite").

`black --check .`: 9 files would be reformatted (`moderation/`, `media/ingest.py`, `rag/ingestion.py`, `scripts/build_media_index.py`, two test files). Pre-existing style drift, not touched.

`mypy .`: not re-run as part of this baseline capture (see `docs/PHASE1_FINAL_REPORT.md` for the Step 8 final run, which does include it).

---

## 3. Current benchmark numbers

See `docs/EVALUATION_CURRENT.md` for the full current run and BASELINE→CURRENT comparison. The previously recorded baseline (`eval/baselines/latest.json`, run `run_20260901T151533Z`, model `llama3.1:8b`, database `AdventureWorksDW2025`, 57 cases):

| Metric | Baseline (2026-09-01) |
|---|---|
| SQL execution accuracy | 92.3% |
| Result-set accuracy | 29.6% |
| Final accuracy | 35.0% |
| Schema retrieval recall | 82.2% |
| Security rejection accuracy | 100% |
| Average latency | 33.3s |
| P95 latency | 79.5s |
| First-attempt success rate | 100% |
| Retry success rate | 66.7% |

This is the number Step 3/4 measure against with a fresh live run, not something taken on faith.

---

## 4. Current dependency status (pre-fix)

Full detail in `docs/DEPENDENCY_SECURITY.md`. Summary of the state as found:

- `pip-audit -r requirements.txt --desc`: **34 unique advisories across 9 pinned packages** — `langgraph` (2), `langgraph-checkpoint`/`langgraph-sdk`/`langchain-core` (transitive, 6 combined), `chromadb` (4), `pillow` (18), `python-dotenv` (1), `pytest`/`black` (3, dev-tooling only). This is down from the `docs/RISK_REGISTER.md`-recorded "65 advisories / 10 packages" (2026-09-13) — the drop is consistent with that entry's own prediction that removing `streamlit` from `requirements.txt` (already done before this session) would shrink the backlog.
- `npm audit` (frontend): **0 vulnerabilities** — clean.
- GitHub Actions: uses tag-pinned (not SHA-pinned) official actions (`actions/checkout@v4`, `actions/setup-python@v5`, `actions/setout-node@v4`) — standard, low-risk practice, not SHA-pinned like the Dockerfile's base images are.
- Docker base images: digest-pinned (`python:3.11-slim@sha256:...`, `node:22-slim@sha256:...`) — no OS-package-level (apt) vulnerability scan was run against the built image in this pass (no Trivy/Grype available in this environment); flagged as a Phase 2 follow-up.
- A stale `streamlit==1.41.1` package was found installed in the local `.venv` but is **not** in `requirements.txt` — a leftover from before Streamlit was removed from the project (per `CLAUDE.md`'s documented history), irrelevant to the actual deployed dependency set (Docker only installs from `requirements.txt`), not a finding requiring remediation.

---

## 5. Current known limitations (self-disclosed, verified still accurate)

`docs/RISK_REGISTER.md` was spot-checked against the current code; every entry checked was still accurate as of this session:

- **R-008 (Critical for multi-tenant)** — no independent authorization layer between the orchestrator's LLM routing decision and actual tool/source execution. Verified still true: `agent/orchestrator/nodes.py::router_node` → `route_after_router` fans out directly with no policy check in between.
- **R-010** — dependency backlog (see §4 above; the count has changed, the underlying gap is the same).
- **R-011** — session-scoped expensive-source cost ceiling is reset by minting a fresh `session_id`. Verified still true.
- **R-006** — no circuit breaker around Ollama/Azure Content Safety/Tavily/IMA. Verified still true (grepped for `CircuitBreaker`/`circuit_breaker` repo-wide — zero real implementations).
- **R-007** — `plan_query_node`/`review_sql_node`'s LLM calls bypass the process-wide LLM-call rate limiter (only `generate_sql_node` checks it). Verified still true.

No entry in the risk register was found to be stale or already resolved without the register being updated.

---

## 6. Critical security review — verified (not duplicated)

Per the Phase 1 brief's explicit instruction to verify rather than rebuild, each item was checked against the current code:

| Control | Verdict | Evidence |
|---|---|---|
| SQL injection (user→SQL path) | **Adequate** | `agent/sql_validator.py::validate_sql` — AST-based allowlist (single SELECT/UNION/EXCEPT/INTERSECT), full-tree walk (not root-only) catches data-modifying CTEs, `SELECT...INTO`, dangerous functions (AST + raw-text fallback for dialect-qualified calls). Re-tested against stacked queries, comment-hidden statements, writable CTEs, dangerous functions — no bypass found. |
| SQL injection (app's own infra tables) | **Adequate** | `rag/store.py`, `moderation/store.py` — parameterized `text()` queries throughout; only f-string interpolation found was of fixed internal constants, never user input. |
| Restricted-column enforcement (data governance, not injection) | **Two bypasses found and fixed in this pass** | See §7 below (AUTHZ-01, AUTHZ-02). |
| Prompt injection / indirect prompt injection | **Adequate, one gap found and fixed** | Every system prompt in `agent/llm_client.py`, `rag/graph.py`, `agent/orchestrator/nodes.py` explicitly frames retrieved/external content as untrusted data, not instructions — genuine prompt text, not just a comment claim. One real gap: `conversation_history` bypassed `agent/input_guard.py`'s sanitization entirely (not fixed in this pass — flagged as a Phase 2 item, see `docs/PHASE1_FINAL_REPORT.md`). |
| RAG poisoning | **Adequate** | `rag/graph.py::_generate_node` never passes a sensitivity-flagged chunk's text to the LLM (verified: the `call_ollama` call only happens in the non-restricted branch); `agent/nodes.py` has a dedicated RAG-poisoning regex scan over retrieved schema/sample-value content, detection-only by design. |
| SSRF | **Adequate, one disclosed residual gap** | `media_gen/download.py::_validate_download_url` genuinely resolves and checks the IP (not just the URL string) against a comprehensive blocklist before fetching. The module's own docstring honestly discloses a DNS-rebinding TOCTOU window (validated IP isn't pinned into the actual fetch) — not closed in this pass (would require a custom transport/session; flagged for Phase 2). `search/web_search.py` has no SSRF surface at all (fixed, non-user-controlled endpoint). |
| Path traversal | **Adequate** | `api/media_library.py::_resolve_safe_path` — `.resolve()` + `is_relative_to()` containment check before any file is opened; verified real, not superficial. |
| Secret leakage (logs/errors/frontend) | **Adequate for secrets specifically; a related gap (PII in questions/SQL, not "secrets") already tracked, not addressed in this pass** | Every secret field is `SecretStr`; `.get_secret_value()` is the only way to get the real value; `agent.exceptions.AgentError.safe_message` + two global FastAPI exception handlers guarantee no raw exception text reaches a response. Question text and generated SQL (which can carry PII, not credentials) are logged verbatim at INFO — a real but different-category gap, out of scope for this pass's "critical application security" P0 bar, flagged for Phase 2. |
| Unsafe file uploads | **Adequate** | PDF/audio/image/video uploads all validate by magic bytes (not extension/Content-Type), size-cap via bounded `.read()`, not full-buffer-then-check. |
| Command injection | **No instance found** | Grepped for `subprocess`, `os.system`, `shell=True` — no user/LLM-influenced value reaches a shell command anywhere in the reviewed code paths. |
| Unsafe deserialization | **No instance found in this app's own code** | No `pickle.loads`/`eval`/`yaml.load` (unsafe loader) on untrusted input found. The one deserialization-shaped CVE in the dependency scan (`langgraph`'s msgpack checkpoint deserialization) is not reachable — this app configures no LangGraph checkpointer at all (grepped `agent/` for `Checkpointer`/`checkpoint` — zero hits outside tests), so there is no checkpoint store to attack. |
| Resource exhaustion | **One real gap found and fixed in this pass** | See §7 below (API-02: unbounded rate-limiter dictionaries). |
| LLM abuse (token/cost) | **Adequate, with known gaps already tracked** | Retry loop genuinely bounded (`max_retries` + recursion-limit fix); real rate limiters exist; R-007/R-011 (above) are the known, already-tracked residual gaps. |
| External API abuse (Tavily/IMA/Azure) | **Adequate** | Media generation requires human approval (server-enforced, verified — see §1); a session-scoped cost ceiling exists (R-011's caveat noted above); the media-generation content-policy check is a self-disclosed weak keyword denylist (`agent/orchestrator/nodes.py::_basic_prompt_safety_check`) — not fixed in this pass (would need routing through the existing `moderation/gate.py` machinery, a larger change than this pass's scope; flagged for Phase 2). |

---

## 7. P0 fixes applied in this Phase 1 pass

Full detail, before/after, and file list in `docs/PHASE1_FINAL_REPORT.md`. Summary:

1. **AUTHZ-01 (High) — `SELECT *` bypassed restricted-column enforcement.** `agent/sql_validator.py::find_restricted_column_references` only matched named `exp.Column` nodes; a wildcard projection never mentioned a restricted column by name and sailed through unflagged. Fixed: a wildcard projection now flags every restricted column of every table the statement references.
2. **AUTHZ-02 (High) — restricted-column check was scoped to the RAG-retrieved subset, not the real query.** The old check required the restricted table to be part of `known_tables` (this attempt's retrieved schema context), so a restricted table reached via a join/subquery the retriever didn't happen to surface for that specific attempt was never checked. Fixed: matching is now grounded in the tables the SQL statement's own parsed `FROM`/`JOIN` clauses actually reference.
3. **MON-02 (High) — authentication failures were never audit-logged.** `api/auth.py::verify_api_key` now logs a structured `auth_failed` event (reason + client IP, never the attempted token value) on every 401.
4. **API-02 (Medium-High) — three rate-limiter dictionaries grew without bound.** `api/main.py`'s per-IP limiter, `api/rate_limit.py`'s per-IP-per-action limiter, and `agent/rate_limit.py`'s per-session expensive-source limiter (keyed by a client-supplied, unauthenticated `session_id`) never evicted entries — a real memory-exhaustion vector requiring nothing more than sending requests with a different key each time. Fixed with a shared `BoundedLimiterCache` (LRU eviction, `agent/rate_limit.py`).
5. **Dependency hygiene** — `pillow` 11.3.0→12.3.0 (closes ~18 advisories, none reachable through this app's own ingestion path but a safe, same-API bump) and `python-dotenv` 1.0.1→1.2.2 (closes a symlink-following file-overwrite CVE this app doesn't reach either, since it never calls `set_key()`/`unset_key()`, but again a safe, low-risk bump).

All five changes are covered by new, passing regression tests (`tests/test_restricted_column_bypass.py`, `tests/test_api_auth.py`, additions to `tests/test_rate_limit.py`) and verified against a full, green rerun of the pre-existing 917-test suite (936 passing after these additions, zero regressions).

Not fixed in this pass (deliberately, to respect "no large rewrite" / "preserve existing functionality"): R-008 (needs a real identity/authz system), the `conversation_history` sanitization gap, the `_basic_prompt_safety_check` weak denylist, the SSRF DNS-rebinding TOCTOU window, and PII-in-logs. Each requires either new infrastructure (identity system, a circuit-breaker library, a distributed rate-limit store) or a properly scoped, separately-reviewed change rather than a same-pass patch. See `docs/PHASE1_FINAL_REPORT.md`'s "Phase 2 recommendations" for the prioritized list.
