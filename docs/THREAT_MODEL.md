# Threat Model

STRIDE-style analysis of the Text-to-SQL agentic AI platform, as of 2026 Phase 2. Reflects the actual verified implementation (`docs/PHASE1_BASELINE.md`, `docs/PHASE2_SECURITY_REPORT.md`), not an idealized design.

## Assets

| Asset | Why it matters |
|---|---|
| Configured database contents | The core data this app exists to answer questions about — potentially includes PII, financial data, HR records |
| Uploaded documents (general + sensitivity-tagged) | Compensation/disciplinary/legal-category files are explicitly the most sensitive data class this app handles |
| Database/service credentials (`.env`) | DB passwords, OIDC config, Azure/Tavily/IMA API keys |
| The LLM's system prompts / internal reasoning | Not secret in a compliance sense, but a leak aids further attack (e.g., crafting a more effective injection) |
| Generated/searched media | Company-owned images/video, plus real-money IMA Studio spend |
| Audit/security event log | The record needed to detect and investigate any of the above being attacked |
| Golden-example store | Poisoning it biases every future user's query generation — an integrity asset, not just a confidentiality one |
| Service availability / cost budget | Ollama compute, IMA Studio credits, Tavily API calls all cost real money or capacity |

## Spoofing (identity)

| Threat | Impact | Likelihood | Mitigation | Remaining risk |
|---|---|---|---|---|
| Attacker forges a JWT to impersonate a legitimate user | Full access as whatever role the forged token claims | Low | Signature verified against the real IdP's JWKS public key; algorithm allowlisted server-side, never trusted from the token (`security/oidc.py`) | None found — see `tests/test_oidc.py`'s forged-signature/alg-confusion coverage |
| Attacker replays an expired/stale token | Access after the token should have expired | Low | `exp`/`iat` validated with bounded clock-skew leeway | Leeway window itself (60s default) is a small, accepted residual window |
| Attacker impersonates the static shared token holder | Full admin access (the token grants no sub-identity) | Medium (if `API_AUTH_TOKEN` leaks) | Timing-safe comparison; `SecretStr` typed; never logged | Inherent to a shared-secret design — mitigated by preferring OIDC in production, not eliminated for the static-token mode by design |
| A caller's `sub` claim alone is trusted as authorization | Privilege escalation via a crafted/naive `sub` value | Low | Authorization reads `roles`, never `sub`, for any permission decision (`tests/test_api_authz.py::TestHorizontalPrivilegeEscalation`) | None found |

## Tampering (data/message integrity)

| Threat | Impact | Likelihood | Mitigation | Remaining risk |
|---|---|---|---|---|
| LLM-generated SQL is tampered/malicious (prompt injection succeeds) | Data exfiltration, disallowed statement execution | Medium (LLM compliance isn't guaranteed) | `agent/sql_validator.py`'s AST allowlist enforces safety independent of what the LLM produces — verified no bypass | Residual: a validator bug could exist; mitigated by extensive, actively-maintained regression coverage |
| Uploaded document content poisons retrieval/generation | Misleading/malicious answers | Low-Medium | Retrieved content explicitly framed as untrusted data in every relevant system prompt; RAG-poisoning detection scan (detection-only) | Detection-only, not blocking — a determined, subtle poisoning attempt could still succeed at influencing an answer (bounded by the SQL validator on the SQL path) |
| Golden-example store is poisoned with plausible-but-wrong (question, SQL) pairs | Future users get confidently wrong answers | Medium (now requires `GOLDEN_EXAMPLE_WRITE`, previously unauthenticated) | RBAC-gated as of this pass | No provenance check that submitted SQL was ever actually generated/validated by this app — open, see Phase 2 report |
| Conversation history tampered to smuggle instructions | Same as prompt injection, via a side channel | Low-Medium | Now normalized + injection-scanned (`agent.input_guard.sanitize_conversation_history`), same structural backstops (prompt framing, SQL validator) apply | Detection-only for history (by design — see rationale in `docs/PHASE2_SECURITY_REPORT.md`) |
| A response from a compromised/malicious provider (IMA, Tavily) is trusted | SSRF via a malicious media URL; misleading "live web" content | Low | IP-validated download (SSRF-hardened), size-capped; web content explicitly framed as untrusted | DNS-rebinding TOCTOU window still open (see Phase 2 report) |

## Repudiation (accountability)

| Threat | Impact | Likelihood | Mitigation | Remaining risk |
|---|---|---|---|---|
| An authenticated user denies performing a destructive action | No way to prove who deleted a document / ran a query | Medium (was: no per-user identity to attribute to at all) | OIDC gives a real `sub` per caller; `authz_denied`/`auth_failed`/generation/moderation events are audit-logged with subject | Two mutating actions (`DELETE /documents/{id}`, `POST /feedback/golden-example`) still have no dedicated audit event (Phase 2 report gap list) |
| Audit log itself is tampered/deleted | No investigation possible after an incident | Medium | Structured, correlation-ID-tagged logging exists | Log is a plain stdout stream, not append-only/tamper-evident storage — a pre-existing, self-disclosed gap (`docs/RISK_REGISTER.md`), not addressed this pass |

## Information Disclosure (confidentiality)

| Threat | Impact | Likelihood | Mitigation | Remaining risk |
|---|---|---|---|---|
| Unauthorized access to restricted database columns | PII/sensitive data exposure | Low (was: Medium — two confirmed bypasses) | Both `SELECT *` and out-of-context-table bypasses closed (Phase 1); now additionally role-gated (`VIEW_RESTRICTED_COLUMNS`) | None found for the closed bypasses; the classification file itself must still be populated by an operator to have any effect |
| Unauthorized download of sensitivity-tagged HR/legal documents | Direct compliance/privacy breach | Low (was: Critical — fully unauthenticated) | Now requires `DOCUMENTS_READ_SENSITIVE`, checked per-document before the response is built | Sensitivity tagging itself is still self-declared at upload time with no independent verification |
| Unauthorized access to the "policies" RAG collection via the orchestrator | Same as above, via chat | Low (was: High — LLM routing alone decided) | `router_node` now authorization-filters before any subgraph runs (closes AGT-01/R-008) | The "restricted content exists" oracle (a distinguishable refusal message) is unchanged from Phase 1 — not addressed this pass |
| Secret leakage via logs/errors/frontend | Credential compromise | Low | `SecretStr` everywhere, `.safe_message` exception contract, `redact_secrets`, pattern-based scan found nothing | Frontend still bundles the static auth token in the JS build (not a "secret" in the SecretStr sense, but a credential nonetheless) — open |
| `/health` leaks DB engine/version to any unauthenticated caller | Reconnaissance aid | Low | Deliberate design tradeoff (orchestrator reachability) | Unchanged, self-disclosed, low severity |
| SSRF reaching internal infrastructure (cloud metadata, localhost) via media download | Internal service compromise, credential theft (e.g. cloud IMDS) | Low | Resolved-IP validated against a comprehensive blocklist before every fetch | DNS-rebinding TOCTOU window (see above) |

## Denial of Service

| Threat | Impact | Likelihood | Mitigation | Remaining risk |
|---|---|---|---|---|
| Unbounded rate-limiter memory growth (client-varied keys) | Process memory exhaustion | Low (was: Medium-High) | `BoundedLimiterCache` (LRU eviction) closes this (Phase 1) | None found |
| A single caller exhausts the process-wide LLM-call budget | Denies service to every other concurrent user | Medium | Rate limiter exists | Still process-global, not per-caller (self-disclosed, R-007) — not addressed this pass |
| No whole-request timeout on `/ask` | A slow/hung Ollama backend ties up worker threads indefinitely | Medium | Per-sub-operation timeouts exist (Ollama call, DB query) | No request-level deadline — open, flagged as a priority Phase 3 item |
| No concurrency limiter on in-flight requests | Thread-pool exhaustion under burst load | Medium | None specific | Open — relies entirely on Starlette's unconfigured default |
| A malicious PDF with an absurd page count | CPU/memory exhaustion during ingestion | Low (was: Medium — no cap existed) | `Settings.max_document_pages` cap added this pass | None found for this specific vector |
| A malicious/compromised media provider response with unbounded size | Memory exhaustion on download | Low (was: Medium — unbounded `requests.get`) | Streamed, size-capped download added this pass | None found |
| Denial-of-wallet via repeated paid media generation | Real financial cost | Low | Human-approval gate (server-enforced) + session cost ceiling + rate limiters | Session ceiling is keyed to authenticated subject when available (this pass) but still falls back to a resettable `session_id` for non-OIDC callers |

## Elevation of Privilege

| Threat | Impact | Likelihood | Mitigation | Remaining risk |
|---|---|---|---|---|
| Vertical privilege escalation (`viewer`/`user` reaching an `admin`-only action) | Full compromise of data/config | Low (was: N/A — no privilege tiers existed at all) | RBAC enforced server-side at every route and inside the orchestrator; verified via `tests/test_api_authz.py::TestVerticalPrivilegeEscalation` | None found in testing |
| Horizontal privilege escalation (one user acting as another) | Cross-user data access | Low | Authorization is role-based, not identity-based — a distinct `sub` grants nothing extra by itself; verified via `TestHorizontalPrivilegeEscalation` | No per-resource ownership model (an `admin`'s reach is intentionally broad — see Phase 2 report) |
| LLM alone authorizes a sensitive action (the orchestrator's own router) | The model effectively grants itself access to restricted sources | Low (was: Medium-High — this was the literal mechanism before this pass) | `router_node`'s post-classification authorization filter — the LLM proposes, a deterministic check disposes | None found; this is the core AGT-01 fix |
| Unrecognized/invalid role name silently granted broad access | Fail-open on a misconfiguration | Low | `agent.authz.permissions_for` grants nothing for an unrecognized role — verified (`TestMissingAndInvalidRoles`) | None found |
| Static-token/no-auth mode implicitly grants `admin` | Any holder of a single shared secret has full access | Medium (inherent to those two modes' design) | Documented, deliberate continuation of pre-existing behavior (not a new privilege) — the real mitigation is deploying OIDC in production, enforced by the fail-closed `ENVIRONMENT=production` check | Accepted by design for local/dev and service-account use; not appropriate for a genuinely multi-user production deployment without OIDC |

## Summary

The highest-severity items this pass closed were **Elevation of Privilege** (no authorization system at all → RBAC everywhere, including inside the orchestrator) and the **Information Disclosure** items downstream of it (unauthorized document/column/policy access). The highest-severity items still open are concentrated in **Denial of Service** (no whole-request timeout, no concurrency limiter, process-global LLM rate limiting) and one **Tampering** item (SSRF DNS-rebinding) — all three require either live-infrastructure verification this environment lacks, or a separately-scoped reliability project, and are honestly carried forward rather than rushed or hidden. See `docs/PHASE2_FINAL_REPORT.md` for the prioritized remaining-risk list and overall score.
