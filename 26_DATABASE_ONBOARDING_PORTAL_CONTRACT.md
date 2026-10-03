# PROMPT 26 — Database Onboarding Portal

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Build the real frontend workflow for onboarding a client database.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect frontend framework, design system, routing, authentication, API client, job/progress patterns and onboarding APIs from Prompt 08.

### Implementation requirements
Create a guided workflow: create/select tenant→select provider→secure connection/secret reference→test connection→start discovery→show progress→show schemas/tables/views/relationships→show profiling→show PII candidates→start semantic analysis→route ambiguous items to SME review→generate golden questions→run evaluation→publish. Use real backend APIs/jobs. Provide validation, retries, progress, errors, cancellation where supported and audit. Never display persisted passwords.

### Testing requirements
Test UI/API integration, authorization, retries, failure states, tenant isolation and responsive behavior.

### Acceptance criteria
An authorized admin can onboard a real SQL Server database through the UI without mock final data.

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

Inspection of Prompt 08's backend (`onboarding/`, `api/onboarding.py`)
found the prompt's own 14-step wizard outline does not map one-to-one
onto 10 real routes: "test connection" and "create job" are one call,
"start semantic analysis"/"generate golden questions" already happen
inside discovery, "run evaluation" already happens inside publish, there
is no "create/select tenant" step at all (tenant is server-resolved),
and discovery/publish are synchronous calls with no background worker,
so there is no incremental progress signal to poll. The resulting UI
(`frontend/src/pages/DatabaseOnboarding.tsx`) is built to match this
real shape exactly, with every one of these differences disclosed in
the page's own docstring and in `CLAUDE.md`, rather than fabricating a
button, a tenant selector, or a fake progress bar for a step the backend
doesn't actually have.

A real, necessary backend-adjacent fix was found during inspection:
`vite.config.ts`'s dev-server proxy list was missing `/onboarding`
entirely, which would have made every new API call fail in `npm run
dev` despite working in production. A real regression was found and
fixed during implementation, not merely avoided: an early version of the
new role-gated nav-tab selector returned a freshly-allocated array on
every Zustand store read, breaking React's snapshot-equality check and
crashing the app shell outright — caught by this project's own
pre-existing `AppShell.test.tsx` suite.

Permission gating is UX-only throughout, matching this codebase's
already-stated principle (`AuthGate.tsx`'s own docstring) — every real
enforcement point stays server-side, unchanged.

See `CLAUDE.md`'s "Database Onboarding Portal" section for the full
design and every disclosed scope decision, and the session's final
report for the complete INSPECT → REPORT accounting.
