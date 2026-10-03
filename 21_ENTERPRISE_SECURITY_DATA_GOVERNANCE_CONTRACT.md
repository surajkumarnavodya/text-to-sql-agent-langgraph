# PROMPT 21 — Enterprise Security & Data Governance

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Perform a comprehensive deterministic security hardening pass.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect authentication, RBAC/ABAC, SQL validation, tenant isolation, secrets, CORS, headers, audit, rate limiting, prompt injection defenses, uploads and dependencies.

### Implementation requirements
Enforce identity→tenant authorization→resource authorization→SQL safety→object/column authorization→database permissions→cost/runtime/result limits→audit. Treat database/document content as untrusted prompt input. Add SAST/DAST-ready checks and regression tests. Preserve existing controls.

### Testing requirements
Run security regression tests including prompt injection, unauthorized access, SQL attacks, tenant escape and sensitive-field access.

### Acceptance criteria
No model-generated instruction can bypass deterministic security.

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

Unlike most prompts in this series, this one found the codebase's existing
deterministic security architecture already strong — RBAC, SQL AST
validation, tenant isolation (Prompt 20), CORS, and dependency-CVE
reachability were all re-verified intact and unchanged. Three real, scoped
gaps were found and closed:

1. SSRF rejections in `media_gen/download.py` were not audit-logged (a
   previously-disclosed gap, `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`'s
   P1-6, confirmed still open).
2. `POST /feedback/golden-example`/`POST /feedback/message` (`api/main.py`)
   had no rate limit, inconsistent with every other mutating route.
3. `POST /onboarding/jobs`/`.../discover`/`.../publish` (`api/onboarding.py`)
   had no rate limit despite each opening a live outbound connection to a
   caller-supplied host/port.

See `CLAUDE.md`'s "Enterprise security & data governance hardening"
section and `docs/security-changelog.md`'s 2026-10-02 entry for the full
design and findings, and the session's final report for the complete
INSPECT → REPORT writeup.
