# Multi-tenancy

Prompt 20 (`20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`) made
tenant isolation a first-class platform boundary. This document is the
decision record for what that means concretely, what it does *not* mean,
and which deployment shapes it genuinely protects.

> **If you run a single-tenant deployment, nothing in this document asks
> you to change anything.** Every setting introduced here defaults to the
> behavior this application had before it existed, and every existing row
> lands in the `default` tenant — which is exactly the value every tenant
> comparison in the codebase already resolved to. The upgrade changes no
> authorization outcome for anybody.

---

## 1. What changed, and why it mattered

Before Prompt 20, `tenant_id` already appeared on five tables
(`conversation_shares`, `onboarding_jobs`, `semantic_catalog_entries`,
`recommendation_records`, `recommendation_feedback_events`) and four
deny-by-default ABAC policy modules genuinely compared it:

| Module | Guards |
|---|---|
| `identity/share_policy.py` | Conversation sharing |
| `onboarding/policy.py` | Client-database onboarding jobs |
| `semantic/catalog_policy.py` | Semantic catalog entries |
| `recommendation/governance_policy.py` | Recommendation feedback lifecycle |

All four were real, exercised code paths — but
`security.tenancy.resolve_actor_tenant_id` returned **one hardcoded
constant for every user**, so no two real callers could ever be in
different tenants and every one of those comparisons was vacuous in
practice. The honest summary of the pre-Prompt-20 state is: the *shape* of
tenant isolation existed; the *fact* of it did not.

Prompt 20 supplied the missing fact. `identity.models.Tenant` is a real
table, `users.tenant_id` is a real `RESTRICT` foreign key onto it, and an
actor's tenant is now read from their own account row. **No call site of
either resolver function had to change for those four policy modules to
become load-bearing** — which was the entire reason resolution was routed
through one module in the first place.

---

## 2. The three rules

These live in `security/tenancy.py`'s own module docstring and are
restated here because every future change to tenancy has to keep them
true.

### 2.1 A tenant id is never read from anything a client sends

Not a request body, not a query parameter, not a header. A tenant comes
from exactly one of:

- **`identity.models.User.tenant_id`** — a row this server loaded from its
  own database (`resolve_actor_tenant_id`).
- **This app's own JWT `tid` claim** — minted server-side by
  `identity.security.create_access_token` from the signing user's own row,
  and read back only after a signature-verifying decode
  (`resolve_tenant_id_for_identity`, `mode="local"`).
- **An IdP-signed OIDC claim**, named by `OIDC_TENANT_CLAIM` — read by
  `security.oidc.extract_tenant_id` only after the token's signature has
  been verified against the issuer's live JWKS. This is the IdP's
  assertion about the caller, not the client's.

`security.tenancy.reject_client_tenant_override` is the belt-and-braces
check: a request carrying `X-Tenant-Id`, `X-Tenant`, `Tenant-Id` or
`X-Tenant-Override` is **refused with a 400**, at `api/auth.py`'s single
authentication chokepoint, before anything is authenticated — including in
`AUTH_MODE=none` development deployments.

Refusing rather than ignoring is deliberate. Ignoring is safe *today*
(nothing reads such a header), but it leaves no signal if a future
reverse-proxy configuration, SDK or well-meaning change starts forwarding
one — at which point "we ignore it" would be an assumption nobody
re-verified.

### 2.2 Unknown means deny, never "default"

`security.tenancy.resolve_tenant_context` is the one gate that enforces
usability, and it fails closed on all four of:

- no resolvable tenant for this caller (`None`),
- a tenant id with no row,
- a soft-deleted tenant,
- a `status != "active"` tenant.

Having exactly one gate is the point: suspending a tenant via
`identity.repositories.tenants.set_tenant_status` takes effect for every
one of its users across every tenant-scoped route at once, rather than
needing each route to remember a status check of its own.

Every failure logs the real reason server-side at `WARNING` but raises the
same generic `TenantResolutionError.safe_message`. A caller must not be
able to distinguish "that tenant does not exist" from "that tenant is
suspended" from "you are not in that tenant" — the same anti-enumeration
reasoning that already maps every cross-tenant denial in this codebase to
the identical 404 a genuinely nonexistent resource gets.

### 2.3 Resolution is per request, never cached

A suspension must not wait for an access token to expire. `tid` is carried
in the token (a stable, server-asserted fact, like `roles`), but *status*
is read live from the `tenants` table on every call that resolves a
context. Login and token refresh both re-check it, which bounds a
suspension's effect to one access-token lifetime at worst rather than to
the much longer refresh-token lifetime.

---

## 3. What is isolated

| Resource | Mechanism |
|---|---|
| User accounts | `users.tenant_id`, `RESTRICT` FK onto `tenants.id` |
| Conversations / chat history | `conversations.tenant_id`, plus `identity/repositories/history.py::_tenant_matches_owner` folded into **every** read |
| Conversation shares | `conversation_shares.tenant_id` + `identity/share_policy.py` (pre-existing, now load-bearing) |
| Onboarding jobs | `onboarding_jobs.tenant_id` + `onboarding/policy.py` (pre-existing, now load-bearing) |
| Semantic catalog entries | `semantic_catalog_entries.tenant_id` + `semantic/catalog_policy.py` (pre-existing, now load-bearing) |
| Recommendations + feedback | `recommendation_records`/`recommendation_feedback_events`.`tenant_id` + `recommendation/governance_policy.py` (pre-existing, now load-bearing) |
| Database connections | `DB_<NAME>_TENANT_IDS` → `Settings.databases_for_tenant` |
| Schema metadata (`GET /schema/tables`) | Resolved through `databases_for_tenant`; cross-tenant name → 404 |
| SQL execution (`POST /execute`) | Resolved through `databases_for_tenant`; cross-tenant name → 404 |
| Agent database routing | `embeddings.retriever.select_database(..., tenant_id=...)` only ever considers that tenant's databases |
| `POST /execute` result cache | Cache key is `(tenant_id, database_name, sql)` |
| Golden examples (few-shot) | `tenant_id` in the document id *and* metadata; Chroma `where` filter plus a per-document re-check |
| Business-context retrieval | `retrieval.retriever._chunk_tenant_id` drops any chunk belonging to another tenant |
| Performance metrics | `observability.metrics.PerformanceMetrics` partitions per tenant; `GET /metrics/performance` returns only the caller's own slice |
| Recommendations (inputs) | `generate_recommendations_node` reads `PerformanceMetrics.snapshot(state["tenant_id"])`, so no recommendation is derived from another tenant's latency |
| Response feedback log | `tenant_id` recorded on every `feedback/store.py` row (a read-later log; nothing retrieves from it into a prompt) |
| Security audit events | `security.audit_log.set_audit_tenant_id`, bound by `run_agent`/`run_orchestrated`/`api/auth.py`; every event is stamped `tenant_id=` |
| Onboarding jobs / catalog / recommendation governance | The four pre-existing deny-by-default ABAC policy modules, now driven by a real `users.tenant_id` instead of a constant |

### 3.1 Database connections

A connection with no `DB_<NAME>_TENANT_IDS` is **shared by every tenant** —
deliberately, because that is what a single-tenant deployment's one
database is, and because a shared read-only reporting database
legitimately serves several tenants. Listing even one tenant restricts the
connection to that list.

```dotenv
DB_CONNECTIONS=sales,hr,reporting
DB_SALES_TENANT_IDS=acme
DB_HR_TENANT_IDS=acme,globex
# DB_REPORTING_TENANT_IDS unset -> shared by every tenant
```

A tenant asking for a connection it may not use gets the same
`404 Unknown database` a nonexistent name gets, and the agent's own
auto-routing never even scores it.

### 3.2 Why a shared database still needs per-tenant caches and retrieval

Two tenants legitimately sharing one database is the case that makes the
cache and retrieval partitions load-bearing rather than redundant:

- A **result cache** keyed only by `(database, sql)` would serve tenant A's
  rows to tenant B running byte-identical SQL. The cache sits *after* every
  authorization gate, so it cannot rely on one.
- A **golden example** is a human-approved question/SQL pair in one
  tenant's own business vocabulary. Serving it into another tenant's
  generation prompt leaks both their vocabulary and their query patterns.
- A **published semantic-catalog concept** is tenant-scoped at rest, but
  before Prompt 20 its vector-store chunk recorded the tenant only inside
  an opaque `source_id` string nothing parsed — so it was retrievable by
  any tenant sharing that database. It now carries `tenant_id` in
  filterable metadata.

Operator-authored content — live schema introspection and the
`data/knowledge/*.yaml` files — is deliberately **not** tenant-scoped: it
is deployment configuration shared by every tenant permitted to query that
database, not one tenant's private content. `_chunk_tenant_id` treats an
absent tenant as `default` for exactly this reason, which also keeps a
pre-Prompt-20 index working untouched.

### 3.3 Shared model infrastructure

Two things in this application are genuinely process-wide singletons by
design: `agent.llm_client._get_ollama_client`'s cached `ollama.Client` and
`agent.graph.build_graph`'s compiled LangGraph graph. Neither may carry
tenant context.

That holds because a tenant is a **plain value in the per-request
`AgentState` dict**, exactly like `selected_database` and `selected_model`
before it — never a field on the graph, never a mutation of the cached
`Settings` singleton. The one piece of ambient state Prompt 20 does
introduce, the audit-log tenant, is a `contextvars.ContextVar` bound and
reset in a `try`/`finally` around the graph invocation, so it cannot
outlive the run or leak into whatever reuses the worker thread next.

This is asserted under **real concurrency**, not sequentially:
`tests/security/test_cross_tenant_shared_infrastructure.py` runs two
`run_agent` calls on real threads with an artificial delay inside a node to
force the race window open, and checks each run saw only its own tenant.
That mirrors `tests/test_model_selection_concurrency.py`, which guards the
identical bug class for `selected_model` — a sequential test would pass even
with a global-mutation bug, because there would be no window for the second
call to clobber the first's in-flight value.

### 3.4 Why metrics are partitioned

`GET /metrics/performance` is admin-gated, but this codebase has **no
platform-admin-versus-tenant-admin distinction** — so admin-gating alone
would have let one tenant's admin read another's latency distributions,
status mixes and cache hit rates, all of which say something real about
another tenant's workload. Each write lands in its own tenant's window and
the route reads back only the caller's own.

The merged, process-wide rollup still exists (`PerformanceMetrics
.snapshot()` with no argument) for operator/profiling use. It is
deliberately **not** reachable over HTTP.

---

## 4. Database migration

`identity/migrations/versions/a4c7e2b9d6f5_multi_tenant_boundary.py`:

```powershell
alembic -c identity\alembic.ini upgrade head
```

Non-destructive by construction:

1. `tenants` is created and seeded with the single `default` row **before**
   either new column exists, so the two foreign keys are satisfiable at the
   moment they are created — there is no window in which an existing row
   violates them.
2. `users.tenant_id` and `conversations.tenant_id` are added `NOT NULL`
   with `server_default='default'`, which is precisely the value
   `resolve_actor_tenant_id` already returned for every user.

`identity.bootstrap.ensure_default_tenant` keeps a freshly
`Base.metadata.create_all`-built database (every test, and
`scripts/bootstrap_admin.py` against an empty database) consistent with a
migrated one. It is called from `seed_rbac`, deliberately, so no caller can
forget it — creating a user without that row present violates the
`RESTRICT` foreign key on PostgreSQL.

---

## 5. Known limitations

Named explicitly rather than left for a future session to rediscover.

1. **The five pre-existing `tenant_id` columns have no foreign key onto
   `tenants`.** Adding one would require every historical row's value to
   already exist in `tenants`, which is true today only because they all
   hold `"default"`; a constraint creation that fails mid-upgrade on a real
   deployment is a worse outcome than the modest integrity gain. They
   remain plain `String(64)`.

2. **`AUTH_MODE=none` and `AUTH_MODE=static_token` are not meaningful
   tenant boundaries.** Every caller under either mode is indistinguishable
   by design (see `security.oidc.real_caller_subject`), so there is no
   per-caller tenant to resolve and all of them land in `default`. Tenant
   isolation requires local accounts or OIDC.

3. **No self-service tenant assignment, and no admin UI for tenants.**
   All four `create_user` call sites leave `tenant_id` at its default, so a
   non-default tenant can only be assigned by an operator running code
   against the identity database. `identity/repositories/tenants.py`
   provides the CRUD; no route exposes it.

4. **No code path moves a user between tenants.** `_tenant_matches_owner`
   is fail-closed about this: if a conversation's tenant ever disagreed
   with its owner's, the conversation becomes invisible rather than
   leaking. That is the correct response to an unexpected state, but it
   does mean a hand-written tenant reassignment would orphan that user's
   history until their conversations were updated too.

5. **Tenant suspension has a one-access-token-lifetime lag for routes that
   do not resolve a context.** Login and refresh re-check status, and every
   route that calls `resolve_tenant_context` reads it live — but a route
   that only needs `resolve_tenant_id_for_identity` (e.g. `/ask`) trusts
   the `tid` claim for the token's remaining life. Shortening
   `ACCESS_TOKEN_EXPIRE_MINUTES` tightens this; making every route hit the
   identity database would not be worth the per-request cost.

6. **Attachments, rate limits and concurrency limits are keyed by caller
   identity, not by tenant.** `security.oidc.real_caller_subject` already
   distinguishes individual callers, and a caller belongs to exactly one
   tenant, so cross-tenant access is not possible through these — but a
   *noisy-neighbour* tenant can still consume a shared global budget
   (`MAX_CONCURRENT_ASK_REQUESTS`). Per-tenant quotas are a real,
   deliberately deferred follow-up.

7. **PII classification is not per-tenant.**
   `config/sensitive_columns.yaml` is global, keyed by bare table name — it
   is not even keyed by *database* today (a pre-existing limitation this
   codebase already documents). Two tenants sharing a configured database
   therefore share one restricted-column policy, and a tenant cannot
   classify a column its neighbours have not. Making this per-tenant means
   moving the policy out of a YAML file and into the identity database with
   its own review workflow — genuinely the shape of
   `semantic/catalog.py`, not a keying change — so it is scoped out rather
   than half-done.

8. **RBAC role and permission *definitions* are global.** Role
   *assignment* is per user and therefore effectively per tenant, which is
   what the four ABAC policy modules consume. But the `roles`/`permissions`
   tables are shared seed data: a tenant cannot define its own role, and an
   operator cannot grant `analyst` different permissions in one tenant than
   another. Per-tenant role customization is a separate feature, not a gap
   in this boundary.

9. **The audit tenant does not cross a raw `ThreadPoolExecutor`.**
   `contextvars` are copied by anyio's thread dispatch but not by a plain
   `concurrent.futures.ThreadPoolExecutor`, and `/ask` uses the latter.
   This is why the binding lives *inside* `run_agent`/`run_orchestrated`
   (the worker side of that boundary) rather than in the route handler — but
   it does mean an event emitted in the route handler itself, outside the
   graph, is tagged only when `api/auth.py`'s own binding is still in
   context. The same limitation already applies to correlation IDs and was
   inherited, not introduced here.

10. **Everything process-local stays process-local.** The result cache,
   metrics rollup, rate limiters and compiled-graph/Ollama-client
   singletons are all per-process, so a multi-replica deployment has one
   independent copy of each — the same limitation
   `docs/SCALE_BASELINE.md`'s "Honest capacity statement" already
   discloses, unchanged by this work.

11. **No live multi-tenant deployment has been exercised.** Every
   invariant above is covered by unit/HTTP-level tests against an
   in-memory SQLite identity database, and the migration was authored
   against the same chain Prompt 18 verified live against real PostgreSQL —
   but two genuinely separate tenants with two separate customer databases
   have not been run end to end in this environment.

---

## 6. Related documents

- [`docs/AUTHENTICATION.md`](AUTHENTICATION.md) — the four auth modes and
  where `tid` is minted.
- [`docs/AUTHORIZATION.md`](AUTHORIZATION.md) — the role → permission
  mapping tenancy layers on top of.
- [`docs/SHARING_SECURITY.md`](SHARING_SECURITY.md) — the first feature to
  carry a `tenant_id`, and the decision record for why it was scoped
  narrowly at the time.
- [`docs/SCALE_BASELINE.md`](SCALE_BASELINE.md) — what is and is not
  suitable for multi-instance deployment.
