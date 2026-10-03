# PROMPT 28 — Global Platform Admin Dashboard

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Build the platform-wide administration dashboard.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect existing admin routes/components, telemetry APIs, tenant APIs, user/RBAC APIs and audit services.

### Implementation requirements
Provide sections for tenants, databases, users, roles, permissions, semantic review queues, models/providers, health, usage, latency, errors, security events, audit logs, jobs and configuration status. Use real telemetry. Provide drill-down and filtering. Enforce platform-admin authorization. Never expose credentials.

### Testing requirements
Test platform-admin versus tenant-admin access and dashboard data authorization.

### Acceptance criteria
Platform administrators can operate the platform using real data without direct database access.

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

Inspection confirmed the central gap this prompt names explicitly in its
own acceptance criterion: this codebase had **no platform-admin-versus-
tenant-admin distinction** anywhere (`observability/metrics.py`'s own
Prompt-20 docstring already flagged this as a known limitation). The
tenant-scoped `admin` role (`agent/authz.py`) is correctly *tenant*-admin
by design; nothing granted cross-tenant visibility. A new
`identity.rbac.Permission.PLATFORM_ADMIN` + `platform_admin` role (a
genuine superset of `admin`, plus that one extra permission) closes this,
enforced by a new `api.identity_authz.require_platform_admin` dependency
on every route in the new `api/platform_admin.py` router.

Two further real, found gaps were closed along the way: `identity.models
.AuditLog` (the `audit_logs` table, with `Permission.AUDIT_READ_ANY`
seeded since this project's identity schema was first built) had never
been written to or read from by anything — a dead table with live
permissions pointed at nothing. This prompt's own new mutating actions
(tenant status changes, role assignment) are its first real writers.
"Security Events" had no queryable backing at all -- `security.audit_log
.log_security_event`'s ~40 existing call sites only ever wrote to a
Python logger. A bounded, in-memory ring buffer now hooks into that one
function itself, so every existing call site started feeding it with
zero changes to any of them.

Heavy reuse, not reinvention: `identity/repositories/tenants.py`'s full
CRUD (list/create/suspend/soft-delete) existed since Prompt 20 and was
never exposed by any route until now; `observability.metrics
.get_default_metrics().snapshot(tenant_id=None)` already supported the
merged, cross-tenant view, "deliberately not reachable through the HTTP
route" per that module's own docstring -- until this one. `GET /health`
and `GET /models` are deliberately **not** duplicated -- both were
already public/unscoped, so the dashboard's frontend calls them directly
via the existing `useHealth`/`useAvailableModels` hooks.

While writing the prompt's own required "concurrent review" coverage for
an unrelated, adjacent fix in the same session, a real, pre-existing
concurrency bug in `identity/repositories/semantic_catalog.py` (Prompt
09/27's own status-transition functions) was found and fixed, and its
flaky real-threading test was replaced with a deterministic two-session
test -- see `CLAUDE.md`'s own "SME Semantic Review Dashboard" section for
the full writeup; that fix landed in this same pass since it surfaced
while stabilizing the full suite for this prompt's own new tests.

See `CLAUDE.md`'s "Global Platform Admin Dashboard" section for the full
design and every disclosed scope decision, and the session's final
report for the complete INSPECT → REPORT accounting.
