# Incident Response

**First version of this document.** `SECURITY_FINAL_REPORT.md` §25
explicitly recorded this as "NOT PRODUCED" — a named, prioritized gap
carried across every prior security pass on this repository until now.
Treat this as a first draft grounded in this app's actual real detection
signals and real containment levers (every event name and config flag
below was verified to exist in the current codebase, not invented) —
**not** a rehearsed, on-call-tested runbook. It has not been exercised
against a simulated incident. See the final line of each scenario for
what verifying it for real would require.

## How detection actually works in this app

Every scenario below leans on the same mechanism:
`security.audit_log.log_security_event(event_type, severity, message,
**context)` — one structured event, one dedicated `security.audit` logger,
correlation-ID-tagged. **This app has no SIEM/alerting integration of its
own** (confirmed — no Prometheus/alerting client in `requirements.txt`);
these events reach wherever the operator's own log pipeline points
`stdout`/`stderr`. Grep-confirmed inventory of every event type this app
actually emits (not a hypothetical list):

```
auth_failed, oidc_token_rejected, authz_denied, user_registered,
account_locked, refresh_token_reuse_detected, logout_all_sessions,
password_changed, password_reset_requested, password_reset_completed,
input_rejected, conversation_history_injection_detected,
possible_rag_poisoning, sql_safety_violation, sensitive_column_blocked,
rate_limit_tripped, rag_role_restricted_content_blocked,
content_moderation_rejected, content_moderation_soft_flagged,
malware_detected, malware_scan_error_fail_closed, malware_scan_skipped,
db_write_privileges_detected, orchestrator_source_denied,
session_expensive_source_limit_tripped, generation_rejected,
generation_rate_limited, generation_provider_failed
```

## 1. Credential compromise (static token / OIDC client secret / API keys)

- **Detection:** `auth_failed` events from an unfamiliar IP/pattern;
  a spike in `oidc_token_rejected`.
- **Severity:** Critical if `auth_mode=static_token` (a single shared
  secret grants full admin-equivalent access — see `docs/RISK_REGISTER.md`
  R-001).
- **Containment:** Rotate `API_AUTH_TOKEN` immediately (`.env`, redeploy —
  no revocation list exists, the old value simply stops matching).
  For OIDC, rotate the client secret at the IdP and revoke active sessions
  at the IdP (this app doesn't hold IdP-side session state to revoke
  itself).
- **Investigation:** Correlate `auth_failed`/`oidc_token_rejected` events
  by correlation ID against any subsequent `authz_denied` or successful
  action from the same window.
- **Not verified:** No tooling exists in this app to force-invalidate a
  specific already-issued JWT before its natural expiry (standard JWT
  statelessness) — compensating control is a short `OIDC_MAX_TOKEN_AGE`-style
  expiry (verify actual claim name in your IdP config) plus IdP-side
  session revocation.

## 2. Account takeover (local accounts, `identity/`)

- **Detection:** `account_locked` (repeated failed logins),
  `refresh_token_reuse_detected` (a stolen, already-used refresh token
  replayed — `identity/repositories/sessions.py`'s reuse-detection revokes
  the *entire* session family on this signal, confirmed by reading that
  module).
- **Containment:** `logout_all_sessions` is the same primitive a user can
  self-trigger; an operator can force it via the same repository function
  for a specific `user_id`.
- **Investigation:** `password_changed`/`password_reset_completed` events
  immediately after a suspected takeover indicate the attacker locking the
  legitimate owner out — treat as high-confidence confirmation.
- **Not verified:** No automated response to `refresh_token_reuse_detected`
  beyond the family-revocation already built in — no alert is sent
  anywhere (this app has no outbound alerting channel configured).

## 3. SQL abuse (attempted dangerous/unauthorized query)

- **Detection:** `sql_safety_violation` (validator rejected the
  statement type/shape), `sensitive_column_blocked` (restricted-column
  gate fired).
- **Severity:** Low in practice — `agent/sql_validator.py`'s AST allowlist
  means a "SQL abuse" event is, by construction, a **rejected** query, not
  an executed one. A real incident here would mean the validator itself
  had a bypass — treat any *sustained* pattern of `sql_safety_violation`
  from one caller as a signal to investigate the validator's own coverage,
  not just block the caller.
- **Containment:** The caller's DB-role read-only-ness (`db.connection
  .check_write_privileges`, startup-enforced in production per
  `api/main.py`) is the real backstop if the validator were ever bypassed
  — verify that check's own startup log for the affected deployment.
- **Not verified:** No automated per-caller lockout on repeated
  `sql_safety_violation` — currently relies on the existing rate limiter
  alone.

## 4. Data leakage (unauthorized read of sensitive data)

- **Detection:** `authz_denied` (RBAC blocked it — the success case),
  `rag_role_restricted_content_blocked`. A genuine leak would show *no*
  denial event at all for the read that shouldn't have happened — absence
  of a denial is not absence of evidence; cross-reference actual query/
  document-access logs (application-level, not security-event-level) for
  the affected window.
- **Containment:** Revoke the responsible identity's roles
  (`identity/repositories/users.py`); for a document-level leak, `DELETE
  /documents/{id}` removes the source (cascades to chunks, confirmed via
  `ON DELETE CASCADE` per `SECURITY_FINAL_REPORT.md` §11).
- **Not verified:** No data-loss-prevention/egress-scanning exists in this
  app — detection here depends entirely on the access-control logs above
  being complete, not on detecting the leaked content itself in transit.

## 5. Prompt injection (successful, not just attempted)

- **Detection:** `input_rejected` (caught pre-generation) or
  `conversation_history_injection_detected` (caught in history, not
  blocking). A **successful** injection producing a malicious SQL
  statement would instead surface as `sql_safety_violation` — the
  validator remains the real backstop regardless of the injection vector
  (verified this engagement, `tests/security/test_prompt_injection_multilingual_and_encoded.py`).
- **Containment:** None needed beyond the existing validator if the SQL
  path is what was targeted (confirmed to hold). If a *non-SQL* answer
  path (RAG/web-search synthesis) was manipulated into an inappropriate
  response, there is no automated containment — manual review of the
  specific conversation via the audit log's correlation ID is the only
  lever today.
- **Not verified:** No semantic/ML-based injection classifier exists —
  the regex layer's blind spots (Base64/URL-encoding/non-English phrasing,
  documented this engagement) mean some injection *attempts* will never
  generate a detection event at all, only the downstream structural
  backstop (SQL validator) protects against consequence, not detection.

## 6. RAG poisoning (malicious ingested document/database content)

- **Detection:** `possible_rag_poisoning` (detection-only scan over
  extracted PDF text or retrieved schema/sampled-value content, fires on
  the same shared injection-pattern set as live questions).
- **Containment:** `DELETE /documents/{id}` for a poisoned document.
  For poisoned live-database content (a value crafted to look like an
  instruction), there is no deletion lever in this app at all — the fix
  is in the operator's own source database.
- **Not verified:** Detection-only by design (see `agent/nodes.py`'s own
  docstring) — a sufficiently subtle poisoning attempt that doesn't match
  the shared regex pattern set produces no event at all.

## 7. Malicious upload (non-malware content policy violation)

- **Detection:** `content_moderation_rejected` (hard-reject — Azure
  Content Safety + text blocklist), `content_moderation_soft_flagged`
  (recorded, not blocked — includes the disclosed "synthetic media,
  not_checked" placeholder).
- **Containment:** Rejected content is never stored (confirmed —
  `moderation.store.record_asset` records only the hash/outcome on
  rejection, per `rag/ingestion.py`'s own docstring, independently
  re-read this session).
- **Not verified:** No incident-response-specific tooling beyond the
  existing gate — a moderation-provider outage fails ingestion closed
  (`ModerationNotConfiguredError`), not silently open, which is itself the
  containment.

## 8. Malware (binary malware in an upload)

- **Detection:** `malware_detected` (signature match — critical),
  `malware_scan_error_fail_closed` (scanner configured but couldn't
  complete — treated as a rejection, not a pass), `malware_scan_skipped`
  (provider is `disabled` — the default; see
  `docs/security/FINAL_PRODUCTION_GATE.md`'s Malware Scanning row for why
  this is a real, disclosed production gap, not resolved by this
  document).
- **Containment:** Rejected uploads are never stored (same mechanism as
  §7). If `malware_scan_skipped` events exist for a deployment that has
  file upload enabled, that is itself the incident to act on —
  reconfigure `MALWARE_SCAN_PROVIDER=clamav` immediately.
- **Not verified:** This entire capability has never been exercised
  against a real `clamd` daemon or a real (even EICAR-test-string) sample
  in this engagement — see `docs/security/FINAL_PRODUCTION_GATE.md`.

## 9. SSRF (attempted or successful)

- **Detection:** No dedicated `log_security_event` call exists for a
  blocked SSRF attempt today — `media_gen/download.py::_validate_download_url`
  raises `MediaGenerationError` (logged via the ordinary exception path,
  not the structured audit-event path) — **a real, disclosed gap found
  while writing this document**, not previously flagged in any prior pass.
- **Containment:** The blocklist itself (broad private/loopback/
  link-local/cloud-metadata ranges, IPv4+IPv6) is the primary control;
  confirmed extensively tested (`tests/test_media_gen.py`).
- **Not verified / recommended follow-up:** Add a `log_security_event`
  call to `_validate_download_url`'s rejection paths so an SSRF attempt is
  as visible in the audit log as every other rejection class in this
  document — not done as part of this pass (a code change beyond this
  gate's own "do not make unnecessary changes" scope, flagged as a
  concrete P1 instead — see `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`).

## 10. Dependency compromise (a trusted package turns malicious)

- **Detection:** None automated — no Dependabot/Renovate exists (§9 of
  `docs/security/DEPENDENCY_SECURITY_FINAL_REPORT.md`). A compromised
  dependency would only surface via `pip-audit`/`npm audit`/Trivy's own
  next manual run, or via anomalous application behavior.
- **Containment:** Pin/rollback the specific package in `requirements.txt`/
  `package-lock.json`, rebuild, redeploy.
- **Not verified:** No SBOM-diffing/provenance-verification tooling
  beyond the SBOMs generated this engagement
  (`docs/security/sbom/`) — those are a point-in-time snapshot, not a
  continuous integrity check.

## 11. API abuse (scraping, credential stuffing, cost exhaustion)

- **Detection:** `rate_limit_tripped`,
  `session_expensive_source_limit_tripped` (the multi-source orchestrator's
  own per-session cost ceiling for paid sources).
- **Containment:** Existing per-IP/per-action rate limiters
  (`api/rate_limit.py`) — confirmed process-local only (see the Rate
  Limiting row in `docs/security/FINAL_PRODUCTION_GATE.md`); a
  distributed/multi-instance attacker could exceed the *intended* global
  budget by hitting different instances, though each instance's own limit
  still individually holds (verified this engagement via
  `tests/security/test_rate_limit_header_spoofing.py`'s header-spoofing
  regression, a related but distinct property).
- **Not verified:** No IP-ban/WAF-layer response exists in this app itself
  — that's expected to sit at a reverse proxy/WAF layer this app doesn't
  own, per `docs/DEPLOYMENT.md`'s reverse-proxy pattern.

## 12. Container compromise

- **Detection:** None application-level — this is infrastructure-layer
  (the operator's own container runtime/orchestrator logging, not
  anything this app emits itself).
- **Containment:** Non-root user (`USER app`, uid 1000, confirmed this
  engagement via `docker run ... id`) limits blast radius of a
  process-level compromise; redeploy from a known-good, re-scanned image.
- **Not verified:** No runtime container security monitoring (Falco or
  equivalent) is part of this project — an infrastructure-layer decision
  for the operator, out of this app's own scope.

## What "verifying this for real" would require

A tabletop exercise walking at least scenarios 1, 2, 8, and 9 (the ones
with the most concrete, testable detection signals) with whoever will
actually be on call for a real deployment — not performed as part of this
engagement (no on-call rotation or real deployment exists in this
sandboxed environment to exercise this against).
