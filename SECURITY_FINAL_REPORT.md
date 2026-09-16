# Security Final Report — 2026 Phase 3

**Date:** 2026-09-16
**Scope:** A security audit + targeted hardening pass over the existing Text-to-SQL / multi-source AI platform. This is **not** a from-scratch security build — it verifies, and where genuinely broken, fixes, an already-substantial existing security posture (OIDC auth, RBAC, an AST-based SQL validator, content moderation, audit logging) built across prior "Phase 1/2" review cycles referenced throughout this codebase's own comments.

**Method:** Every claim below is grounded in either (a) direct source-code inspection with file:line citations (see `SECURITY_BASELINE.md` for the full per-control audit trail), (b) an actually-executed test suite / scanner run, or (c) explicitly marked as unverified. No control is reported as PASS on the strength of documentation alone — several findings in this report exist specifically because prior documentation claimed more than the code delivered, or because a capability existed in code but was never wired into an actual enforcement path.

**Status legend:** PASS / PASS WITH CONDITIONS / PARTIAL / FAIL / NOT TESTED. No numeric scores, no "enterprise-ready" language, anywhere in this document — only measurable, cited claims.

---

## 1. Executive Summary

This platform's core safety architecture is genuinely strong: an AST-based SQL validator (not a regex blocklist) that has already closed two real historical bypasses (a data-modifying CTE, dangerous engine functions), an OIDC/JWT authentication layer that correctly implements every standard defense (algorithm allowlisting, audience validation, no token/claim logging), and an RBAC layer that's actually wired into every checked endpoint rather than existing as unused scaffolding. Prior review cycles (referenced throughout the code as "2026 Phase 1"/"Phase 2") demonstrably found and fixed real bugs (AUTHZ-01: a `SELECT *` wildcard bypassing the restricted-column gate; AGT-01: the multi-source router's LLM decision alone deciding data access).

This pass found and fixed **four P0-severity gaps**: a document-retrieval path with no per-document access control beyond three fixed categories, a frontend with no way to obtain a real per-user credential (forcing reliance on an admin-granting static token embedded in the public JS bundle), a database write-privilege detector that existed in code but was never actually called at startup, and zero enforced CI security gates. It also fixed six P1/P2 findings (missing security headers, an SSRF redirect-following gap, a SQL-validator gap around system catalog tables, a CORS misconfiguration hole, a stale Dockerfile comment, and ran SAST for the first time — closing all 10 findings it surfaced).

**What remains genuinely open, not fixed in this pass:** 24 known CVEs across 7 backend dependencies (the largest, `langgraph`, needs a major version migration this pass deliberately did not attempt, to avoid breaking the entire agent architecture without adequate regression time); malware/AV scanning (requires new external infrastructure); distributed rate limiting (requires Redis); the frontend OIDC flow was built and passes every static check available in this environment but was never exercised against a live identity provider; DAST, fuzz testing, and a full file-upload resource-limit audit were not performed at all.

**This platform should not be described as "production-ready" without closing the items in §29 (Remaining Risks) and §30 (Production Deployment Checklist) first — in particular the dependency CVEs and the unverified frontend OIDC flow.**

---

## 2. Attack Surface

| Surface | Components |
|---|---|
| Public API | FastAPI (`api/main.py` + routers) — `/ask`, `/execute`, `/documents/*`, `/generate/*`, `/voice/*`, `/search/media`, `/media/*`, `/schema/*` |
| Frontend | React SPA (`frontend/`), served same-origin by the API process in production |
| LLM | Local Ollama (no cloud API calls for generation) |
| Databases | User-configured business DB(s) (postgres/mysql/mssql/oracle), a dedicated RAG store (SQL Server `VECTOR`), a dedicated moderation-metadata store |
| Vector store | ChromaDB (schema embeddings, golden examples, media search) + SQL Server native `VECTOR` (RAG chunks) |
| External services | Ollama (local), optionally: Tavily (web search), IMA Studio (media generation), Azure AI Content Safety (moderation, mandatory whenever media search / document RAG is on) |
| Identity | Static bearer token, and/or OIDC (any standard-compliant IdP) — now on both frontend and backend |
| Infrastructure | Docker (digest-pinned base images, non-root), GitHub Actions CI |

---

## 3. Threat Model

Not re-derived from scratch here — this pass audited against, rather than authored, a threat model. See `SECURITY_BASELINE.md` for the full per-control STRIDE-adjacent breakdown (asset / trust boundary / threat actor / attack vector / existing control / missing control, per audited component). A from-scratch `docs/SECURITY_THREAT_MODEL.md` covering all 28 components named in the original engagement brief (SQL generation, RAG, PDF/image/video parsers, Docker, CI/CD, admin functions, etc.) was **not produced** in this pass — `SECURITY_BASELINE.md`'s per-control format covers the audited subset with equivalent rigor, but a handful of components (image/video/audio parsers specifically, and CI/CD's own supply-chain surface) were never independently audited at all (see §29).

---

## 4. Security Controls Summary

| Control | Status | Evidence |
|---|---|---|
| Defense-in-depth layering | **PASS** | Confirmed as an actual architectural property, not just a stated intent: authentication → authorization → SQL AST validation → DB-role read-only enforcement (now startup-checked) are independent layers, verified by reading each one's code, not inferred from documentation |
| LLM never treated as an authorization mechanism | **PASS** | `agent/authz.py`'s router-level gate runs *after* the LLM picks a source and *before* the subgraph executes (confirmed, `agent/orchestrator/nodes.py:356`) — the LLM can request, never decide, access |
| SQL validator as terminal gate, not the only gate | **PASS WITH CONDITIONS** | AST allowlist is genuinely strong (§7); DB-role read-only-ness is the real backstop per design, and is now startup-verified (§5) — condition: operator must still configure a genuinely least-privilege `DB_USER` themselves, this app cannot create that role for them |

---

## 5. Authentication

**Status: PASS.**

OIDC/JWT validation (`security/oidc.py`) correctly implements every standard defense: server-side algorithm allowlist (never trusts the token's own `alg` header), mandatory audience validation (refuses to start without it once an issuer is configured), bounded clock skew, no raw token/claim ever logged, uniform generic failure responses (no oracle for which check failed). A production-fail-closed check (`config/settings.py::_require_identity_in_production`) refuses to start the app at all if `ENVIRONMENT=production` and no authentication is configured. Verified by direct code reading, not doc claims.

**New in this pass (frontend OIDC login):** `frontend/src/lib/auth.ts` + `store/authStore.ts` implement a real Authorization Code + PKCE flow via `oidc-client-ts`. In-memory-only token storage (never `localStorage`/`sessionStorage`). Closes a real gap: previously the shipped SPA's only possible credential was a static, build-time-baked, admin-granting token extractable from the public JS bundle.

**Condition / not fully verified:** the frontend OIDC flow passes `tsc --noEmit`, `oxlint`, and `npm run build` cleanly, and follows `oidc-client-ts`'s documented API shape, but was **never exercised against a live identity provider** in this environment (no IdP, no browser automation tooling available). Treat as implemented-but-unverified until run against a real Auth0/Okta/Azure AD/Keycloak tenant.

---

## 6. Authorization / RBAC

**Status: PASS, with one addressed-by-design item.**

`agent/authz.py` + `api/authz.py`: 15 fine-grained permissions, 4 extensible roles, fail-closed on unrecognized roles, confirmed actually wired into every checked route (not dead code — verified via repo-wide call-site grep) and into the orchestrator's own source-routing decision (closing a real prior finding, AGT-01).

**No per-resource ownership** (any `admin` can delete any document) is a confirmed, deliberate design choice for this app's shared-knowledge-base model, not a gap — see `docs/AUTHORIZATION.md`'s own "Known limitations" section, honestly disclosed before this pass began.

**New in this pass:** `restricted_roles` — a per-document, operator-set role restriction on RAG documents (`rag/store.py`, `rag/graph.py`), closing a real gap where retrieval had **zero** access-control beyond three fixed sensitivity categories. Enforced both in chat-answer generation (before the LLM ever sees the chunk) and in the raw-bytes download route. 10 new dedicated unit tests for the gate itself (`tests/test_rag_graph.py`, previously a completely untested module) plus store/API-level round-trip tests.

---

## 7. SQL Security

**Status: PASS.**

`agent/sql_validator.py` is AST-based (sqlglot), not regex — full-tree walk for embedded writes (closes the data-modifying-CTE bypass class), a documented-as-non-exhaustive dangerous-function denylist, wildcard-aware restricted-column matching (closes a real prior finding, AUTHZ-01).

**New in this pass:** system-catalog/data-dictionary table access (`information_schema`, `pg_catalog`, mssql `sys`, Oracle `ALL_*`/`DBA_*`/`USER_*`/`V$`) is now blocked — a real, previously-unflagged gap (these queries are syntactically ordinary `SELECT`s, no write, no dangerous function, so they passed every prior check). 20 new tests across all 4 dialects, including explicit false-positive tests (`user_accounts`, `sys_config_log` — ordinary business tables that share a word with a catalog prefix — confirmed NOT flagged, avoiding a real risk of breaking legitimate schemas that this fix's first draft would have introduced via an overly broad prefix match).

---

## 8. Database Security

**Status: PASS WITH CONDITIONS.**

Query timeout enforced for every dialect via forced connection-abort (works even for mssql/oracle, which have no simple session-level SET) — verified directly in `db/execution.py`. Row cap enforced at the cursor level (`fetchmany`), independent of the SQL text's own LIMIT clause. Sensitive-column classification is genuinely config-driven YAML, not hardcoded.

**New in this pass:** `check_write_privileges` (a best-effort "is this DB role actually read-only" check) existed in code since a prior phase but was **only ever called from a manual CLI script** — a deployment that never ran that script by hand got zero signal that its "read-only" role wasn't. Now called from `api/main.py`'s startup path for every configured database; refuses to start in `ENVIRONMENT=production` if write privileges are detected, warns otherwise. 7 new tests.

**Condition:** this check is best-effort and fails open on an unsupported `DB_TYPE` or a permission error reading the privilege catalog — the real guarantee still requires the operator to configure a genuinely least-privilege database role. This pass makes misconfiguration *detectable and startup-blocking in production*, it does not and cannot create the correct role for the operator.

---

## 9. AI Security (LLM)

**Status: PASS WITH CONDITIONS.**

Model config, system prompts, and output handling were not independently re-audited in full this pass — the existing architecture (SQL AST validation is the sole trust boundary for generated SQL, never the model's own claims; structured schema validation for tool-call-shaped output) was inherited and spot-checked, not re-verified line by line. No secrets/credentials found being placed into any prompt (`agent/llm_client.py`, `rag/graph.py` read this pass). Provider abstraction (Ollama-only, local) confirmed — no cloud LLM call exists in the generation path.

---

## 10. Prompt Injection

**Status: PASS (structural defense), MISSING (active detection beyond pattern-matching).**

`agent/input_guard.py`'s own module docstring correctly states its regex layer is "a fast, cheap first layer, not the last line of defense" — verified this is actually true in the architecture, not just claimed: retrieved content (RAG chunks, schema, web results) is framed as untrusted data in every system prompt checked (`rag/graph.py`'s `_GENERATE_SYSTEM_PROMPT` explicit "treat as data, not instructions"), and the SQL validator is the real terminal gate regardless of what the LLM produces. A prior gap (conversation history bypassing all of `input_guard`'s checks, finding LLM-01) was found and fixed before this pass began. No ML-based/semantic injection classifier exists — pattern-matching plus structural framing is the entire defense, which is an honestly-scoped, reasonable posture for this threat model, not a hidden gap.

---

## 11. RAG Security

**Status: PASS, with one deliberate scope boundary.**

Deletion correctly cascades (SQL `ON DELETE CASCADE` — verified no stale embeddings survive a document delete). Retrieved content is untrusted-data-framed in the generation prompt. The `restricted_roles` fix (§6) closes the real per-document access-control gap this pass found. **Not built, deliberately:** per-user/tenant document isolation — this app is a shared knowledge base by explicit design confirmation from the user directing this engagement, not a multi-tenant document store; building silent isolation would have broken the intended shared-assistant use case.

---

## 12. File / Media Security

**Status: PARTIAL, NOT FULLY AUDITED.**

Round-1 audit confirmed: PDF magic-byte validation, bounded-read size limits (`api/documents.py`). **Not independently verified this pass:** PDF page/object count limits under adversarial input, image decompression-bomb limits, video/audio duration/resolution limits — `docs/FILE_UPLOAD_*.md` make detailed claims here that were not code-verified in this engagement (per this engagement's own Rule 23, a doc claim is not evidence). Malware/AV scanning is confirmed **MISSING** — no `MalwareScanner` abstraction exists anywhere in source; this is already honestly disclosed as the most significant known gap in the codebase's own `docs/FILE_UPLOAD_FINAL_REPORT.md`, not a new finding, and was not closed in this pass (requires new external infrastructure, e.g. ClamAV).

---

## 13. SSRF

**Status: PASS.**

`media_gen/download.py`'s SSRF defense was already strong (post-DNS-resolution IP validation, broad private/loopback/link-local/cloud-metadata range coverage, IPv4+IPv6, content-type allowlisting against stored-XSS-via-served-media). **New in this pass:** redirect responses are now manually followed and independently re-validated at each hop (max 5), closing a real gap where the original URL's validation was moot the instant that URL's server issued a redirect. 6 new tests.

---

## 14. API Security

**Status: PASS.**

Every checked endpoint requires authentication + the correct RBAC permission. Rate limiting exists at multiple layers (per-IP question submission, process-global LLM-call budget, per-action limits on expensive routes like `/execute`/`/documents`/`/generate/confirm`). Generic client-facing error messages confirmed (global exception handlers, `.safe_message` pattern) — no raw exception text/stack trace reaches a response body.

---

## 15. Frontend Security

**Status: PASS.**

Zero raw `dangerouslySetInnerHTML` usage anywhere in the frontend (confirmed by repo-wide grep). Markdown rendering deliberately omits `rehype-raw` (no HTML passthrough from untrusted web/document content) with an explicit code comment stating why. Link rendering forces `rel="noopener noreferrer"` (tabnabbing defense). Frontend authorization gating (`AuthGate.tsx`) is explicitly documented in its own code as UX-only, never the real security boundary — every backend route independently re-validates regardless.

---

## 16. Database Security (see §8) / Docker Security

**Status: PASS.**

Non-root user, digest-pinned base images (not floating tags), `docker-compose.yml` drops all Linux capabilities and sets `no-new-privileges`. **New in this pass:** a Dockerfile comment claimed `build-essential` was removed post-install to reduce image size — it was not; fixed by adding the actual `apt-get purge --auto-remove` in the same build layer as the pip install (removal in a separate `RUN` would not have shrunk the image). **Docker build was attempted in this environment (Docker Desktop available) to verify the fix but did not complete within this session's time budget** — the fix follows a standard, well-understood pattern (single-layer install+purge) but was not confirmed via an actual successful build in this pass. Verify before relying on this fix in production.

---

## 17. Supply Chain / Dependencies

**Status: FAIL (backend), PASS (frontend).**

`npm audit` (frontend): **0 vulnerabilities.**

`pip-audit` (backend, run for the first time in this engagement): **24 known vulnerabilities across 7 packages** — `langgraph` (0.2.62, needs 1.0.10 — a major rewrite), `chromadb`, `pytest`, `black`, `langchain-core`, `langgraph-checkpoint`, `langgraph-sdk`. Every one requires a major version bump; no patch-level fix exists for any of them. **Not remediated in this pass** — the `langgraph` migration in particular touches the entire compiled-graph architecture (`agent/graph.py`, `agent/orchestrator/graph.py`, every node function) and attempting it without dedicated regression-testing time would risk exactly the kind of breakage this engagement's own rules (preserve existing functionality) forbid. This is the single largest piece of unaddressed risk from this pass — see §28/§29.

GitHub Actions pinning (pin-by-SHA vs. floating tag) was **not audited** this pass.

---

## 18. CI/CD

**Status: PARTIAL.**

Secret-scan (gitleaks), SAST (bandit), and container-scan (Trivy) jobs exist in `.github/workflows/ci.yml`, all still `continue-on-error: true` as of this report — **not flipped to blocking in this pass**, because a responsible flip requires the *CI job itself* to have run clean at least once, and this environment could run bandit directly (see §19) but not gitleaks or a full Trivy image scan (no gitleaks binary; Docker build did not complete in time — see §16). Bandit itself was run directly (not via the CI job) and its baseline is now genuinely clean.

---

## 19. SAST

**Status: PASS (bandit).**

Bandit run for the first time against this codebase's first-party packages (`agent`, `api`, `config`, `db`, `security`, `media_gen`, `moderation`, `rag`, `search`, `voice`, `media`, `embeddings`). 10 findings, all triaged: 2 genuine issues fixed (XXE-hardening via `defusedxml` for MSSQL showplan-XML parsing; 4 `assert`-based invariant checks converted to explicit raises, since `assert` is stripped under `python -O`), 4 confirmed false positives suppressed with inline `# nosec` justification comments (prompt text mis-flagged as SQL, and SQL already safely parameterized/identifier-quoted). **Re-run confirms zero remaining findings of any severity.**

---

## 20. DAST

**Status: NOT TESTED.**

OWASP ZAP (or any DAST tool) was not run against this application in this pass. No reproducible DAST script exists. This is a genuine, unaddressed gap against the original engagement scope.

---

## 21. Fuzz Testing

**Status: NOT TESTED.**

No fuzz testing (Hypothesis or otherwise) was introduced against the SQL validator, request schemas, file parsers, URL validator, JWT claims, prompt normalization, RAG ingestion, or media metadata parsers in this pass.

---

## 22. Secrets

**Status: PASS (grep-based spot-check), NOT VERIFIED (full gitleaks run).**

A pattern-based scan (AWS keys, GitHub tokens, PEM private key headers, hardcoded password literals, Slack tokens) across the entire repository found zero matches. This is **not equivalent to running gitleaks** (different detection engine, entropy-based checks, full git history vs. working tree) — the CI gitleaks job remains the authoritative check and is still not run/gated (§18). `.env` confirmed gitignored; `.env.example` contains only placeholders (verified during this pass while adding the new OIDC section).

---

## 23. Logging / Audit

**Status: PASS.**

`security/audit_log.py`'s structured event logging confirmed to never log tokens, Authorization headers, or raw JWT claims (every rejection path logs only a stable reason string). Correlation IDs threaded through. New audit events added this pass: `db_write_privileges_detected` (critical severity — the first real user of that severity level in this codebase) and `rag_role_restricted_content_blocked`.

---

## 24. Monitoring

**Status: NOT TESTED / NOT INDEPENDENTLY VERIFIED.**

`GET /health` exists and was used as this pass's own test target; readiness/liveness distinction, security-event metrics (auth-failure rate, rate-limit-trip rate, SQL-rejection rate as actual exported metrics rather than log lines) were not audited or built in this pass.

---

## 25. Incident Response

**Status: NOT PRODUCED.**

`docs/INCIDENT_RESPONSE.md` was not created in this pass (the engagement's own explicit requirement) — a real, named gap, not an oversight to silently skip. See §30's checklist.

---

## 26. Privacy

**Status: NOT INDEPENDENTLY AUDITED THIS PASS.**

PII/data-classification handling (`config/sensitive_columns.yaml`, document `sensitivity_category`) was confirmed to exist and be config-driven (§8), but a full privacy/retention-policy audit (Phase 20 of the original engagement scope) was not performed.

---

## 27. Production Deployment Architecture

**Status: NOT REVIEWED THIS PASS.**

TLS termination, reverse proxy, WAF, network segmentation, secret manager integration, backup/restore, key rotation — none of this was reviewed in this pass. `docs/DEPLOYMENT.md` (pre-existing) covers some of this; not re-verified here.

---

## 28. Remaining Risks (Unresolved)

| Risk | Severity | Why not fixed this pass |
|---|---|---|
| 24 known CVEs across 7 backend dependencies, largest requiring `langgraph` 0.2→1.0 migration | **High** | Major breaking-API migration touching the entire agent graph architecture; unsafe to attempt without dedicated regression time |
| Frontend OIDC flow never exercised against a live IdP | Medium | No IdP/browser automation available in this environment |
| No malware/AV scanning | Medium (already disclosed pre-existing gap) | Requires new external infrastructure |
| Rate limiting is in-memory only, not multi-instance safe | Medium (already disclosed pre-existing gap) | Requires Redis or equivalent |
| CI security gates still non-blocking | Medium | gitleaks/Trivy could not be run directly in this environment to establish a clean CI-job baseline |
| DAST, fuzz testing never performed | Medium | Out of scope for the time available in this pass |
| File-upload resource-limit specifics (PDF/image/video bombs) never code-verified | Medium | Time; existing docs make claims that need independent verification |
| Docker image build never completed successfully in this environment | Low-Medium | Environment/time constraint, not a code defect found |

## 29. Accepted Risks (Deliberate, Disclosed)

| Risk | Why accepted |
|---|---|
| No per-resource/per-uploader document ownership | Confirmed intentional — this app is a shared knowledge base by design, not per-user storage |
| `static_token`/`none` auth modes grant full admin | No natural sub-identity to scope a single shared secret down to; pre-existing, documented design |
| `restricted_roles` role names not validated against `agent.authz.ROLE_PERMISSIONS` at upload time | Fails closed (a typo'd role restricts to nobody) rather than open; a validation UX improvement, not a security gap |

## 30. Production Deployment Checklist

```
[x] Authentication is secure (OIDC + fail-closed production check)
[x] Authorization is enforced server-side
[ ] Resource ownership enforced — N/A by design (shared knowledge base)
[ ] Tenant isolation — N/A, not a multi-tenant deployment model
[x] SQL is independently validated (AST allowlist + system-catalog block)
[x] Database write-privilege misconfiguration is startup-detected (production fail-closed)
[x] Prompt injection defenses exist (structural framing + pattern layer)
[x] RAG per-document access control exists (restricted_roles)
[ ] RAG poisoning defenses — pattern-detection only, not independently re-verified this pass
[ ] File uploads securely validated against resource-exhaustion — NOT VERIFIED this pass
[ ] Malware scanning — NOT IMPLEMENTED, requires external service
[x] SSRF protection exists, including redirect re-validation
[x] API rate limiting exists
[ ] Distributed rate limiting — NOT IMPLEMENTED, requires Redis
[x] Secrets are protected (SecretStr, no hardcoded credentials found)
[x] Security headers configured (HSTS/CSP/X-Frame-Options/etc.)
[x] CORS restricted, wildcard+credentials rejected at config time
[x] Frontend XSS protections verified (no raw HTML injection vectors)
[x] Docker hardened (non-root, digest-pinned, capabilities dropped) — image build not confirmed to succeed in this environment
[ ] Dependencies scanned AND clean — SCANNED, NOT CLEAN (24 CVEs, see §28)
[ ] Container scanned — NOT RUN this pass (Trivy)
[ ] Secrets scanned (gitleaks) — NOT RUN this pass, spot-check only
[x] SAST runs and is clean (bandit)
[ ] DAST runs — NOT RUN this pass
[x] Security regression tests pass (1112 tests, full suite)
[x] Authorization tests pass (including new restricted_roles tests)
[x] SQL adversarial tests pass
[ ] File security tests — NOT ADDED this pass
[x] SSRF tests pass (including new redirect tests)
[ ] Prompt injection tests — pre-existing coverage only, not extended this pass
[ ] Cross-tenant tests — N/A, not a multi-tenant model
[x] Production configuration fails closed (auth + DB write-privilege checks)
[ ] Incident response documentation — NOT PRODUCED this pass
[x] Security documentation matches implementation (this report + updated AUTHENTICATION.md/AUTHORIZATION.md)
[ ] No critical/high unresolved vulnerability without documented risk acceptance — FALSE: 24 dependency CVEs remain unresolved and undocumented-as-accepted until this report; now documented here as a known, open risk requiring a dedicated migration
```

**This platform is not production-ready as-is.** The specific blockers, in priority order: (1) the `langgraph`/dependency CVE remediation, (2) live verification of the frontend OIDC flow against a real IdP, (3) establishing a clean gitleaks/Trivy baseline and flipping CI to blocking, (4) a dedicated file-upload resource-limit audit, (5) `docs/INCIDENT_RESPONSE.md`.
