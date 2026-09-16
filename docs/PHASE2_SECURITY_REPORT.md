# Phase 2 Security Report

Strengthening the application from a secure AI prototype into an enterprise-grade production architecture. Every section states what already existed (verified, not assumed), what was added, and what remains open — see `docs/PHASE2_FINAL_REPORT.md` for the overall score and P0/P1/P2 status.

## 1. Authentication

**Before:** one optional static bearer token (`API_AUTH_TOKEN`), no-op when unset, no per-user identity.

**Added:** standard OIDC/JWT validation (`security/oidc.py`) as a second, production-grade mode, dispatched from the same `api/auth.py::verify_api_key` dependency every route already used. Signature (JWKS + `kid`, never trusting the token's own `alg`), issuer, audience, and expiration are all verified in one call. `ENVIRONMENT=production` now fails closed at startup if no authentication is configured at all. Full detail: `docs/AUTHENTICATION.md`.

## 2. Authorization (RBAC)

**Before:** none — every authenticated caller had identical, unrestricted access.

**Added:** `agent/authz.py` (pure policy) + `api/authz.py` (FastAPI wiring). Four extensible default roles (`viewer`/`user`/`analyst`/`admin`), enforced at every route that touches data or an expensive/sensitive operation, **and inside the multi-source orchestrator itself** (`router_node` now filters the LLM's own source selection through a permission check before any subgraph runs) — closing 2026 Phase 1's AGT-01/R-008 finding ("no independent authorization layer between routing and execution"). Full detail and the complete resource-to-permission mapping: `docs/AUTHORIZATION.md`.

### API endpoint security matrix

| Endpoint | Auth | Authz | Input validation | Rate limit | Resource limit | Audit event | Sensitive data | Error handling |
|---|---|---|---|---|---|---|---|---|
| `GET /health` | None (by design) | None | — | None | — | None | DB version/engine leaked to any caller (disclosed, pre-existing) | Generic, redacted |
| `POST /ask` | Required | `ASK` | Pydantic + `input_guard` (length/injection/off-topic) + history sanitization | Per-IP, per-minute | Retry budget + graph recursion cap | `input_rejected`, `auth_failed`, `authz_denied`, `orchestrator_source_denied` | `safe_message` only | 200-with-failed-body, never raw exception |
| `POST /execute` | Required | `EXECUTE_SQL` | Pydantic + `validate_sql` (AST allowlist) | Per-IP, per-action | Row cap + query timeout | `sql_safety_violation`, `sensitive_column_blocked` | `redact_secrets` on DB errors | Clean rejected/failed body |
| `POST /feedback/golden-example` | Required | `GOLDEN_EXAMPLE_WRITE` | Pydantic | **None** (gap — see below) | — | **None** (gap) | — | Standard |
| `POST /schema/refresh` | Required | `SCHEMA_REFRESH` | — | Per-IP, per-action | Re-embeds all configured DBs (expensive, admin-gated) | None | — | Standard |
| `GET /schema/tables` | Required | `ASK` | `database` param validated against configured connections | **None** (gap) | — | None | — | 404 on unknown DB |
| `GET /documents` | Required | `DOCUMENTS_READ` | — | **None** (gap) | — | None | — | 503 if store unconfigured |
| `POST /documents` | Required | `DOCUMENTS_WRITE` | Magic bytes, size cap, **page-count cap (new)**, moderation gate | Per-IP, per-action | Bounded read (`max_bytes+1`) | Moderation decision events | — | 400/503 on rejection |
| `DELETE /documents/{id}` | Required | `DOCUMENTS_DELETE` | — | Per-IP, per-action | — | **None** (gap — an irreversible action with no audit record) | — | 204 |
| `GET /documents/{id}/download` | Required | `DOCUMENTS_READ` **+ `DOCUMENTS_READ_SENSITIVE`** (new, checked per-document) | — | **None** (gap) | — | `authz_denied` on sensitive-document denial (new) | Sensitivity check closes the Phase 1 RAG-01 gap | 403/404 |
| `POST /generate/confirm` | Required | `MEDIA_GENERATE` | Media kind always re-inferred server-side, never trusted from client | Per-IP + process-wide media-gen limiter | Human-approval gate (re-checked server-side) | `generation_*` events | — | Standard |
| `GET /media/{id}` | Required | `MEDIA_GENERATE` | — | **None** (gap) | Bounded in-process cache (100 entries, FIFO) | None | — | 404 on expired/missing |
| `GET /media/library/{id}` | Required | `MEDIA_SEARCH` | Path-traversal containment check | **None** (gap) | — | None | — | 404 |
| `POST /search/media` | Required | `MEDIA_SEARCH` | Pydantic | Per-IP, per-action | — | None | — | Standard |
| `POST /voice/transcribe` | Required | `VOICE_USE` | Bounded read, duration cap | Per-IP, per-action | STT model call | None | — | 413/500 with `safe_message` |
| `POST /voice/synthesize` | Required | `VOICE_USE` | Text length capped to `max_question_length` | Per-IP, per-action | TTS model call | None | — | 503/500 with `safe_message` |

**Protecting oversized requests / excessive prompt size / flooding / expensive queries / expensive LLM calls / excessive retries / concurrency exhaustion** (the specific abuse classes named in the Phase 2 brief): all present and verified — input length caps (`check_input`), the retry-budget + recursion-limit cap, the process-wide LLM-call rate limiter, per-IP question/action limiters, and row/timeout caps on every SQL execution path. **Concurrency exhaustion specifically remains an open gap** (no semaphore/concurrency limiter on in-flight `/ask` requests, no whole-request wall-clock timeout) — self-disclosed in `docs/RISK_REGISTER.md` and not addressed in this pass; see "Remaining risks" below.

**Gaps found while building this matrix, not fixed in this pass** (each is a genuine, narrow finding, not a critical one — flagged honestly rather than silently left): five read-mostly/low-cost routes (`/feedback/golden-example`, `/schema/tables`, `GET /documents`, `GET /documents/{id}/download`, `GET /media/{id}`, `GET /media/library/{id}`) have no dedicated rate limiter, and two mutating/irreversible actions (`POST /feedback/golden-example`, `DELETE /documents/{id}`) have no audit-log event. All are now at least **authenticated and authorized** (they were not, before this pass), which closes the primary risk (an unauthorized caller reaching them at all); the remaining gap is defense-in-depth (rate limiting, audit trail) for an *already-authorized* caller, not an access-control hole.

## 3. Data / tenant isolation

This app has **no multi-tenant data model** — one shared set of configured database connections, RAG collections, and a media library, all now gated by role rather than partitioned by tenant. Per the brief's own instruction ("without unnecessarily implementing a huge multi-tenant platform"), no tenant-partitioning system was built. What was verified/established instead:

- **LangGraph state**: per-request only, never persisted or shared across requests (confirmed: no checkpointer is configured — see `docs/DEPENDENCY_SECURITY.md`'s langgraph reachability analysis).
- **Conversations**: stateless by design — the API never stores conversation history server-side; the caller resends it each request (now sanitized — see RAG security below).
- **Caches**: `agent.rate_limit`'s bounded limiter caches are keyed by IP/session/subject, not shared across callers. `media_gen.cache.MediaCache` is a single shared, unauthenticated-by-content cache (any caller with `MEDIA_GENERATE` can fetch any cached `media_id`) — acceptable for this app's stated single-organization deployment model, not acceptable for a genuinely multi-tenant one.
- **Schema cache / RAG collections / golden examples**: already scoped per-database (one Chroma collection per configured database — pre-existing, verified, not a Phase 2 change).
- **Uploaded files / generated media**: no per-uploader ownership tracking — an `admin` can manage any document/media regardless of who created it (documented limitation, matches this app's pre-existing "Knowledge Sources page is already fully-privileged" posture).
- **Audit logs**: a single shared log stream, not partitioned — acceptable for a single-organization deployment, a real gap for a genuine multi-tenant SaaS.
- **Database credentials**: one credential set per configured database connection, shared by every caller with sufficient role — no per-tenant credential isolation.
- **Temporary files**: per-request temp files (PDF/media processing) are process-local and cleaned up after use; no cross-request persistence found.

**Honest conclusion:** role-based access control (added this pass) is the right-sized answer for this app's actual deployment shape (one organization, several roles). A genuine multi-tenant architecture (isolated data per tenant, tenant-scoped credentials, tenant-scoped caches) is a materially larger undertaking, correctly out of scope here, and should be a deliberate Phase 3 decision if this app is ever deployed to serve multiple separate organizations from one instance — not something to retrofit piecemeal.

## 4. SQL security (final review)

Full lifecycle re-verified: AST validation, SELECT-only enforcement, nested-write/DDL detection, dangerous-function denylist, comment/case/stacked-query bypass resistance, resource limits (row cap, query timeout, cost estimation) — **no bypass found**, consistent with Phase 1's own findings. New this pass: explicit regression tests for encoding/homoglyph bypass and NUL-byte smuggling (`tests/test_sql_validator_hardening.py::TestEncodingBypass`), and the two restricted-column bypasses already closed in Phase 1. Concurrency control: SQL execution itself has a query timeout and row cap; whole-request concurrency (how many `/ask`/`/execute` calls run at once) remains unbounded (see "Remaining risks").

## 5. SSRF (final review)

`media_gen/download.py`'s existing hardening re-verified as genuine (resolves and checks the actual IP against a comprehensive private/reserved/cloud-metadata blocklist, HTTPS-only) — no duplicate implementation added. **New this pass**: a response-size limit (streamed download, aborted once `Settings.media_max_file_mb` is exceeded, checked against both a declared `Content-Length` and the actual streamed byte count) — the one concrete gap found relative to the full SSRF checklist (scheme allowlist, hostname validation, DNS/IP validation, redirect restrictions, timeout, **response-size limits**). The DNS-rebinding TOCTOU window (validated IP isn't pinned into the actual `requests.get` call) **remains open** — a properly-tested fix requires connection-level pinning this environment has no live endpoint to verify against safely; see "Remaining risks."

## 6. RAG security

Retrieved content (schema DDL, documents, web results, golden examples) confirmed already framed as untrusted data in every relevant system prompt — genuine prompt text, not just a docstring claim. The sensitivity gate (`rag/graph.py`) confirmed to never pass a restricted chunk's text to the LLM. **New this pass**: `conversation_history` — previously the one place untrusted, LLM-bound text bypassed `agent.input_guard.check_input` entirely — is now normalized (closing the same homoglyph gap the live question's own check closes) and injection-scanned (logged, not rejected — a prior turn is historical context, not the live request) via `agent.input_guard.sanitize_conversation_history`, called from `sanitize_input_node`. Golden-example trust boundary (Phase 1's API-04 finding: unverified provenance on saved examples) **remains open** — `POST /feedback/golden-example` is now at least authenticated/authorized (`GOLDEN_EXAMPLE_WRITE`), which meaningfully narrows who can poison the store, but the store still has no server-side verification that submitted SQL was ever actually generated/validated by this app.

## 7. File security

Verified: size limits, MIME validation by magic bytes (not extension/`Content-Type`), filename handling, path-traversal containment (`api/media_library.py`), bounded reads. **New this pass**: a PDF page-count cap (`Settings.max_document_pages`, enforced in `rag.ingestion.extract_pdf_pages` before extracting any page's text) — closes a decompression-bomb-shaped gap the existing byte-size cap alone didn't (a small file can still declare an absurd page count).

## 8. Secrets

Every secret field confirmed `pydantic.SecretStr`; no plaintext-secret call site found (re-verified across every file touched this pass). `.env` confirmed never committed, `.dockerignore` confirmed excludes `.env`/`.git`. New OIDC config (`OIDC_ISSUER`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL`) is correctly non-secret plain config. A lightweight pattern-based scan (AWS keys, PEM private keys, common API-key/token shapes) across tracked files found nothing. **Known, deliberately unfixed gap**: `frontend/src/lib/api.ts` still bundles `VITE_API_AUTH_TOKEN` into the public JS build at build time (a pre-existing Phase 1 finding) — fixing this properly requires a real browser-side OIDC login flow (redirect to IdP, token storage/refresh), a substantial frontend project of its own, correctly out of scope for this backend-focused pass. Documented here rather than silently left; see "Remaining risks."

## 9. CI/CD security

**Before:** lint, format-check, type-check, mocked test suite, and a report-only `pip-audit` step.

**Added** (`.github/workflows/ci.yml`): a dedicated secret-scanning job (`gitleaks`, full-history checkout), a SAST step (`bandit`, scoped to this project's own first-party packages), a Docker image build-and-scan job (`trivy`, CRITICAL/HIGH severity), and a `workflow_dispatch`-gated benchmark-regression job (real, not a stub — but only runnable on a self-hosted runner with live Ollama/DB access, which this repository's CI environment doesn't have; documented honestly rather than faked or silently omitted). All three new scanners are **report-only** for now (`continue-on-error: true`), consistent with the existing `pip-audit` precedent — none has been run against this repository before, so none has a triaged baseline yet; flipping each to a hard gate is a one-line change once that triage happens. **This workflow was validated for YAML correctness and correct action usage, not by an actual GitHub Actions run** (no live CI runner in this environment) — flagged honestly, not claimed as fully verified.

## 10. Docker security

Verified already solid: non-root user, digest-pinned base images, healthcheck, `.dockerignore` excludes secrets, localhost-bound by default. **Added**: `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and CPU/memory resource limits in `docker-compose.yml`. **Deliberately not added**: a fully read-only root filesystem — this app writes to more paths than the one existing named volume covers (voice models, video keyframe thumbnails, model caches), and getting every one of those right requires verification against a live running container this environment can't perform; recommended as a properly-tested Phase 3 item rather than risked here.

## 11. Tests

998→1011 tests added across this pass (`tests/test_oidc.py`, `tests/test_api_authz.py`, additions to `tests/test_orchestrator.py`, `tests/test_nodes_security_wiring.py`, `tests/test_settings_validation.py`, `tests/test_media_gen.py`, `tests/test_sql_validator_hardening.py`, `tests/test_rag_ingestion.py`, `tests/test_adversarial_input.py`), all passing, zero regressions in the pre-existing suite. Covers: authentication (valid/expired/forged/malformed JWTs, alg-confusion), authorization (vertical/horizontal privilege escalation, missing/invalid roles, cross-user), tenant-isolation-adjacent behavior (per-role source authorization in the orchestrator), SSRF (response-size limits), SQL security (encoding/homoglyph/NUL-byte bypass attempts), prompt injection (conversation-history sanitization), RAG poisoning (unchanged, already covered), uploads (PDF page-count cap), rate limits (bounded-cache eviction), cost controls (session-ceiling identity binding), secret handling (no-token-in-logs), API security (the full endpoint matrix above, exercised end-to-end).

## Remaining risks (honestly carried forward, not hidden)

| Risk | Severity | Why not fixed this pass |
|---|---|---|
| SSRF DNS-rebinding TOCTOU window | Medium | Requires connection-level IP pinning; no live endpoint here to verify a fix doesn't silently break TLS validation |
| No whole-request timeout / concurrency limiter on `/ask` | Medium-High | A real, separately-scoped reliability change; risk of destabilizing request handling without load-testing infrastructure this environment lacks |
| Frontend still bundles a static auth token | Medium | Real fix is a full browser OIDC login flow — a separate frontend project |
| Golden-example provenance unverified | Medium | Now behind RBAC (narrows exposure); full fix needs a signed-token/echo mechanism tying a save back to a real prior `/ask` |
| 6 low-cost routes lack dedicated rate limits / 2 lack audit events | Low | Now behind RBAC (primary risk closed); defense-in-depth follow-up |
| No RBAC per-resource ownership (any `admin` manages any document) | Low-Medium | Matches this app's pre-existing single-organization design; a real ownership model is Phase 3-scale work |
| `langgraph` major-version dependency upgrade | Low (not reachable today) | Real regression risk of its own, deliberately deferred with reachability analysis in `docs/DEPENDENCY_SECURITY.md` |
| CI security scanners unvalidated against a real run | — | No live GitHub Actions runner in this environment; YAML correctness verified, execution not |

See `docs/THREAT_MODEL.md` for the structured STRIDE analysis behind these, and `docs/PHASE2_FINAL_REPORT.md` for the overall score.
