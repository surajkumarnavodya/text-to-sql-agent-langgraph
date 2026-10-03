# Production Security Readiness Report — Final Gate

**Date:** 2026-09-18. This is the terminal document of this engagement —
it does not re-derive evidence already established in
`docs/security/FINAL_PRODUCTION_GATE.md`, `CVE_TRIAGE.md`,
`DEPENDENCY_SECURITY_FINAL_REPORT.md`, `LANGGRAPH_UPGRADE_SECURITY_ASSESSMENT.md`,
`INCIDENT_RESPONSE.md`, `OIDC_E2E_TEST.md`, `DAST_REPORT.md`, or
`THREAT_MODEL.md` — it classifies and rules on it.

> **Superseded (Prompt 25, 2026-10-03):**
> [`../PRODUCTION_READINESS_RELEASE_GATE.md`](../PRODUCTION_READINESS_RELEASE_GATE.md)
> is now the current, authoritative production-readiness document — this
> report predates Prompts 19-24 (SQL Server Query Store intelligence,
> multi-tenant architecture, a dedicated enterprise-security hardening
> pass, scale/performance hardening, observability/evaluation, and
> full-pipeline integration validation). Its three P0 findings (DAST,
> live-OIDC verification, live malware-scanner verification) remain open
> and environment-blocked exactly as stated below — nothing in the newer
> work closed them, since none of it had access to a staging environment,
> a live IdP, or a real ClamAV daemon either. Kept as historical evidence,
> not re-derived or rescored.

## 1. Executive Summary

This application's **tested, code-level security architecture is
genuinely strong**: an AST-based SQL validator with no known bypass across
an extensive, actively-maintained regression suite; RBAC wired into every
checked route *and* the multi-source router's own access decision; IDOR
protection verified by dedicated cross-user tests; SSRF defense that
resolves and validates IPs post-DNS, not just string-matches URLs; secret
handling with zero real findings after two independent scans; SAST at
zero issues; and dependency CVE exposure that, after rigorous,
file-path-level reachability verification this engagement, currently has
no confirmed exploitable path in this application's actual configuration.

**None of that is what this report's verdict turns on.** Per this gate's
own explicit decision rule, incomplete verification of a mandatory
production control is itself a `NOT READY` trigger, independent of
whether a concrete vulnerability was found. Three such gaps exist and
were not closeable from this sandboxed environment: **DAST has never
run**, **OIDC has never been verified end-to-end against a live Identity
Provider**, and **malware scanning defaults off and has never been
verified against a real scanner**. A fourth — incident response
documentation — was fully absent before this session and has been given a
first draft, but a first draft is not a rehearsed capability.

## 2. Overall Security Posture

Strong foundation, incomplete production verification. Not a
euphemism — see the gate matrix (`FINAL_PRODUCTION_GATE.md`): 15 of 25
domains are a clean, evidence-backed `PASS`; 10 are not, and every one of
those 10 is individually scoped and explained rather than lumped into a
vague caveat.

## 3. Production Gate Matrix

See `docs/security/FINAL_PRODUCTION_GATE.md` (full 25-row table). Summary
counts: **PASS: 15 · PARTIAL: 8 · FAIL: 1 · NOT VERIFIED: 1**.

## 4. P0 Blockers

| # | Issue | Component | Impact | Evidence | Status |
|---|---|---|---|---|---|
| P0-1 | DAST never executed against this application, at any point in this repository's history | Whole application | Unknown — active scanning (fuzzing, spidering, method probing) tests classes of bug no unit/integration test is designed to find | `docs/security/DAST_REPORT.md` | NOT VERIFIED |
| P0-2 | OIDC never verified end-to-end against a live IdP | Authentication (OIDC mode specifically) | Unknown — mocked tests can't catch a real IdP claim-shape mismatch, browser iframe/cookie policy issue, or key-rotation handling bug | `docs/security/OIDC_E2E_TEST.md` | NOT VERIFIED |
| P0-3 | Malware scanning defaults to `disabled`; the `clamav` path has never been run against a real daemon | File upload (`ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG`/`ENABLE_MEDIA_SEARCH`) | An operator who enables upload features without separately setting `MALWARE_SCAN_PROVIDER=clamav` gets zero malware scanning, silently | `docs/security/FINAL_PRODUCTION_GATE.md` Malware Scanning row; `security/malware_scanner.py` | PARTIAL — code correct and unit-tested, real-world unverified |

**None of these three represents a known, exploitable vulnerability in
this application's code.** They represent verification this environment
could not perform. That distinction matters for remediation planning (see
§27) but does not change the verdict math per this gate's own rule.

## 5. P1 Issues

| # | Issue | Component | Impact | Evidence |
|---|---|---|---|---|
| P1-1 | Incident response doc was fully absent; now first-drafted, not rehearsed | Operations | Slower, less coordinated response to a real incident | `docs/security/INCIDENT_RESPONSE.md` |
| P1-2 | Rate limiting is process-local only, no distributed option | Availability (multi-instance deployments only) | A determined attacker distributed across instances exceeds the *intended* global budget (each instance's own limit still holds individually) | `agent/rate_limit.py`'s own docstring; confirmed zero Redis usage |
| P1-3 | `langgraph` 0.2.62, 10 CVEs not reachable today but migration pending | Core orchestration engine | Architectural-drift risk — reachability could change if a future feature adds a checkpointer/cache | `docs/security/LANGGRAPH_UPGRADE_SECURITY_ASSESSMENT.md` |
| P1-4 | ~9 Debian OS-package CVEs in the container image not independently reachability-verified | Container base image | Unknown — same honest gap as P0-1/P0-2's shape, scoped smaller (OS packages, not the whole app) | `docs/security/CVE_TRIAGE.md` §4 |
| P1-5 | No automated dependency-update monitoring (no Dependabot/Renovate) | Supply chain | A new advisory goes unnoticed between manual scan passes | `docs/security/DEPENDENCY_SECURITY_FINAL_REPORT.md` §9 |
| P1-6 | SSRF rejections aren't audit-logged (`media_gen/download.py::_validate_download_url` raises without calling `log_security_event`) | Observability | An SSRF attempt leaves no structured signal in the one place every other rejection class does | Found and verified this session while writing `INCIDENT_RESPONSE.md` §9 |
| P1-7 | `mypy .` (the exact CI invocation) fails immediately with a module-resolution error, before producing any per-file result | CI/CD | The "blocking" mypy gate may not be providing the protection its configuration implies | Confirmed via `git stash` (reproduces on a clean checkout) during the dependency-security session |
| P1-8 | No whole-request timeout or concurrency limiter on `/ask` | Availability | A hung Ollama backend or burst load can exhaust worker threads | `docs/THREAT_MODEL.md`'s own DoS section, independently re-read this session |
| P1-9 | No documented backup/recovery procedure | Operations | Unknown recovery time/data-loss exposure for any operator's own database | `docs/DEPLOYMENT.md` (zero mentions, confirmed via grep) |

## 6. P2 Improvements

- Dev tooling (`pytest`/`black`/`ruff`/`mypy`) ships inside the production
  Docker image despite never being invoked there — `requirements.txt`/
  `requirements-dev.txt` split recommended, not performed
  (`docs/security/DEPENDENCY_SECURITY_FINAL_REPORT.md` §9).
- GitHub Actions are tag-pinned, not SHA-pinned (already disclosed,
  unchanged).
- No container-image-level SBOM (only backend/frontend package-level
  SBOMs exist — `docs/security/sbom/`).
- `docs/THREAT_MODEL.md` not refreshed to reflect Phase 3+/this
  engagement's own new controls (`docs/security/THREAT_MODEL.md`'s own
  delta section lists exactly what's missing).
- No typosquatting/malicious-package-name detection tooling.
- No metrics/alerting integration (health/liveness checks exist; nothing
  beyond that).
- Pre-existing `ruff`/`black` findings in 9-10 files this engagement never
  touched (confirmed pre-existing via `git stash`, not introduced).

## 7. Security Controls Verified

Authentication (JWT/local/static-token code paths), RBAC (vertical +
horizontal escalation), IDOR (conversation + document ownership), SQL AST
validation (every listed attack shape), SSRF (resolved-IP + redirect
re-validation), rate limiting (process-local, header-spoof-proof),
security headers, CORS wildcard rejection, secret handling, SAST, file
upload validation, malware-scanner fail-closed logic (mocked), prompt-
injection structural backstop, RAG access gates. Each with a specific,
re-run-this-session test file cited in `FINAL_PRODUCTION_GATE.md`.

## 8. Security Controls Not Verified

OIDC live flow, DAST, malware scanning against a real daemon, distributed
rate limiting (doesn't exist to verify), backup/restore, ~9 OS-package
CVE reachability, gitleaks (tooling unavailable this whole engagement —
runs natively in GitHub Actions regardless).

## 9. Vulnerabilities Fixed

This final-gate session made **no code changes** (per its own Rule #2 —
no vulnerability, blocker, failed test, or broken config was found that
required one). All fixes (malware scanner, CI gate hardening, CVE
reachability documentation) were completed in prior sessions of this same
engagement and independently re-verified, not re-fixed, here.

## 10. Vulnerabilities Remaining

14 application-dependency CVEs (10 NOT REACHABLE, 4 DEV ONLY) + 29
container-level CVEs (4 NOT REACHABLE, ~17 OS-level partially verified).
Zero confirmed-exploitable findings in either set. Full detail:
`docs/security/CVE_TRIAGE.md`.

## 11. Dependency/CVE Results

Re-run fresh this session, identical to the immediately-prior dependency-
security session: `pip-audit` 24 raw/16 unique/14 CVE, `npm audit` 0,
zero drift. See `docs/security/DEPENDENCY_SECURITY_FINAL_REPORT.md`.

## 12. SAST Results

`bandit` re-run fresh this session: **0 issues**, 18,123 lines.

## 13. DAST Results

**NOT VERIFIED.** See `docs/security/DAST_REPORT.md`.

## 14. Secret Scan Results

`detect-secrets` re-run fresh this session: 20 files / 46 matches, zero
drift from the already-triaged all-false-positive baseline. `gitleaks`
NOT VERIFIED (tooling unavailable, this whole engagement).

## 15. Container Scan Results

Trivy run for the first time this engagement (prior sessions' build never
completed in time): 29 unique CRITICAL/HIGH findings, 4 confirmed NOT
REACHABLE (2 are vendored copies inside `pip`/`setuptools` themselves, 2
are build-time-only tooling), ~17 OS-level (partially verified — see
`docs/security/CVE_TRIAGE.md` §4). Non-root user and digest-pinned base
images confirmed.

## 16. Adversarial AI Security Results

Prompt injection: structural backstop (SQL validator) holds under
simulated full model hijack. Regex detection layer has confirmed,
disclosed blind spots (Base64/URL-encoding/non-Latin script) —
`tests/security/test_prompt_injection_multilingual_and_encoded.py` (14
tests, added this engagement, pass). RAG poisoning: detection-only scan
exists, not blocking — by design, disclosed. Tool/agent-escalation
attacks (unauthorized tool selection, recursive/excessive tool calls):
not independently re-tested this session — the orchestrator's own
router-level authorization filter (`agent/orchestrator/nodes.py`'s
`router_node`) was read and confirmed to run *after* LLM classification
and *before* any subgraph executes, the same structural pattern already
verified in prior sessions, not re-verified with new adversarial cases
this pass.

**2026-09-25 update — this section is now superseded by a materially
larger, live evidence base**: a 500-case externally-supplied prompt-
injection benchmark (`eval/security_benchmark/`,
`docs/security/PROMPT_INJECTION_BENCHMARK_GAP_REPORT.md`) was built,
unit-tested, and run to completion against the real live agent (real
Ollama, real SQL Server databases) — not a 14-case static regex-blind-spot
probe. Headline result: **all 500 cases completed, 0 critical findings**
(zero writes executed, zero unauthorized sources reached, zero secrets
leaked, zero system prompts leaked) — a materially stronger claim than
"the structural backstop holds under one simulated hijack," now backed by
500 live adversarial attempts across 20 categories including tool/agent-
escalation and cross-tenant/session-isolation specifically (the two
categories this section's prior text named as "not independently
re-tested"). Also produced a genuine, previously-unknown finding, fixed in
the same pass: `OPTION (MAXRECURSION 0)` (disabling MSSQL's own recursive-
CTE safety limit) passed `agent/sql_validator.py` unguarded and could hang
a live query — see the gap report §3c for the full incident writeup. A
true multi-turn persistence test (10 payloads × organic-turn-1 +
worst-case-simulated-turn-2, 0 critical findings) and the pre-existing
`conversation_id` IDOR/cross-tenant HTTP test
(`tests/test_api_chat_history.py::TestOwnershipIsolation`, 5/5 passing)
were also confirmed. **Honest residual gap, not closed by this update**:
66.4% overall pass rate means 168 of 500 cases are `expected_behavior`
mismatches (mostly over-engagement — the model answers where a refusal
was expected — never a hard-gate violation) that have not each been
individually root-caused; two categories in particular
(`Indirect multi-source injection`, `Indirect glossary/metric injection`)
have notably low content-refusal rates and are flagged for dedicated
follow-up in the gap report rather than claimed closed. Read
`docs/security/PROMPT_INJECTION_BENCHMARK_GAP_REPORT.md` for the current,
authoritative state of this control — it supersedes this section's
original text the same way this document's own conventions already treat
newer, more rigorously-sourced evidence as authoritative over older
summaries.

## 17. File Upload/Malware Security Results

Magic-byte validation, size caps, PDF page caps, mandatory moderation gate
— all tested, pass. Malware scanner: fail-closed logic unit-tested (18
tests, mocked), never run against a real daemon — PARTIAL, see P0-3.

## 18. SQL Security Results

AST-based allowlist, zero known bypass across an extensive suite covering
every attack shape this gate's Phase 6 lists (DROP/DELETE/UPDATE/INSERT/
multi-statement/CTE/UNION/wildcard/system-catalog/dangerous-function/
nested-aggregate/homoglyph/null-byte). Read-only DB engine, row cap, query
timeout confirmed.

## 19. Authentication/Authorization Results

See §7. Fail-closed production startup check confirmed
(`_require_identity_in_production`). OIDC live flow NOT VERIFIED (P0-2).

## 20. SSRF Results

Comprehensive, tested, pass. One observability gap found this session
(P1-6 — rejections not audit-logged).

## 21. Rate Limiting Results

Tested, pass, process-local only (P1-2).

## 22. Resource Exhaustion Results

Row/timeout/size caps confirmed present. Whole-request timeout/
concurrency limiter absent (P1-8, pre-existing, not re-verified further
this session beyond re-reading the existing disclosure).

## 23. CI/CD Security Gate Results

`bandit`/`pip-audit`/`detect-secrets` blocking, confirmed via direct read.
2 `continue-on-error` remain (gitleaks, Trivy), both documented. mypy
full-repo run issue noted (P1-7).

## 24. Operational Readiness

Health/liveness checks real and confirmed. No metrics/alerting. No backup
procedure. No incident-response rehearsal (first draft only).

## 25. Incident Response Readiness

First draft produced this session (`docs/security/INCIDENT_RESPONSE.md`),
grounded in this app's actual, grep-confirmed `log_security_event` event
inventory — not generic boilerplate. Not tabletop-tested.

## 26. Backup/Recovery Readiness

NOT VERIFIED — no procedure documented, no live database available to
test a restore against in this environment.

## 27. Remaining Accepted Risks

None formally accepted as part of this session — that determination
belongs to whoever owns the actual production deployment decision, not to
this assessment. What this report provides is the evidence and
classification (§4/§5/§6) that decision needs, not the decision itself.

## 28. Exact Commands Executed

`pytest -q` · `bandit -r agent api config db security media_gen
moderation rag search voice media embeddings -f txt` · `pip-audit -r
requirements.txt --desc` · `npm audit --json` (frontend) · `detect-secrets
scan $(git ls-files) --baseline .secrets.baseline` · `grep`-based
production-config audit (CORS/cookies/DEBUG/startup-fail-closed/hardcoded-
secrets/TODO-FIXME) · `docker run --rm text-to-sql-dashboard:localscan
[find|pip list|id]` (container reachability checks) · direct source reads
of `api/main.py`, `config/settings.py`, `media_gen/download.py`,
`agent/rate_limit.py`, `docs/AUTHENTICATION.md`, `docs/DEPLOYMENT.md`,
`docs/THREAT_MODEL.md`, `SECURITY_FINAL_REPORT.md`.

## 29. Files Changed During Final Gate

New: `docs/security/FINAL_PRODUCTION_GATE.md`,
`PRODUCTION_SECURITY_READINESS_REPORT.md` (this file), `INCIDENT_RESPONSE.md`,
`OIDC_E2E_TEST.md`, `DAST_REPORT.md`, `THREAT_MODEL.md`. **No application
code changed** — per this gate's own Rule #2, nothing found required a
code change (every finding is either a verification gap or already-tracked
from a prior session).

## 30. Final Production Status

# NOT READY

**This is not a statement that the application is insecure.** The tested,
verifiable security architecture — authentication, RBAC, IDOR protection,
SQL injection defense, SSRF defense, secret handling, SAST — is genuinely
strong and evidenced throughout this document set. This verdict reflects
three specific, named, currently-unverifiable-from-this-environment gaps
(P0-1 DAST, P0-2 OIDC E2E, P0-3 real-world malware scanning) that this
gate's own explicit decision rule treats as blocking regardless of code
quality: *"critical verification is incomplete for a mandatory production
control"* is listed as a `NOT READY` trigger independent of whether an
exploitable vulnerability was found.

**What would change this verdict to `READY WITH DOCUMENTED ACCEPTED
RISKS`:** closing P0-1 and P0-2 requires real infrastructure (a staging
deployment + ZAP; a live IdP tenant) this sandboxed session never had
access to — both are executable by whoever owns real deployment
infrastructure, using the procedures in `docs/security/DAST_REPORT.md`
and `OIDC_E2E_TEST.md`. P0-3 requires either standing up a real `clamd`
daemon and testing against it, or a deliberate, documented decision to
accept the risk of shipping without malware scanning for a deployment
that genuinely never enables file upload (`ENABLE_DOCUMENT_RAG`/
`ENABLE_POLICY_RAG`/`ENABLE_MEDIA_SEARCH` all stay off) — in which case
P0-3 is moot for that specific deployment shape, not fixed in general.

**A deployment that (a) doesn't use OIDC (stays on `static_token` behind
a trusted network, or accepts that mode's own documented shared-secret
tradeoff), (b) doesn't enable any file-upload feature, and (c) runs DAST
once against its own staging before going live** would close all three P0s
through configuration and one real scan, not a code change — worth
stating plainly since "NOT READY" should not be read as "far away," for a
deployment shaped that way.
