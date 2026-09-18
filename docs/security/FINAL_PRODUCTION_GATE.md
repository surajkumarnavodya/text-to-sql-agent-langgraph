# Final Production Gate — Release Matrix

**Date:** 2026-09-18. **Method:** every row below is backed by either (a)
an actually-executed test/scanner run this session (command shown in
`docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`'s evidence
appendix), or (b) a direct source-code read with file:line citation. No
row is marked PASS on the strength of a prior document's claim alone —
several rows below were independently re-verified this session
specifically because "previous documentation is evidence to investigate,
not proof" was this gate's own explicit rule.

**Status legend:** `PASS` · `FAIL` · `PARTIAL` · `NOT VERIFIED` · `N/A`

| Domain | Status | Evidence | Blocker? |
|---|---|---|---|
| Authentication | PASS | 4 modes (`none`/`static_token`/`oidc`/`local`), fail-closed in production (`config/settings.py::_require_identity_in_production` — refuses to start if `ENVIRONMENT=production` and `auth_mode=="none"`, verified by direct read). `tests/test_oidc.py`, `tests/test_api_auth.py`, `tests/test_identity_security.py` all pass this session. | No |
| JWT | PASS | Server-side algorithm allowlist (never trusts the token's own `alg`), mandatory audience validation, bounded clock skew, JWKS-based signature verification (`security/oidc.py`, read this session). `tests/test_oidc.py` covers forged signature, `alg=none`, wrong issuer/audience, expired/malformed tokens — all pass. | No |
| OIDC | **PARTIAL** | Code-complete, passes `tsc`/`oxlint`/`npm run build`, follows `oidc-client-ts`'s documented PKCE flow (per `docs/AUTHENTICATION.md`, independently re-confirmed this session, not just cited). **Never exercised against a real Identity Provider end-to-end** (no live IdP available in this environment) — see `docs/security/OIDC_E2E_TEST.md`. Per this gate's own Phase 4 instruction: do not mark PASS from source inspection alone. | **Yes, if OIDC is the deployer's intended production auth mode** (the app's own recommended mode for genuine multi-user production use, per `docs/RISK_REGISTER.md` R-001) |
| RBAC | PASS | 15 permissions, 4 roles, fail-closed on unrecognized role. `tests/test_api_authz.py::TestVerticalPrivilegeEscalation`/`TestHorizontalPrivilegeEscalation`/`TestMissingAndInvalidRoles` (class names independently confirmed present via `grep` this session, not assumed from docs) all pass. | No |
| IDOR | PASS | `tests/test_api_chat_history.py` (`test_user_a_cannot_list_user_b_conversations`, `test_user_a_cannot_open_user_b_conversation_by_id`, `test_changing_the_url_id_cannot_bypass_ownership`), `tests/test_api_documents.py` (sensitivity/restricted-role gates) — all pass. | No |
| SQL Security | PASS | AST-based (sqlglot) allowlist, not a blocklist — `agent/sql_validator.py`. Tested against DROP/DELETE/UPDATE/INSERT/multi-statement/data-modifying-CTE/UNION/`SELECT *`/`table.*`/system-catalog/`information_schema`/dangerous-function/nested-aggregate/homoglyph/null-byte shapes (`tests/test_sql_validator.py`, `tests/test_sql_validator_hardening.py`, `tests/test_restricted_column_bypass.py`) — all pass. Read-only DB engine + row cap + query timeout independently confirmed in `db/execution.py`. | No |
| Prompt Injection | PASS (structural, gap disclosed) | LLM output is never trusted directly — the SQL validator is the real boundary, confirmed to hold even under a simulated fully-hijacked model response (`tests/test_adversarial_input.py::TestPoisonedValueCannotBypassTheValidatorEvenIfModelIsTricked`). The regex detection *layer* has confirmed, disclosed blind spots (Base64/URL-encoded phrasing bypasses it entirely — `tests/security/test_prompt_injection_multilingual_and_encoded.py`, added and verified this engagement) but this was never the security boundary in the first place, and the boundary that matters (SQL validation) was independently re-confirmed to hold regardless. | No |
| RAG Security | PASS (with disclosed scope limit) | Sensitivity-category + `restricted_roles` gates enforced before generation and on download (`tests/test_rag_graph.py`, `tests/test_api_documents.py`) — pass. No per-tenant document isolation, but this is a confirmed **deliberate design choice** (shared knowledge base), not an unverified gap — see `docs/AUTHORIZATION.md`'s own "Known limitations." | No |
| File Upload | PASS | Magic-byte validation (not extension-trust), bounded reads, PDF page-count cap, mandatory content-moderation gate before storage. `tests/test_api_documents.py`, `tests/test_media_ingest.py`, `tests/security/test_malware_scanner_gate.py` — all pass. | No |
| Malware Scanning | **PARTIAL** | `security/malware_scanner.py` (ClamAV `INSTREAM` protocol) is real, fail-closed-when-enabled (infected/timeout/unreachable/malformed all block), and unit-tested against a mocked socket (`tests/security/test_malware_scanner_gate.py`, 18 tests, pass). **Never run against a real `clamd` daemon** (none available in this environment) and **defaults to `disabled`** (`Settings.malware_scan_provider`) — an operator who enables document/media upload without separately configuring `MALWARE_SCAN_PROVIDER=clamav` gets zero malware scanning. Per this gate's own Phase 10 instruction: do not call this production-ready. | **Yes, for any deployment enabling `ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG`/`ENABLE_MEDIA_SEARCH` without also configuring a real scanner** |
| SSRF | PASS | Resolved-IP validation (not string-matching) against a broad private/loopback/link-local/cloud-metadata blocklist, IPv4+IPv6, HTTPS-only, manual redirect-chain re-validation (max 5 hops), size-capped streaming download. `tests/test_media_gen.py` — private/internal addresses, redirect-to-private, non-HTTPS redirect, too-many-redirects all rejected; genuinely public address allowed. All pass. | No |
| Rate Limiting | **PARTIAL** | Real, tested (`tests/test_rate_limit.py`, `tests/test_api_rate_limit.py`, `tests/security/test_rate_limit_header_spoofing.py` — all pass, including a verified-this-engagement regression proving spoofed `X-Forwarded-For`/`X-Real-IP` headers cannot bypass or pollute per-client budgets). **Process-local only** — confirmed via `grep` (zero Redis usage anywhere in this codebase or `requirements.txt`) and the module's own docstring ("not a substitute for real rate limiting in a multi-tenant deployment"). | **Yes, only for a horizontally-scaled (multi-instance) deployment** — a single-instance deployment is not blocked by this |
| Resource Exhaustion | PARTIAL | Many real limits confirmed (row caps, query timeouts, PDF page caps, upload size caps, media file-size caps). No whole-request timeout or concurrency limiter on `/ask` — a pre-existing, self-disclosed gap (`docs/THREAT_MODEL.md`'s own DoS section, independently re-read this session, not just cited) not re-verified or closed this pass. | No (DoS-shaped, not a confidentiality/integrity breach) |
| Secrets | PASS | `detect-secrets` re-run fresh this session: 20 files / 46 matches, zero drift from the already-triaged, all-false-positive baseline. `SecretStr` typing + `.get_secret_value()` discipline confirmed in `config/settings.py`. gitleaks itself remains NOT VERIFIED (tooling unavailable in this sandbox — it runs natively in GitHub Actions regardless). | No |
| Dependencies | PARTIAL | `pip-audit`/`npm audit` re-run fresh this session: 24 raw / 16 unique / 14 CVE findings, **zero newly reachable**, all independently re-confirmed NOT REACHABLE or DEV ONLY this engagement (file:line evidence in `docs/security/CVE_TRIAGE.md`). The `langgraph` major-version migration that would close most of these remains **explicitly out of scope** for this gate. | No (not reachable today) — but the pending migration is a named, tracked risk |
| SAST | PASS | `bandit -r agent api config db security media_gen moderation rag search voice media embeddings` re-run fresh this session: **0 issues**, 18,123 lines. | No |
| DAST | **NOT VERIFIED** | No staging environment or OWASP ZAP available in this sandboxed session. Never run, at any point in this engagement. See `docs/security/DAST_REPORT.md`. | **Yes** — per this gate's own explicit Phase 17/30 rule |
| Container Security | PARTIAL | Non-root user confirmed (`docker run ... id` → uid 1000), digest-pinned base images confirmed. Trivy run for the first time this engagement: 29 unique CRITICAL/HIGH findings — 4 Python-ecosystem (all confirmed NOT REACHABLE via direct in-container file inspection) + ~17 Debian OS-package CVEs (only `util-linux`/`curl` groups independently reachability-checked; ~9 others honestly marked NOT INDEPENDENTLY VERIFIED — see `docs/security/CVE_TRIAGE.md` §4). | No confirmed exploitable finding, but verification is incomplete |
| CI/CD | PASS | `bandit`/`pip-audit`/`detect-secrets` are blocking (no `continue-on-error`), confirmed via direct read of `.github/workflows/ci.yml` this session. Exactly 2 `continue-on-error: true` remain (gitleaks, Trivy), both with a dated, reasoned, non-silent comment explaining why. | No |
| Security Headers | PASS | HSTS, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, CSP all present by default (`api/main.py::_add_security_headers`). `tests/test_security_headers.py` passes. | No |
| CORS | PASS | Wildcard origin (`CORS_ALLOWED_ORIGINS=*`) rejected at config-validation time (`config/settings.py::_reject_wildcard_cors_origin`) — a startup fail-closed check, confirmed by direct read this session, not assumed. | No |
| Logging | PASS | Structured security-event logging with correlation IDs (`security/audit_log.py`), `.safe_message` exception contract preventing raw internal errors from reaching responses, secret redaction (`security/redaction.py`, `tests/test_redaction.py`/`tests/test_observability_redaction.py` pass). | No |
| Monitoring | **PARTIAL** | `GET /health` + Docker `HEALTHCHECK` exist and are real (liveness-shaped). No metrics/alerting integration exists anywhere in this codebase (confirmed by grep — no Prometheus/StatsD/OpenTelemetry-metrics client in `requirements.txt`) — consistent with this app's own stated "local-first, single-operator" scope, not a hidden regression, but a real gap for "enterprise production" specifically. | No (operational maturity gap, not a security hole) |
| Incident Response | **FAIL** | `docs/security/INCIDENT_RESPONSE.md` did not exist before this session (confirmed absent via `ls`; `SECURITY_FINAL_REPORT.md` §25 already explicitly stated "NOT PRODUCED" as a named, prioritized blocker). Created this session — see that document; it is a first version, not battle-tested. | **Yes, until reviewed by an actual on-call/ops owner** — a document existing is not the same as a rehearsed capability |
| Backup/Recovery | **NOT VERIFIED** | No backup/restore procedure exists in `docs/DEPLOYMENT.md` (confirmed via grep — zero mentions). No live database available in this sandboxed session to test a restore against even if a procedure existed. | **Yes, for any deployment holding data the operator cannot afford to lose** — this app has no opinion on the operator's own database's backup strategy today |
| Threat Model | PASS | `docs/THREAT_MODEL.md` exists, STRIDE-formatted, substantive (79 lines, per-threat mitigation + residual-risk columns), independently spot-checked this session (its cited test class names were confirmed to actually exist via `grep`, not taken on faith). Dated "Phase 2" — has not been refreshed to reflect Phase 3 hardening or this session's own new controls (malware scanner, CVE triage rigor) — a currency gap, not an absence. | No |

## Reading this matrix

**10 of 25 rows are not a clean PASS.** That is the honest count, not a
rounding-down. Of those:

- **3 are explicit, rule-mandated NOT READY triggers per this gate's own
  Phase 30**: DAST (never run), OIDC E2E (never run against a live IdP),
  and malware scanning defaulting off with no real scanner ever verified.
- **1 is a genuine FAIL**, now remediated within this same session
  (Incident Response document — see `docs/security/INCIDENT_RESPONSE.md`),
  but a first-draft document is not the same as a rehearsed, owned
  capability.
- **1 is NOT VERIFIED with no realistic path to closing it in this
  sandboxed environment** (backup/recovery — this app has no database of
  its own to back up; it's entirely the operator's own infrastructure
  decision).
- **The remaining 5 PARTIALs** (rate limiting, resource exhaustion,
  dependencies, container, monitoring) are each conditional or
  operational-maturity gaps, not confirmed exploitable vulnerabilities —
  each is explicitly scoped in its own row above.

See `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md` §4/§5 for the
full P0/P1/P2 classification and `§30` for the final verdict and its
reasoning.
