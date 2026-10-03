# PROMPT 29 — Client/Tenant Admin Dashboard

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Build the tenant-specific administration portal.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect tenant APIs, database management APIs, user/RBAC APIs, semantic status APIs and audit services.

### Implementation requirements
Show tenant profile, databases, health, users, roles, permissions, semantic catalog status, metrics, pending reviews, golden questions, evaluation, AI usage, analytics usage, recommendations and tenant audit. Allow authorized tenant admins to manage users, roles, onboarding status and discovery refresh. Do not expose platform-wide configuration or other tenants.

### Testing requirements
Test tenant isolation and role-specific actions.

### Acceptance criteria
Tenant admins can manage their own environment and cannot access platform-wide or other-tenant data.

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

Unlike Prompt 28's platform-wide dashboard, this prompt introduces **no
new RBAC permission or role** — a tenant admin is exactly what the
existing, tenant-scoped `admin` role already means. Every route in the
new `api/tenant_admin.py` resolves `tenant_id` exactly once, from
`security.tenancy.resolve_actor_tenant_id(user)`, and no route signature
in the file accepts one as a parameter at all — the structural guarantee
behind "cannot access platform-wide or other-tenant data," proven
directly (`tests/test_api_tenant_admin.py::TestTenantIsolation
::test_no_route_accepts_a_tenant_id_override`).

Reuse was even heavier than Prompt 28's: `identity.repositories.tenants
.get_tenant`, `identity.repositories.users.list_users`/
`list_roles_with_permissions`/`assign_role`/`remove_role`,
`identity.repositories.semantic_catalog.list_entries`,
`identity.repositories.onboarding.list_jobs_for_tenant`/
`list_review_items`/`list_artifacts`, and `Settings.databases_for_tenant`
all already existed and were already tenant-aware. `GET
/metrics/performance` and `GET /recommendations`(`/metrics`) were
confirmed, by reading them, to already resolve and scope to the caller's
own tenant — so this dashboard's AI-usage/analytics/recommendations
sections call those existing routes directly rather than duplicating
them under `/tenant-admin/*`, closing the real, disclosed gap that
neither route had ever had a frontend consumer before this prompt.

One real, newly-relevant tenant-isolation gap was found and closed:
`POST /schema/refresh` (`api/main.py`) refreshes *every* configured
database with no tenant filter at all — correct for its existing,
unrelated platform-operator use, but wrong to hand a tenant-admin
dashboard as-is. Rather than modify that existing route, a new, narrowly
tenant-scoped `POST /tenant-admin/databases/refresh` was added, looping
only `Settings.databases_for_tenant(caller_tenant_id)` and reusing the
exact same `embeddings.schema_indexer.refresh_schema_index` function per
database — proven directly
(`tests/test_api_tenant_admin.py::TestSchemaRefreshIsTenantScoped`).

The one restriction this dashboard adds on top of reused permissions: a
tenant admin can never assign or see the `platform_admin` role (excluded
from both the roles list and the assignable-role check) — a cross-tenant
role has no legitimate place in a tenant-scoped surface.

See `CLAUDE.md`'s "Client/Tenant Admin Dashboard" section for the full
design and every disclosed scope decision, and the session's final
report for the complete INSPECT → REPORT accounting.
