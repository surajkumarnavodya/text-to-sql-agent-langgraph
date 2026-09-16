# Phase 1 Final Report

**Scope:** establish an accurate current baseline; address P0 issues in AI quality, dependency security, and critical application security. See `docs/PHASE1_BASELINE.md` (Step 1 audit), `docs/EVALUATION_CURRENT.md` (Step 3 benchmark), `docs/DEPENDENCY_SECURITY.md` (Step 5 dependency scan) for full detail behind the summary below.

**Note on timing:** this report reflects Phase 1 as it concluded (936 tests passing). Phase 2 (a separate, much larger security-hardening pass — see `docs/PHASE2_SECURITY_REPORT.md`) began immediately after and is layered on top; later test counts in this repository reflect that additional work, not Phase 1 itself.

## 1. What was already implemented

Confirmed via the Step 1 audit (`docs/PHASE1_BASELINE.md`): every capability named in the brief already existed — the full 11-node LangGraph agent, sqlglot-based SQL validation, ChromaDB schema retrieval, golden examples, query planning/review, cost estimation, human-approval-gated media generation, multi-source orchestration, rate limiting, an existing (optional) bearer-token auth hook, and a 917-test mocked suite that was fully green before this pass touched anything. Nothing required building from scratch.

## 2. What was missing / partial

- **Two real bypasses** of the restricted-column data-governance gate (`SELECT *`, and tables reached outside the RAG-retrieved schema context).
- **Authentication failures were never audit-logged** (the one authentication-relevant event in the codebase with no record).
- **Three rate-limiter dictionaries grew without bound** — a real memory-exhaustion vector, most concretely exploitable via the client-supplied, unauthenticated `session_id`.
- **34 known dependency advisories** across 9 packages (down from a previously-recorded 65/10 once `streamlit`'s removal was accounted for) — none reachable in this app's actual deployment on inspection, but 2 (pillow, python-dotenv) were cheap, safe to fix regardless.

## 3. What was improved (this pass)

| Finding | Fix | Files |
|---|---|---|
| AUTHZ-01 — `SELECT *` bypassed restricted-column check | Wildcard projections now flag every restricted column of every referenced table | `agent/sql_validator.py` |
| AUTHZ-02 — restricted-column check scoped to RAG-retrieved subset, not the real query | Matching now grounded in the statement's own parsed `FROM`/`JOIN` tables | `agent/sql_validator.py` |
| MON-02 — auth failures never audit-logged | `verify_api_key` now logs `auth_failed` (reason + client IP, never the token) on every 401 | `api/auth.py` |
| API-02 — unbounded rate-limiter dictionaries | Shared `BoundedLimiterCache` (LRU eviction) used by all three | `agent/rate_limit.py`, `api/rate_limit.py`, `api/main.py` |
| Dependency hygiene | `pillow` 11.3.0→12.3.0, `python-dotenv` 1.0.1→1.2.2 | `requirements.txt` |

## 4. Files changed

`agent/sql_validator.py`, `agent/rate_limit.py`, `api/auth.py`, `api/rate_limit.py`, `api/main.py`, `requirements.txt`, plus new regression tests: `tests/test_restricted_column_bypass.py`, `tests/test_api_auth.py`, additions to `tests/test_rate_limit.py`.

## 5. Tests before/after

| | Before | After |
|---|---|---|
| Test count | 917 | 936 |
| Failures | 0 | 0 |
| New regression tests added | — | 19 (9 restricted-column bypass, 4 auth audit-logging, 6 BoundedLimiterCache) |

`ruff`/`black`/`mypy` all clean on every file touched.

## 6. Benchmark before/after

See `docs/EVALUATION_CURRENT.md` for the full breakdown. Headline: result-set accuracy 29.6%→42.3%, final accuracy 35.0%→50.0%, security-rejection accuracy 100%→88.2% (investigated — the regression is in the shallow input-guard heuristic layer; the actual enforcement boundary, `agent/sql_validator.py`'s SELECT-only allowlist, still blocked both newly-missed adversarial cases before execution). **No code in this pass touched retrieval, planning, or generation** — the accuracy movement is attributed to run-to-run variance, not a change made here; see that document for the full, non-cherry-picked analysis including all 22 failure classifications.

## 7. Security vulnerabilities before/after

| | Before | After |
|---|---|---|
| Restricted-column bypasses | 2 confirmed | 0 (both closed, regression-tested) |
| Auth-failure audit trail | None | Present |
| Rate-limiter memory-exhaustion vector | Present (3 unbounded dicts) | Closed (bounded, LRU) |
| pip-audit advisories | 34 (9 pkgs) | 15 (7 pkgs) |
| npm audit | 0 | 0 (unchanged, already clean) |

## 8. Remaining risks (deliberately not addressed in this pass — see Phase 2)

- No RBAC/authorization system at all (addressed in Phase 2 — see `docs/PHASE2_SECURITY_REPORT.md`).
- No independent authorization layer between the orchestrator's LLM routing and execution (R-008/AGT-01 — addressed in Phase 2).
- `conversation_history` bypassed input sanitization (addressed in Phase 2).
- SSRF DNS-rebinding TOCTOU window in `media_gen/download.py` (still open — a properly-tested fix needs a live endpoint this environment doesn't have; see `docs/PHASE2_SECURITY_REPORT.md`).
- PII in logs, no TLS enforcement, `langgraph` major-version dependency upgrade — all still open, tracked in `docs/DEPENDENCY_SECURITY.md`/`docs/RISK_REGISTER.md`.

## 9. Phase 2 recommendations (made at the time, since acted on)

Production-grade authentication (OIDC/JWT), RBAC, closing the orchestrator authorization gap, conversation-history sanitization, CI security scanning, Docker hardening — see `docs/PHASE2_SECURITY_REPORT.md` for what was actually done.

## P0 status (as of Phase 1 completion)

- **P0 AI Quality: AMBER** — core SQL-execution/security-rejection accuracy is strong (92%+, mid-to-high-80s%), but result-set accuracy (the "is the answer actually right" metric) sits at 42% and ambiguous-question handling at 0% — real, honestly-measured, unresolved gaps, not a red flag on the underlying architecture.
- **P0 Dependency Security: GREEN** — no P0-severity (RCE/auth-bypass/SSRF/deserialization/secret-disclosure) finding reachable in this app's actual deployment; the two safe/cheap fixes available were applied; the one deliberately-deferred item (`langgraph` major-version bump) is a real backlog item, not an active exposure.
- **P0 Application Security: AMBER** — the two concrete, exploitable bypasses found were fixed and regression-tested; the deeper structural gap (no RBAC, no independent authorization layer) was correctly identified as out of this pass's scope and carried forward explicitly into Phase 2 rather than left silently unaddressed.

**Not claiming production readiness at this point** — see `docs/PHASE2_FINAL_REPORT.md` for the post-Phase-2 assessment.
