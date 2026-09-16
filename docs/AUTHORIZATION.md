# Authorization (RBAC)

**Status: implemented, 2026 Phase 2.** Layered on top of `docs/AUTHENTICATION.md`'s identity layer. Authentication answers "who is this?"; this document covers "what may they do?" — enforced entirely server-side.

## Before this pass

None. Every authenticated caller (in practice, anyone at all, since `API_AUTH_TOKEN` was the only gate and often unset) had identical, unrestricted access to every route and every data source. `docs/RISK_REGISTER.md`'s R-008 and the 2026 Phase 1 security review's AGT-01 finding both named this explicitly: the multi-source orchestrator's LLM router alone decided whether a question reached the access-sensitive "policies" RAG collection or triggered paid media generation, with no independent check of whether the *caller* was actually permitted to.

## What Phase 2 added

A role-based access control layer: `agent/authz.py` (pure policy — role → permission mapping, no web-framework dependency) and `api/authz.py` (the FastAPI `Depends(...)` wiring built on top of it).

### Role model

Four default roles, in ascending privilege order — **extensible, not a closed set**: `agent.authz.ROLE_PERMISSIONS` is a plain dict; adding a role (or narrowing/widening an existing one) is a data change, not a code change at any call site.

| Role | Grants (cumulative) |
|---|---|
| `viewer` | Ask questions, list/download ordinary (non-sensitive) documents, search the media library, use voice mode |
| `user` | + Execute/confirm SQL, use web search, save golden-example feedback |
| `analyst` | + View restricted columns, download sensitivity-tagged documents, query the "policies" RAG collection, generate media |
| `admin` | + Upload/delete documents, refresh schema, administrative/config actions |

A role name `ROLE_PERMISSIONS` doesn't recognize (a typo, or a claim from an identity provider this deployment hasn't mapped) grants **no permissions** — fail closed, never fail open on an unrecognized or missing role.

### Where roles come from

- **OIDC mode**: the JWT's `OIDC_ROLE_CLAIM` claim (default `roles`; a single string or list of strings, both accepted — `security.oidc.extract_roles`).
- **Static-token / no-auth ("dev") modes**: a fixed `admin` role. Both are a single shared credential (or no credential at all) with no natural sub-identity to scope down further — granting `admin` here is a deliberate continuation of what a valid credential already implicitly granted before RBAC existed, not a new privilege (see `docs/AUTHENTICATION.md`).

### What's protected

| Resource | Permission | Enforced at |
|---|---|---|
| Asking a question (`POST /ask`) | `ASK` | Route |
| SQL execution (`POST /execute`) | `EXECUTE_SQL` | Route |
| Restricted columns (`config/sensitive_columns.yaml`) | `VIEW_RESTRICTED_COLUMNS` | `agent/nodes.py::validate_sql_node`, inside the graph |
| Schema listing/refresh | `ASK` / `SCHEMA_REFRESH` | Route |
| Document list/download | `DOCUMENTS_READ` | Route |
| Sensitivity-tagged document download | `DOCUMENTS_READ_SENSITIVE` | Route, checked against the specific document's `sensitivity_category` |
| Document upload | `DOCUMENTS_WRITE` | Route |
| Document delete | `DOCUMENTS_DELETE` | Route |
| "Policies" RAG collection | `POLICY_RAG_QUERY` | **`agent/orchestrator/nodes.py::router_node`**, before the subgraph runs |
| Web search | `WEB_SEARCH` | `router_node`, before the subgraph runs |
| Media generation (propose + confirm) | `MEDIA_GENERATE` | `router_node` + `POST /generate/confirm` + `GET /media/{id}` |
| Media library search/serving | `MEDIA_SEARCH` | `router_node` + `POST /search/media` + `GET /media/library/{id}` |
| Golden-example feedback write | `GOLDEN_EXAMPLE_WRITE` | Route |
| Voice transcribe/synthesize | `VOICE_USE` | Router-level |

**No frontend/UI-level restriction is treated as a security boundary anywhere in this codebase.** The React dashboard may hide a button a caller lacks permission for, but every corresponding API route (or, for the orchestrator, the corresponding internal node) re-checks independently — the same request made directly with curl and no elevated role gets the same 403.

### Closing AGT-01/R-008: authorization inside the orchestrator, not just at the API boundary

The most architecturally significant piece: `router_node` (`agent/orchestrator/nodes.py`) filters the LLM's own source selection through `_SOURCE_PERMISSIONS` **after** classification but **before** `route_after_router` ever fans out to a subgraph node. A denied source is dropped from that turn's route (falling back to `sql` alone, never an empty/unrunnable route) and logged (`orchestrator_source_denied`) — the LLM may still *request* a source, but it never gets to decide alone whether the request is *permitted*. This is the same principle the SQL pipeline's own validator already embodies ("never trust the LLM's output as authorization"), extended to the multi-source router's routing decision itself.

### Auditability

Every denial — a route-level 403 (`api/authz.py::require_permission`) or an orchestrator-level source drop (`router_node`) — is logged via `security.audit_log.log_security_event` (`authz_denied` / `orchestrator_source_denied`) with the caller's subject, roles, and the permission that was missing. Role/permission names aren't secrets, so (unlike authentication-failure logging) this detail is logged directly — it's exactly the record needed to investigate a privilege-escalation attempt after the fact.

## Testing

- `tests/test_nodes_security_wiring.py` — the restricted-column gate respects `VIEW_RESTRICTED_COLUMNS`.
- `tests/test_orchestrator.py::TestRouterNodeAuthorization` — the AGT-01 fix itself: a denied source is dropped before its subgraph runs; missing/invalid roles fail closed; the default role hierarchy actually differentiates; denial is audit-logged without leaking content.
- `tests/test_api_authz.py` — end-to-end through real HTTP routes with real signed JWTs:
  - **Vertical privilege escalation**: `viewer`→`/execute`, `user`→`/schema/refresh`, `analyst`→document delete, all 403.
  - **Horizontal privilege escalation**: two different subjects with the same role get identical (denied) treatment; a `sub` claim that merely *looks* administrative grants nothing.
  - **Missing roles**: no `roles` claim at all, and an empty `roles` list, both resolve to no permissions.
  - **Invalid roles**: an unrecognized role name grants nothing.
  - **Expired credentials**: a 401 (authentication failure), correctly distinguished from a 403 (authenticated but unauthorized).
  - Positive controls confirming the above 403s are genuinely role-based, not a route-wiring mistake that would deny everyone.

## Per-document role restriction (2026 Phase 3)

**Status: implemented.** This app is, and remains, a **shared knowledge base by design** — the RAG collections ("documents"/"policies") are a company-wide assistant's source material, not per-user private file storage, and the change below was deliberately scoped to preserve that: it does *not* introduce per-uploader ownership or ordinary-document isolation.

What it closes: a real, confirmed gap where `rag/store.py::similarity_search` filtered retrieval only by `collection`/`status`, with no way to restrict a specific document beyond the three fixed `sensitivity_category` values (compensation/disciplinary/legal). `rag.store.DocumentRecord.restricted_roles` — an optional, operator-set list of role names, entered via the upload form's `restricted_roles` field (comma-separated) — closes that: `rag/graph.py::_generate_node` now blocks summarizing a chunk into an answer unless the caller holds at least one of the document's `restricted_roles`, checked **before** the LLM ever sees the chunk (the same "filter before context construction" principle the `sensitivity_category` gate already followed), and `api/documents.py`'s download route enforces the same check on raw-bytes downloads. Both gates are independent and additive — a document can carry `sensitivity_category`, `restricted_roles`, both, or neither.

`rag.store.DocumentRecord.uploaded_by` (the uploader's `AuthIdentity.subject`) was also added, but **audit-trail only** — never used to scope retrieval, download, or delete. This is a deliberate distinction from `restricted_roles`: knowing *who* uploaded something is useful for investigation; automatically restricting a document to only its uploader would turn this from a shared assistant into per-user file storage, which is not what this app is.

## Known limitations (honestly scoped)

- **No per-resource *ownership*.** Document delete is still `DOCUMENTS_DELETE`-gated, not scoped to "documents this caller uploaded" — any `admin` can delete any document, matching this app's pre-existing "Knowledge Sources page is already fully-privileged" posture (see `CLAUDE.md`). This is an intentional design choice for a shared knowledge base, not a gap being tracked for a future fix — see the "Per-document role restriction" section above for the access-control mechanism this app does support (role-based, not ownership-based).
- **Role assignment is entirely the identity provider's responsibility.** This app has no user-provisioning/role-management UI of its own — see `docs/AUTHENTICATION.md`'s own scope note.
- **The permission set is coarse-grained per data source by default** — `POLICY_RAG_QUERY` gates the whole "policies" collection; per-document granularity now exists via `restricted_roles` above, but is opt-in per upload, not the default for every document.
- **`restricted_roles` role names are not validated against `agent.authz.ROLE_PERMISSIONS`** at upload time — that mapping is deliberately extensible/operator-editable, so a typo'd role name here fails closed (nobody can see the document until a role granting access exists) rather than failing loudly at upload time. Worth a follow-up validation pass if this becomes a common source of operator confusion in practice.
