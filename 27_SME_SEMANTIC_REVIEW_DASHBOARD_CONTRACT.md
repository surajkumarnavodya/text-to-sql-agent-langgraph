# PROMPT 27 — SME Semantic Review Dashboard

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Build the SME dashboard that converts AI-inferred semantics into confirmed business truth.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect semantic APIs, metric APIs, relationship APIs, PII review APIs, existing frontend components and authorization.

### Implementation requirements
Display tables, columns, business names, descriptions, metrics, dimensions, relationships, PII classifications, confidence, evidence, status, owner and history. Prioritize low-confidence, conflicting, high-impact and sensitive items. Actions: Confirm, Reject, Edit, Request Clarification, Defer. For metrics such as Revenue show source, proposed expression, evidence, confidence, affected questions, version and reviewer. All state changes must use backend APIs, enforce authorization, audit and versioning.

### Testing requirements
Test role access, state transitions, concurrent review, tenant isolation and audit.

### Acceptance criteria
An SME can review and approve/reject semantic meaning without touching database code, and approval changes real semantic state.

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

Inspection found two real, genuinely unbridged backend review systems --
`onboarding/` review items (job-scoped PII/relationship/semantic-label/
golden-question, already reviewable via Prompt 26's `DatabaseOnboarding`
page) and the governed semantic catalog (`semantic/catalog.py`,
`api/semantic_catalog.py`, Prompt 09/10 -- entities/metrics/dimensions/
domains, with **zero frontend before this prompt**). Nothing in
`onboarding/` ever creates a `SemanticCatalogEntry` row. The new
`frontend/src/pages/SemanticReview.tsx` is the catalog's own review
surface -- the actual subject of the prompt's own "Revenue metric"
example -- and deliberately links into, rather than duplicates,
`DatabaseOnboarding`'s existing onboarding-item review table.

The prompt's five actions (Confirm, Reject, Edit, Request Clarification,
Defer) don't map one-to-one onto the catalog's three real transitions
(draft→reviewed, reviewed→draft, reviewed→published); there is **no
"reject" status at all**. Mapped honestly, disclosed in the page's own
docstring: Confirm is context-sensitive (Approve or Publish), Reject has
no equivalent (closest is Request Changes on a *reviewed* entry only),
Edit is `PATCH` on a draft only, Request Clarification is the real
`request-changes` route, and Defer makes no API call at all.

Two real backend gaps were found and closed, both additive: `reviewed_by_
display_name`/`published_by_display_name` (the ORM row always recorded
who reviewed/published an entry; the response never surfaced it), and an
opt-in `include_conflicts` param on the list route (conflicts were only
ever computed on create/publish, never on a plain list the dashboard
needs to prioritize). A real, pre-existing concurrency bug was found and
fixed while writing this prompt's own required "concurrent review" test:
every status-transition function validated against the caller's
in-memory `entry.status` and then committed an *unconditioned* `UPDATE`
-- two concurrent callers could both pass that check and both "succeed,"
losing the "only one should win" guarantee a 409 is supposed to provide.
Fixed via one atomic conditional `UPDATE ... WHERE status = expected`
(`identity.repositories.semantic_catalog._apply_transition`), portable
across every SQL dialect this app supports, no advisory lock needed.
Structured audit logging (`security.audit_log.log_security_event`) was
also added to every catalog state change and to the onboarding
review-item decide route, closing the one remaining literal requirement
("all state changes must... audit") that wasn't already true.

See `CLAUDE.md`'s "SME Semantic Review Dashboard" section for the full
design and every disclosed scope decision, and the session's final
report for the complete INSPECT → REPORT accounting.
