# Security Production Checklist

Operator-facing checklist for taking this application from local/dev use to a real production deployment. Each item is tagged with its verification status from the 2026 Phase 3 security pass (see `SECURITY_FINAL_REPORT.md` for the full evidence behind each).

Legend:
- **IMPLEMENTED AND VERIFIED** — code exists, and was confirmed working via an actually-executed test/scanner in this pass.
- **IMPLEMENTED BUT NOT FULLY VERIFIED** — code exists and passes static checks, but wasn't exercised end-to-end (e.g. against a live external service).
- **REQUIRES EXTERNAL INFRASTRUCTURE** — the code has a documented integration point, but you must stand up something yourself.
- **REQUIRES THIRD-PARTY SERVICE** — not implemented at all; needs a new vendor/service integration.
- **REQUIRES MANUAL PENETRATION TEST** — no automated substitute exists for this item.
- **KNOWN ACCEPTED RISK** — deliberately not fixed, with a stated reason.

---

## 1. Authentication

- [x] **IMPLEMENTED AND VERIFIED** — Set `ENVIRONMENT=production`. The app refuses to start with no authentication configured (`config/settings.py::_require_identity_in_production`).
- [x] **IMPLEMENTED AND VERIFIED** — Configure `OIDC_ISSUER`/`OIDC_AUDIENCE` (see `.env.example`'s OIDC section, now documented) for real per-user authentication. Verified: algorithm allowlisting, audience validation, no token/claim logging, uniform failure responses.
- [ ] **IMPLEMENTED BUT NOT FULLY VERIFIED** — Configure `VITE_OIDC_AUTHORITY`/`VITE_OIDC_CLIENT_ID` (register a separate public/SPA OIDC client at your IdP) so the React dashboard can actually obtain a per-user token. **Action required: exercise this against your real IdP before relying on it** — it was never tested against a live provider in this pass.
- [ ] **KNOWN ACCEPTED RISK** — `API_AUTH_TOKEN`/`VITE_API_AUTH_TOKEN` (the static-token fallback) grants full `admin` RBAC and, if set, is extractable from the public JS bundle. Use this mode only for trusted-network/service-to-service access, never as your sole production credential for a publicly-reachable dashboard.

## 2. Authorization

- [x] **IMPLEMENTED AND VERIFIED** — RBAC is enforced server-side on every route (`agent/authz.py`/`api/authz.py`). Assign roles via your IdP's `roles` claim (or `OIDC_ROLE_CLAIM` if your provider uses a different claim name).
- [x] **IMPLEMENTED AND VERIFIED** — Per-document RAG access restriction (`restricted_roles`, set via the upload form) for content that needs finer-grained access than the fixed sensitivity categories.
- [ ] **KNOWN ACCEPTED RISK** — No per-uploader document ownership. Any caller with `DOCUMENTS_DELETE` can delete any document. This is a deliberate design choice (shared knowledge base) — if your deployment needs per-user private documents, this app's current architecture does not support that and would need real changes, not a config flag.

## 3. SQL / Database Security

- [x] **IMPLEMENTED AND VERIFIED** — AST-based SQL validation, including the new system-catalog-access block.
- [x] **IMPLEMENTED AND VERIFIED** — Startup detection of a misconfigured (writable) database role, fails closed in production.
- [ ] **REQUIRES EXTERNAL INFRASTRUCTURE** — You must still configure `DB_USER` as a genuinely least-privilege, read-only database role yourself. The app can now *detect and refuse to start* if you get this wrong; it cannot create the correct role for you.
- [ ] **REQUIRES MANUAL PENETRATION TEST** — A dedicated adversarial-SQL penetration test (beyond this codebase's own 100+ adversarial unit tests) is recommended before handling genuinely sensitive production data.

## 4. File Upload Security

- [ ] **IMPLEMENTED BUT NOT FULLY VERIFIED** — Magic-byte validation and bounded-read size limits exist. PDF page-count limits, image decompression-bomb limits, and video/audio resource limits are documented in `docs/FILE_UPLOAD_*.md` but were not independently code-verified in this pass.
- [ ] **REQUIRES THIRD-PARTY SERVICE** — No malware/AV scanning exists. If you accept uploads from untrusted users, integrate a scanner (e.g. ClamAV) ahead of the moderation gate before going to production. This is the single most significant, already-disclosed gap in this codebase's own file-upload security documentation.

## 5. Network / SSRF

- [x] **IMPLEMENTED AND VERIFIED** — Media-generation download SSRF protection, including redirect re-validation.
- [ ] **REQUIRES MANUAL PENETRATION TEST** — No automated DAST/SSRF scan was run against the live application.

## 6. API Security

- [x] **IMPLEMENTED AND VERIFIED** — Authentication + authorization on every checked route, generic client-facing errors, rate limiting.
- [ ] **REQUIRES EXTERNAL INFRASTRUCTURE** — Rate limiting is in-memory, per-process. If you deploy more than one worker/instance, each gets an independent budget (multiplying the effective limit). Put a distributed limiter (Redis-backed, or your reverse proxy/API gateway's own rate limiting) in front for a multi-instance deployment.

## 7. Frontend Security

- [x] **IMPLEMENTED AND VERIFIED** — No raw HTML injection vectors, safe Markdown rendering, frontend authorization is documented as UX-only (never trust it as the real boundary).

## 8. Security Headers / CORS

- [x] **IMPLEMENTED AND VERIFIED** — HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy all on by default. CORS wildcard+credentials rejected at config time.
- [ ] **Action required if using a non-default font provider or embedding this app in a frame** — the default CSP is scoped to this app's actual asset origins (self + Google Fonts); override `CONTENT_SECURITY_POLICY` in `.env` if your deployment needs something different.

## 9. Docker / Container Security

- [x] **IMPLEMENTED AND VERIFIED** — Non-root user, digest-pinned base images, dropped Linux capabilities, `no-new-privileges`.
- [ ] **IMPLEMENTED BUT NOT FULLY VERIFIED** — A Dockerfile fix (proper `build-essential` removal) was made this pass but the image build did not complete successfully within this session's environment/time budget. **Action required: run `docker build .` yourself and confirm it succeeds before deploying this image.**

## 10. Supply Chain

- [ ] **Action required, High priority** — `pip-audit` found 24 known CVEs across 7 backend dependencies, the largest requiring a `langgraph` 0.2→1.0 major-version migration. **This was not remediated in this pass** (too large/breaking to attempt safely without dedicated regression time) — schedule this as its own project before going to production with sensitive data.
- [x] **IMPLEMENTED AND VERIFIED** — `npm audit`: 0 frontend vulnerabilities.
- [ ] **REQUIRES EXTERNAL INFRASTRUCTURE** — Establish a gitleaks and Trivy baseline (neither could be run in this pass's environment) and flip `.github/workflows/ci.yml`'s `continue-on-error: true` to blocking once that baseline is triaged.

## 11. CI/CD

- [x] **IMPLEMENTED AND VERIFIED** — Bandit SAST baseline is clean (verified by direct execution, not just CI config).
- [ ] **REQUIRES EXTERNAL INFRASTRUCTURE** — gitleaks/Trivy CI jobs exist but remain non-blocking; establish and triage a real baseline before flipping them to gate deployment.
- [ ] **REQUIRES THIRD-PARTY SERVICE** — DAST (OWASP ZAP or equivalent) is not wired into CI at all.

## 12. Logging / Monitoring / Incident Response

- [x] **IMPLEMENTED AND VERIFIED** — Structured audit logging with no credential/token leakage.
- [ ] **REQUIRES EXTERNAL INFRASTRUCTURE** — No centralized log aggregation, alerting, or exported security metrics (auth-failure rate, rate-limit-trip rate, etc.) beyond log lines were built/verified in this pass.
- [ ] **NOT PRODUCED** — `docs/INCIDENT_RESPONSE.md` does not exist. Write one covering (at minimum) credential compromise, a malicious upload getting past moderation, unauthorized SQL execution, and dependency-vulnerability response, before going to production.

## 13. Deployment Architecture

- [ ] **REQUIRES EXTERNAL INFRASTRUCTURE, NOT REVIEWED THIS PASS** — TLS termination, reverse proxy/WAF, network segmentation, secret manager integration, backup/restore testing, and key rotation were out of scope for this pass. `docs/DEPLOYMENT.md` (pre-existing) covers some of this; review and verify independently.

---

## Summary: What Blocks Production Today

In priority order:

1. **Dependency CVEs** — 24 known vulnerabilities, requiring a dedicated `langgraph` migration project.
2. **Frontend OIDC flow unverified against a live IdP.**
3. **No malware scanning** if accepting uploads from untrusted users.
4. **CI security gates still non-blocking** — no clean gitleaks/Trivy baseline established.
5. **`docs/INCIDENT_RESPONSE.md` doesn't exist.**
6. **Docker image build not confirmed to succeed** in this environment — verify before relying on the Dockerfile fix.
7. **No DAST/fuzz testing/dedicated penetration test performed.**

Everything else in this checklist that's marked IMPLEMENTED AND VERIFIED reflects genuine, tested, working controls — not aspirational documentation.
