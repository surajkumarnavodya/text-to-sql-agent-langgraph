# PROMPT 25 — Production Readiness & Release Gate

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Perform the final backend/platform production-readiness review before product UI expansion.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect architecture, security, SQL safety, onboarding, semantics, analytics, recommendations, tenancy, observability, deployment and operations.

### Implementation requirements
Review architecture, database safety, AI evaluation, analytics correctness, recommendation evidence, tenant isolation, security, scale, backup/recovery, rollback, deployment and monitoring. Classify P0/P1/P2/P3 findings using concrete evidence. Implement justified P0/P1 fixes. Produce release checklist, unresolved risks, prerequisites and post-release monitoring.

### Testing requirements
Run final backend/security/integration tests.

### Acceptance criteria
Backend platform has a documented production-readiness state before UI prompts begin.

### Non-functional requirements
- Preserve existing functionality.
- Avoid duplicate implementations.
- Enforce authentication, authorization and tenant isolation.
- Never allow the LLM to bypass deterministic security controls.
- Keep secrets out of source code and logs.
- Add structured logging/tracing using the existing observability framework.
- Keep APIs backward compatible unless a documented migration is required.
- Update documentation and configuration examples.

### Required execution lifecycle
INSPECT → PLAN → IMPLEMENT → TEST → REVIEW → FIX → DOCUMENT → REPORT

Before broad implementation, present the implementation plan. After implementation, run relevant tests and fix regressions.

### Required final report
Report:
1. Areas inspected.
2. Existing functionality reused.
3. Files created/modified.
4. API/config/database changes.
5. Tests added.
6. Tests executed and results.
7. Security findings.
8. Tenant-isolation findings.
9. Performance implications.
10. Known limitations.
11. Remaining risks.
12. Recommended next prompt.

## Outcome summary

The inspection found a real documentation problem before finding any new
code problem: this project had accumulated six overlapping "readiness"
documents across three prior sessions, two of which contained **actively
false** claims (`docs/RISK_REGISTER.md`'s R-001 title and
`docs/PRODUCTION_CHECKLIST.md`'s "neither the UI nor the API has real
auth" line — both predate the authentication/RBAC/tenancy system that now
exists), and two more that were simply stale snapshots (the 2026-09-01
69/100 score, and the 2026-09-18 security verdict, which predates Prompts
19-24 entirely).

The new `docs/PRODUCTION_READINESS_RELEASE_GATE.md` is now the single
current, authoritative source — reusing, not re-deriving, every still-
valid prior finding, re-verifying what changed since (multi-tenancy,
scale hardening, this project's own Prompt 21 security audit,
observability, full-pipeline integration testing), and adding the areas
this prompt named that no prior document covered at all (analytics
correctness, recommendation evidence, onboarding, semantic catalog, AI
evaluation currency, tenant isolation as its own row).

Two P1 findings were concretely fixable in this sandboxed environment and
closed: `docs/DEPLOYMENT.md` gained real **Backup & Recovery** and
**Rollback** sections (previously absent entirely). The three P0 findings
(DAST never run, OIDC never verified against a live IdP, malware scanning
never verified against a real daemon) remain open — identical to every
prior session's own disclosure, since this environment still has no
staging deployment, live IdP, or ClamAV daemon. A fourth, newly-surfaced
P1 (the AI-accuracy benchmark numbers are now 13 prompts stale) was
documented, not closed — re-measuring it needs a live Ollama instance and
database, also unavailable here.

**Verdict: CONDITIONALLY READY** — a real change from the prior NOT READY
framing, not because any of the three environment-blocked items closed,
but because this pass confirmed, fresh, that everything built since
September still holds up under direct re-inspection, and closed every
gap that was actually fixable without live infrastructure.

See `docs/PRODUCTION_READINESS_RELEASE_GATE.md` for the full gate matrix,
P0-P3 findings, release checklist, and post-release monitoring plan, and
`CLAUDE.md`'s own section for the condensed summary.
