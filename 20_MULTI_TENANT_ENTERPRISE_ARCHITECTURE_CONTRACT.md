# PROMPT 20 — Multi-Tenant Enterprise Architecture

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Make tenant isolation a first-class platform boundary.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect tenant models, authentication, authorization, cache keys, jobs, persistence and APIs.

### Implementation requirements
Isolate database connections, metadata, semantic catalogs, metrics, permissions, PII policies, golden questions, feedback, audit, caches and background jobs. Propagate tenant context through API, LangGraph, retrieval, database adapter, analytics and persistence. Never trust unverified client tenant IDs. Ensure shared model infrastructure cannot leak tenant context.

### Testing requirements
Add cross-tenant negative tests for APIs, caches, jobs, metadata, analytics and recommendations.

### Acceptance criteria
No authenticated tenant can access another tenant's data or configuration.

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
