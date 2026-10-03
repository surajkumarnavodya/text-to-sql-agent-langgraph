# Prompt 32 — Complete UX, RBAC & Role-Based Navigation Integration

Mandatory first step: `00_MASTER_IMPLEMENTATION_CONTRACT.md` (read and applied).

## Objective

Make the finished product one coherent application: each signed-in caller sees
exactly the screens and actions their permissions allow, enforced on the server
and not just hidden in the browser, and the chain from client onboarding to
semantic approval to recommendation work holds together.

## Areas inspected

- **Frontend routes and navigation.** `App.tsx` (routes, unguarded by design for `/auth/callback`, `/shared/:ref`, `/accept-invitation/:token`), `AppShell.tsx` (header tabs gated by three role-name booleans), `AuthGate.tsx` (OIDC, local, and no-auth sessions), the review and admin pages (each re-deriving its buttons from role names), `vite.config.ts` (dev proxy and PWA caching).
- **Backend authorization.** Two systems: `agent/authz.py` (base roles `viewer`/`user`/`analyst`/`admin`, gating ASK, documents, SQL, media) and `identity/rbac.py` (DB-backed grants, gating onboarding, catalog, recommendations, admin dashboards). Also `api/authz.py`, `api/identity_authz.py` (`require_local_user`, `require_identity_permission`), and `api/auth.py` (the identity each auth mode produces).
- **Tenant context.** `security/tenancy.py` (`resolve_actor_tenant_id`, `resolve_tenant_context`), enforced on every identity route through `require_local_user`.
- **Onboarding, SME, admin and recommendation APIs.** `onboarding/jobs.py` (`publish_job`), `onboarding/semantic_contract.py`, `identity/repositories/semantic_catalog.py`, `api/onboarding.py`, `api/semantic_catalog.py`, `api/platform_admin.py`, `api/tenant_admin.py`, `api/recommendation_governance.py`.
- **Existing test suites** for each of the above, and the dev-proxy and production SPA-serving behaviour.

## Gaps found by inspection

1. **Navigation was decided in the browser from role names.** Hidden tabs were the only protection. Every screen URL was open to any signed-in user; an unauthorized user typing `/platform-admin` got a page of failed requests.
2. **Five pages re-derived their actions from role names** (`SemanticReview`, `DatabaseOnboarding`, `Recommendations`, `TenantAdmin`, `PlatformAdmin`), so the browser held a second copy of the authorization policy.
3. **Onboarding and SME review never met.** `publish_job` built a semantic-contract artifact and created no catalog entries. The SME dashboard never saw onboarding output.
4. **A real routing bug from Prompt 31.** The recommendation page was at `/recommendations`, which is also the governance API's JSON list route. In production, a hard refresh of that page reached the API (`api/main.py` registers API routes before the SPA fallback). Also: `/recommendations` was missing from the dev proxy list (the recurring gap Prompts 26–29 each found), and the dev proxy sent browser reloads of page paths that share an API prefix (`/platform-admin`, `/tenant-admin`) to the backend, which answered 404 JSON.
5. **A job with no connection details** (no host or database name) made discover and publish return a generic 500, because `ConfigurationError` escaped the route.
6. **A free-text onboarding label broke publish.** The label became the catalog `database_id`, which names a Chroma collection. Chroma rejects spaces, so publish failed for `My Warehouse`.
7. **`platform_admin` is not a base role.** It carries no `agent.authz` grants, so an operator holding only that role had no AI workspace, and the chat tab was shown anyway. This is the existing, deliberate tradeoff; navigation now shows it honestly.

## Persona mapping (existing roles only)

No new role was added. Navigation follows permissions, so personas map onto the seeded roles:

| Persona | Role(s) | Screens |
|---|---|---|
| Platform Super Admin | `platform_admin` **and** `admin` | All eight |
| Tenant Admin | `admin` | All except Platform Admin |
| SME / Semantic Reviewer | `analyst` | Chat, knowledge, media, recommendations, onboarding, SME review |
| Business Analyst | `analyst` | Same as SME. The existing model cannot separate them without a new seeded role |
| Business User | `user` | Chat, knowledge, media |
| Read-only Viewer | `auditor` | Tenant dashboard only (no AI grants, so cannot ask questions) |

`platform_admin` alone shows the admin screens but no AI workspace, by design.

## Design

**Server-side policy, one source of truth** — `security/navigation.py` (pure, no FastAPI or DB):
- `NAV_ITEMS`: eight screens. Each lists the `agent.authz` or `identity.rbac` permissions that allow it. Visibility is deny-by-default.
- `CAPABILITY_RULES`: twelve named actions (`ask`, `execute_sql`, `manage_onboarding`, `review_catalog`, `view_tenant_dashboard`, ...), each granted by exactly one permission.
- `resolve_navigation()` returns both lists from the same two permission sets, so they cannot disagree.

**Endpoints** — `api/navigation.py`:
- `GET /navigation` returns `items`, `capabilities`, `roles`, `tenant_id`. A local account's identity permissions come from the database, through `require_local_user`. That means a suspended tenant or a deleted account loses navigation on its next request. Any other caller gets only the permissions its roles carry in `agent.authz`, which are the only ones the identity routes would let it reach.
- `POST /navigation/access-denied` audit-logs (`event=ui_route_denied`) a refused screen, but only for a registered screen path. Anything else is answered identically and not logged, so the endpoint cannot write free text into the audit log. Rate-limited, and always 204.

**Frontend**:
- `useNavigation()` (React Query, keyed by the local user id or a session key, `staleTime: 0`) and `useCapabilities()`.
- `AppShell` draws its tabs from the server's `items`. Nothing is shown while navigation loads. The role-name booleans are gone.
- `RequireNavItem` guards each screen route. States: loading (explicit text), error (alert and retry), denied (forbidden page, once-per-visit audit report, focus moved to the heading), allowed (renders the screen).
- The forbidden page links only to a screen the caller *can* open, or to nothing.
- The five pages read `capabilities` instead of role names.

**Onboarding → SME bridge** — `onboarding/catalog_bridge.py`, called from `publish_job` before the job is marked published:
- One DRAFT `entity` per table that has at least one SME-confirmed semantic label or relationship. Pending and rejected items are ignored.
- The database id is a slug of the job's label plus the first eight characters of the job id. The slug is a valid Chroma name, and two labels that differ only in punctuation cannot collide.
- Idempotent: a table with a live (non-superseded) entity is skipped on a retried publish.
- Master rule 10 holds. The bridge creates drafts only. Publishing a draft still needs a catalog reviewer's approval and a separate admin publish. A static guard is not used; the journey test proves drafts only.

**Failure handling** — `api/onboarding.py::_engine_for_job`: a missing-connection configuration error answers **422** with a message that names no settings, and the job stays pending.

**Dev proxy and PWA** — `vite.config.ts`: `/recommendations` and `/navigation` added to the proxy and PWA list. The proxy's `bypass` sends any `Accept: text/html` request to the app, so a reload of a page path shared with an API prefix works in development.

## API / config / database changes

- **New:** `GET /navigation`, `POST /navigation/access-denied`.
- **Changed (behaviour):** `POST /onboarding/jobs/{id}/discover` and `.../publish` answer 422, not 500, for a job without connection details.
- **Changed (side effect):** `POST /onboarding/jobs/{id}/publish` now also creates DRAFT catalog entities (see the bridge above). Existing response shapes are unchanged.
- **Config:** none. The access-denied report uses the existing `API_ACTION_RATE_LIMIT_PER_MINUTE`.
- **Database:** none. No migration. The bridge uses the existing `semantic_catalog_entries` table.
- **Frontend route change:** the recommendation page moved from `/recommendations` to `/recommended-actions`. The old path was never a working page (see gap 4). Bookmarks never worked, and nothing else links to the old path.

## Tests added

**Backend — 63 tests:**
- `tests/test_security_navigation.py` (28): each persona's screens and capabilities, deny-by-default, platform-admin-alone, screen and capability registry invariants.
- `tests/test_api_navigation.py` (22): the endpoint for every persona with real local accounts and real tenants. **Every visible screen's representative API call is not refused (403), and every hidden one is refused**, checked for all five personas. Direct unauthorized API access, suspended-tenant denial, the access-denied report (audited for real paths, not logged for arbitrary ones, rejects unknown fields, requires auth).
- `tests/test_onboarding_catalog_bridge.py` (9): only confirmed items bridged, pending-only tables skipped, drafts only, evidence traces to the job, mean confidence, idempotent, superseded entries do not block, the slug is a valid Chroma name and never collides, empty labels still get an id.
- `tests/test_journey_onboarding_to_recommendation.py` (4): the **acceptance journey** over HTTP: onboarding job → discover → SME confirms every item → publish → drafts appear for the SME → SME approves → admin publishes → the SME accepts a recommendation, takes ownership, leaves a note, and the audit trail records each step → navigation matches. Also: the tenant boundary holds at every handoff, a business user is refused every review surface, and a job missing its connection details is a 422 that leaves the job pending (regression for gap 5).

**Frontend — 57 new tests, plus 6 files updated:**
- `components/auth/RequireNavItem.test.tsx` (8): loading state, error and retry, allowed render, denied forbidden page, audit report once per visit, focus on the forbidden heading, safe link target, no dead-end link.
- `App.routes.test.tsx` (49): **the role × URL matrix**: five personas plus signed-out, each opening each of the eight guarded URLs through the real `App`, shell and routes. A screen renders only when the server lists it; otherwise the forbidden page appears and the refusal is reported. Plus the header's links match the server's list.
- Updated: `AppShell.test.tsx` (gating tests now wait for the navigation response; a negative assertion that passes before data arrives proves nothing), and the page tests for `SemanticReview`, `DatabaseOnboarding`, `Recommendations`, `TenantAdmin`, `PlatformAdmin`, which now serve their user's navigation.
- `test/navigationFixtures.ts`: `navigationFor(roles)` mirrors the server's persona grants for component tests. The server suites pin the real mapping.

## Tests executed and results

- **Backend, full suite (`pytest`, including `tests/security`): 3,652 passed, 0 failed.** Run after all changes except a final ruff and black pass; the touched suites were rerun after that pass (103 passed).
- **Frontend, full suite (`vitest run`): 479 passed across 53 files, 0 failed.** Type check (`tsc --noEmit`): clean.
- **Lint:** `ruff check` clean on every touched Python file; `black --check` clean.

## Accessibility and responsive checks

- The primary navigation has an accessible name (`Primary navigation`), and every tab has an accessible name even when its label is visually hidden below `sm`.
- The forbidden page has a real `h1`, takes focus on arrival so the change is announced, and its link names a screen the caller can use.
- Loading and error states use `role="status"` and `role="alert"`.
- Each route's content keeps the existing `main` landmark.
- **Not checked:** colour contrast, keyboard traversal across the whole app, and layout at each breakpoint. No visual or browser test tool is part of this project.

## Security review

- **Navigation is no longer the access decision in the browser.** The server decides; the frontend only draws what it was told. Hidden tabs and guarded routes are presentation; every API route still enforces its own permission.
- **Unauthorized URLs are refused in two places:** the guard (forbidden page, audit report) and the API (403 with its own audit line).
- **The audit endpoint cannot be used to write attacker text into the log.** Only registered screen paths are logged, and the response is the same for every path, so it does not reveal which paths exist.
- **OIDC, static-token, and auth-off callers get no identity-gated screens** (server-enforced, tested). Their navigation matches what the identity routes would accept.
- **Suspended tenant:** navigation returns 401 on the very next request (tested).
- **Logs:** the denial line carries the screen id, path, subject and roles. The refused owner-assignment line from Prompt 31 is unchanged.

## Tenant-isolation review

- The tenant id in `/navigation` is resolved server-side from the caller's own account. The frontend sends none. Two-tenant navigation tests confirm each tenant sees its own id.
- The onboarding→catalog bridge files every draft under the **job's own tenant**. Its existing-entity check is tenant-scoped.
- The journey test proves cross-tenant refusal at each handoff: onboarding job, catalog entry, recommendation (all 404 to the other tenant).
- Navigation is cached per user key, so one person's screens cannot show for another on the same browser.

## Performance review

- **One extra request per screen navigation.** `staleTime: 0` refetches the navigation response on each guarded-route mount, so a changed role or suspension takes effect at once. React Query merges the concurrent calls from the shell, the guard and the page within one render. The trade is a small, fixed request per route change against a cache that could show stale access. A longer stale window plus an explicit invalidation on sign-out would cut the requests; it was not done here because access freshness was judged more important.
- **Server cost:** a local account's `GET /navigation` runs one permission query and one tenant lookup. No new table, index or background work.
- **Bridge cost:** one list query and one insert per new table, inside the existing publish transaction.

## Known limitations

- **"Confirm and run" is not capability-gated.** A viewer or auditor who can ask a question sees the button; the server refuses it. The button needs `capabilities.execute_sql`, which means touching the chat turn component and its many tests. This is the one case where a button is shown that the server will refuse.
- **No real Business Analyst versus SME separation.** Both are `analyst`. A dedicated seeded role would separate them, and it is the natural next step.
- **No browser end-to-end.** The project has no browser test tool. The journey is proven at the HTTP API layer, and the role matrix at the routed-component layer.
- **The analytics leg is not in the journey.** Analytics comes from `/ask`, which needs a live LLM. `tests/test_full_pipeline_integration.py` covers that path with the LLM stubbed.
- **No standalone "Dashboards" or "Insights" screen exists.** Analytics and insights appear inside each answered question. Inventing a dashboard screen would have been a new feature, so the navigation lists only real screens.
- **Owner-only history does not name owners** (carried over from Prompt 31).
- **Onboarding labels are slugged.** Two jobs whose labels differ only in case, punctuation or spacing still get separate catalog namespaces, because the job id suffix keeps them distinct. An operator looking for "My Warehouse" in the catalog sees the slug.

## Remaining risks

- **Stale navigation during a long session.** Access changes take effect on the next screen change, not in real time. A suspended tenant's screen-level API calls are refused at once, but a screen already open keeps its layout until the next navigation.
- **The one-screen-per-route guard depends on the registries staying in step.** `security/navigation.py`'s `NAV_ITEMS` and `frontend/src/App.tsx`'s `RequireNavItem` ids must match. The route matrix test catches a mismatch for every guarded URL; a new screen that skips the guard would not be caught by it.
- **The Prompt 30 analytics test is still timing-sensitive** under full-suite load (its lazy-loaded panel can exceed testing-library's default `findByRole` timeout). It passed in every run here but was not changed in this prompt.
- **Owner-assignment and access-denied rate limits are per IP**, not per user (carried over).

## Recommended next prompt

Prompt 33 should add the two remaining pieces of this prompt: a capability gate for "Confirm and run" in the chat turn, and a seeded `sme` role that separates the SME reviewer from the business analyst, with its own navigation test. It should also add a browser end-to-end suite (Playwright or similar) that runs the journey and the role matrix against a live server.
