"""Server-side authorization (RBAC) -- pure policy, no web framework
dependency -- 2026 Phase 2 security review.

Separate from, and layered on top of, `api/auth.py`/`security/oidc.py`
(authentication: "who is this?"). This module answers "what may they do?"
-- every check here reads a `security.oidc.AuthIdentity` (never a raw
token or request header directly). Deliberately has **no FastAPI/`api.*`
import** -- `agent/nodes.py` and `agent/orchestrator/nodes.py` (business
logic, not the HTTP layer) both need to consult it directly (see below),
and `agent/` importing anything from `api/` would be a layering inversion.
The FastAPI-specific dependency wiring (`Depends(...)`, request/response
handling) lives in `api/authz.py`, built on top of this module.

**No frontend/UI-level restriction is treated as a security boundary
anywhere in this codebase** -- the React dashboard may hide a button a
caller lacks permission for, but the corresponding API route (`api/authz
.require_permission`) or, for the multi-source orchestrator, the
corresponding internal node (this module's `has_permission`, called
directly) re-checks independently and would reject the same request made
directly with curl.

## Role model

Four default roles, in ascending order of privilege: `viewer`, `user`,
`analyst`, `admin`. **Extensible, not a closed set** -- `ROLE_PERMISSIONS`
is a plain dict an operator can add entries to (or, for a larger
deployment, replace with a lookup against an external policy store)
without touching any call site: `has_permission`/`permissions_for` only
ever read from it, they never hardcode role names.

An identity's roles come from `security.oidc.AuthIdentity.roles` --
either real JWT `roles`/custom claims (OIDC mode) or a fixed "admin" for
the "none"/"static_token" modes (see `api/auth.py`'s own docstring for why
those two grant full access: a dev-mode no-op and a single shared secret
both have no natural sub-identity to scope down further, and granting
`admin` there just continues what a valid credential already implicitly
granted before this module existed -- it does not introduce a new
privilege). **A role name not present in `ROLE_PERMISSIONS` grants no
permissions at all** (`permissions_for` silently skips it) -- fail closed,
never fail open on an unrecognized/misspelled role.

## What this protects (see docs/AUTHORIZATION.md for the full mapping)

- SQL execution and restricted-column visibility (`agent/nodes.py`'s
  `validate_sql_node`, `api/main.py`'s `POST /execute`).
- The multi-source orchestrator's own per-source fan-out (`agent/
  orchestrator/nodes.py::router_node`) -- closing 2026 Phase 1's AGT-01/
  R-008 finding: the LLM's own routing decision alone used to be sufficient
  to reach the access-sensitive "policies" RAG collection or trigger paid
  media generation, with no independent check in between. That check now
  lives here, applied *after* the LLM picks a source and *before* that
  source's subgraph node actually runs -- the LLM may still request a
  source, but it never gets to decide alone whether the request is
  permitted (the core principle `SECURITY.md`/this project's own audit
  history states explicitly).
- Document upload/download/delete, including the sensitivity-tagged
  ("compensation"/"disciplinary"/"legal") document class (`api/documents.py`).
- Web search, media generation/approval, media search (`api/generation.py`,
  `api/media_search.py`, orchestrator nodes).
- Schema refresh and the golden-example feedback write
  (`api/main.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from security.oidc import AuthIdentity


class Permission(str, Enum):
    """One discrete capability. Deliberately fine-grained (a document
    *read* is a different permission than a document *delete*) rather than
    a handful of broad buckets -- see this module's docstring for why that
    matters for the "protect sensitive columns/RAG collections/expensive
    operations" requirement specifically: a coarse "can use documents"
    permission couldn't distinguish "may list/download ordinary files"
    from "may delete anything" or "may read HR/legal-sensitive files"."""

    ASK = "ask"
    EXECUTE_SQL = "execute_sql"
    VIEW_RESTRICTED_COLUMNS = "view_restricted_columns"
    SCHEMA_REFRESH = "schema_refresh"
    DOCUMENTS_READ = "documents_read"
    DOCUMENTS_WRITE = "documents_write"
    DOCUMENTS_DELETE = "documents_delete"
    DOCUMENTS_READ_SENSITIVE = "documents_read_sensitive"
    POLICY_RAG_QUERY = "policy_rag_query"
    WEB_SEARCH = "web_search"
    MEDIA_GENERATE = "media_generate"
    MEDIA_SEARCH = "media_search"
    GOLDEN_EXAMPLE_WRITE = "golden_example_write"
    VOICE_USE = "voice_use"
    ADMIN_CONFIG = "admin_config"


_VIEWER: frozenset[Permission] = frozenset(
    {Permission.ASK, Permission.DOCUMENTS_READ, Permission.MEDIA_SEARCH, Permission.VOICE_USE}
)
_USER: frozenset[Permission] = _VIEWER | {
    Permission.EXECUTE_SQL,
    Permission.WEB_SEARCH,
    Permission.GOLDEN_EXAMPLE_WRITE,
}
_ANALYST: frozenset[Permission] = _USER | {
    Permission.VIEW_RESTRICTED_COLUMNS,
    Permission.DOCUMENTS_READ_SENSITIVE,
    Permission.POLICY_RAG_QUERY,
    Permission.MEDIA_GENERATE,
}
_ADMIN: frozenset[Permission] = _ANALYST | {
    Permission.DOCUMENTS_WRITE,
    Permission.DOCUMENTS_DELETE,
    Permission.SCHEMA_REFRESH,
    Permission.ADMIN_CONFIG,
}

#: Default role -> permission-set map. An operator may add roles (e.g. a
#: narrower "auditor" role) or adjust an existing role's permission set by
#: editing this dict directly -- there is deliberately no code path that
#: hardcodes a role name anywhere else in this module.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": _VIEWER,
    "user": _USER,
    "analyst": _ANALYST,
    "admin": _ADMIN,
}


def permissions_for(roles: Sequence[str]) -> frozenset[Permission]:
    """Union of every permission granted by any of `roles`.

    An unrecognized role name (a typo, a role from an identity provider
    this app's `ROLE_PERMISSIONS` hasn't been told about yet) contributes
    no permissions -- fail closed, never fail open on an unknown role.
    """
    result: set[Permission] = set()
    for role in roles:
        result |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(result)


def has_permission(identity: AuthIdentity, permission: Permission) -> bool:
    """True if any of `identity.roles` grants `permission`."""
    return permission in permissions_for(identity.roles)


def has_role_permission(roles: Sequence[str], permission: Permission) -> bool:
    """Same as `has_permission`, but takes a bare role sequence directly --
    for callers that only have `AgentState["caller_roles"]`/
    `OrchestratorState["caller_roles"]` (a plain tuple, set by
    `agent.graph.run_agent`/`agent.orchestrator.graph.run_orchestrated`
    from the caller's `AuthIdentity.roles`) rather than a full
    `AuthIdentity` object -- `agent/nodes.py` and
    `agent/orchestrator/nodes.py` both use this form, since the LangGraph
    state dict is the only thing they have in hand.
    """
    return permission in permissions_for(roles)
