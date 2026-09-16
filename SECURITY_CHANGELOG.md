# Security Changelog — 2026 Phase 3

Chronological record of this pass's audit findings and fixes. See `SECURITY_BASELINE.md` for the full audit trail with file:line evidence, and `SECURITY_FINAL_REPORT.md` for the structured scorecard.

## Audit (no code changes)

- Audited authN/authZ, SQL validator/DB permissions, file upload/moderation/SSRF, API/infra hardening (CORS/headers/rate-limits/CI/Docker/secrets), RAG authorization, prompt injection framing, DB execution timeout/row-cap enforcement, sensitive-column config, frontend XSS/token storage, agent/tool execution limits. Full findings in `SECURITY_BASELINE.md`.
- Confirmed via direct code reading (not doc claims) that this codebase's prior "Phase 1"/"Phase 2" security reviews delivered real fixes: AUTHZ-01 (wildcard-projection bypass of the restricted-column gate), AUTHZ-02 (cross-table-scope bypass), AGT-01/R-008 (LLM router decision alone deciding multi-source access), LLM-01 (conversation history bypassing input sanitization).

## Fixed — P0

- **RAG per-document access control.** `rag.store.DocumentRecord` gained `restricted_roles` (operator-set, role-based) and `uploaded_by` (audit-trail only). `rag/graph.py::_generate_node` blocks summarizing a role-restricted chunk into an answer unless the caller holds a matching role, checked before the LLM sees the content. `api/documents.py`'s download route enforces the same check on raw bytes. Threaded `caller_roles` through `rag/graph.py::run_rag` and `agent/orchestrator/nodes.py::_run_rag_node`. New: `tests/test_rag_graph.py` (10 tests, first dedicated coverage for `_generate_node`), extended `tests/test_rag_pdf_download.py` and `tests/test_api_documents.py`.
  - Deliberately does **not** add per-uploader document isolation — confirmed this app is a shared knowledge base by design; see `docs/AUTHORIZATION.md`'s updated "Per-document role restriction" section.

- **Frontend OIDC login.** New `frontend/src/lib/auth.ts` (OIDC config + `UserManager` singleton, in-memory-only token storage via a custom `InMemoryWebStorage`), `frontend/src/store/authStore.ts` (Zustand store: `initialize`/`signIn`/`signOut`/`completeSignIn`, silent-renew event handling), `frontend/src/pages/AuthCallback.tsx`, `frontend/src/components/auth/AuthGate.tsx`. Wired into `App.tsx` (new `/auth/callback` route, `AuthGate` wraps `AppShell`) and `AppShell.tsx` (sign-out button). `frontend/src/lib/api.ts`'s `getBearerToken()` (moved to `authStore.ts`) now mirrors `api/auth.py`'s own OIDC-primary/static-token-fallback dispatch order. New dependency: `oidc-client-ts`. `.env.example` gained the `VITE_OIDC_*` section (and the previously entirely-missing backend `OIDC_*`/`ENVIRONMENT` section). i18n keys added to all 5 locales (`auth.*`).
  - `api/main.py`'s new CSP derives `frame-src` from `OIDC_ISSUER` — required for OIDC silent-renew's hidden iframe, which a `frame-src 'self'` default would have silently blocked.
  - Not end-to-end verified against a live IdP (see final report).

- **DB write-privilege startup check.** `api/main.py::_enforce_database_write_privileges` (extracted as a standalone, unit-testable function) calls the pre-existing but previously-unused `db.connection.check_write_privileges` for every configured database at startup. Raises `ConfigurationError` (refuses to start) if `ENVIRONMENT=production` and any database's connected role appears to hold write privileges; warns otherwise. Emits a new `critical`-severity audit event, `db_write_privileges_detected`. New: `tests/test_startup_write_privilege_enforcement.py` (7 tests).

- **CI security gate triage.** Ran bandit directly against this codebase for the first time (not previously run in this engagement). 10 findings, all resolved — see "Fixed — P1/P2" and "Suppressed" below. `.github/workflows/ci.yml` itself not yet edited (gitleaks/Trivy could not be run to establish a comparable clean baseline in this environment) — see final report's remaining risks.

## Fixed — P1

- **SQL validator: system-catalog access.** `agent/sql_validator.py` gained `_find_system_catalog_reference` (a new `system_catalog_access` violation type, added to `SAFETY_VIOLATION_TYPES`) — blocks `information_schema`/`pg_catalog`/mysql internal schemas/mssql `sys`/Oracle `ALL_*`/`DBA_*`/`USER_*`/`V$`/`GV$` references. Deliberately uses curated suffix matching for Oracle (not a blind prefix) to avoid false-positiving on ordinary business tables like `user_accounts`. New: 20 tests in `tests/test_sql_validator_hardening.py`, including explicit false-positive regression tests.

- **SSRF: redirect re-validation.** `media_gen/download.py::download_media_bytes` now calls `requests.get(..., allow_redirects=False)` and manually follows redirects (max 5 hops), re-running `_validate_download_url` against each `Location` header before following it. New: 6 tests in `tests/test_media_gen.py`.

- **Security headers middleware.** New `api/main.py::_add_security_headers` — `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` (microphone allowed for voice mode, everything else denied), `Content-Security-Policy`. Configurable via new `Settings.enable_security_headers`/`content_security_policy`/`hsts_max_age_seconds`. New: `tests/test_security_headers.py` (16 tests).

## Fixed — P2

- **CORS wildcard rejection.** `config/settings.py::_reject_wildcard_cors_origin` refuses to start if `CORS_ALLOWED_ORIGINS` contains `"*"` (the app's `CORSMiddleware` always sets `allow_credentials=True`, and that combination is meaningless/browser-rejected anyway — now caught at config time instead of relying on browser behavior). New: 4 tests in `tests/test_security_headers.py`.

- **Dockerfile stale-comment fix.** A comment claimed `build-essential` was removed post-`pip install` to reduce image size; no such removal step existed. Added the actual `apt-get purge -y --auto-remove build-essential` in the same `RUN` layer as the `pip install` (a later layer would not shrink the image).

## SAST findings (bandit) — first run, all resolved

- **Fixed (real issues):**
  - `db/query_cost.py`: swapped `xml.etree.ElementTree` for `defusedxml.ElementTree` (XXE/billion-laughs hardening for MSSQL `SHOWPLAN_XML` parsing). Added `defusedxml==0.7.1` to `requirements.txt` (was present only transitively).
  - `agent/nodes.py` (×3), `api/main.py` (×1): converted `assert`-based internal invariant checks to explicit `raise RuntimeError(...)` — `assert` is stripped entirely under `python -O`.
- **Suppressed (confirmed false positives, justified inline):**
  - `agent/llm_client.py`: an f-string building an LLM system-prompt string, not a SQL query — bandit's B608 heuristic matched on the word "SQL" nearby.
  - `db/value_sampling.py`: SQL built from identifiers already passed through the dialect's own `identifier_preparer.quote()`.
  - `rag/store.py` (×2): SQL built with a fixed module-level constant (`_VECTOR_CAST`) and every real value bound as a parameter. Restructured from multi-line triple-quoted f-strings to single-line concatenated literals so the `# nosec` marker could sit outside the string content (a `# nosec` inside a multi-line f-string would have become literal SQL text).
- Re-run confirms zero remaining bandit findings of any severity.

## Dependency scans (run, not remediated)

- `pip-audit`: 24 known vulnerabilities across 7 backend packages (`langgraph`, `chromadb`, `pytest`, `black`, `langchain-core`, `langgraph-checkpoint`, `langgraph-sdk`) — every one requires a major version bump; none remediated in this pass (see final report's Remaining Risks for why).
- `npm audit` (frontend): 0 vulnerabilities.

## Documentation updated

- `docs/AUTHENTICATION.md`: new "Frontend OIDC login (2026 Phase 3)" section.
- `docs/AUTHORIZATION.md`: new "Per-document role restriction (2026 Phase 3)" section; "Known limitations" revised to distinguish the deliberate no-ownership design from the new role-restriction capability.
- `.env.example`: added the previously entirely-missing backend `OIDC_*`/`ENVIRONMENT` section, plus the new frontend `VITE_OIDC_*` section.
- `SECURITY_BASELINE.md`: full audit trail (round 1 + round 2 + this fix pass's priority-summary status update).

## Test suite

- Started this pass at 1033 tests (per the prior session's commit); ends at **1112 tests, all passing**. Net new: 79 tests across `tests/test_startup_write_privilege_enforcement.py` (new), `tests/test_sql_validator_hardening.py` (extended), `tests/test_media_gen.py` (extended), `tests/test_security_headers.py` (new), `tests/test_rag_graph.py` (new), `tests/test_rag_pdf_download.py` (extended), `tests/test_orchestrator.py` (extended), `tests/test_api_documents.py` (extended).
- `mypy`: clean on every file touched this pass (4 pre-existing errors in `moderation/gate.py`/`media/ingest.py`, confirmed unmodified by this pass).
- `ruff`: clean on every file touched this pass.
- Frontend: `tsc --noEmit`, `oxlint`, `npm run build` all clean.
