"""Server-side navigation policy -- Prompt 32 (`32_ROLE_BASED_NAVIGATION_CONTRACT.md`).

One source of truth for which screens and actions a signed-in caller may use.
The frontend renders its navigation and route guards from this, and never
decides visibility from a role name itself. Visibility is only a presentation
of an access decision the backend already makes. Every item here is backed by
the same permission check its API routes use, so navigation can never offer a
screen whose data calls would be refused.

Pure policy: no FastAPI, no database. Inputs are the caller's two permission
sets, computed by the caller. Deny-by-default throughout: an item is visible
only if one of its permissions is granted.

Two permission systems are in play, and this module reads both:

- `agent.authz.Permission` (base roles: `viewer`, `user`, `analyst`, `admin`).
  Gates chat, documents, media and SQL execution. Available to every
  authenticated identity, from the roles it carries.
- `identity.rbac.Permission` (DB-backed, local accounts only). Gates
  onboarding, the semantic catalog, recommendations and the admin dashboards.
  Only a local account has these, so an OIDC or static-token caller gets none.
  That matches `api.identity_authz.require_local_user`, which refuses them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from identity.rbac import Permission as IdentityPermission

from agent.authz import Permission as AgentPermission


@dataclass(frozen=True)
class NavItem:
    """One screen in the application.

    Attributes:
        id: Stable identifier the frontend keys on (never a display label).
        path: The route this screen lives at.
        group: Presentation grouping only ("workspace", "review",
            "administration"). Carries no access meaning.
        agent_any: Visible if the caller holds any of these `agent.authz`
            permissions.
        identity_any: Visible if the caller holds any of these identity
            permissions (string values, as `get_user_permissions` returns).
    """

    id: str
    path: str
    group: str
    agent_any: frozenset[AgentPermission] = field(default_factory=frozenset)
    identity_any: frozenset[str] = field(default_factory=frozenset)


def _identity(*permissions: IdentityPermission) -> frozenset[str]:
    return frozenset(p.value for p in permissions)


NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem(
        id="chat",
        path="/",
        group="workspace",
        agent_any=frozenset({AgentPermission.ASK}),
    ),
    NavItem(
        id="knowledge_sources",
        path="/knowledge-sources",
        group="workspace",
        agent_any=frozenset({AgentPermission.DOCUMENTS_READ}),
    ),
    NavItem(
        id="media_search",
        path="/media-search",
        group="workspace",
        agent_any=frozenset({AgentPermission.MEDIA_SEARCH}),
    ),
    NavItem(
        id="recommendations",
        # Not `/recommendations`: that path is the governance API's list route
        # (`api/recommendation_governance.py`), so a page reload there would
        # reach JSON, not the screen. See `frontend/src/App.tsx`.
        path="/recommended-actions",
        group="review",
        identity_any=_identity(
            IdentityPermission.RECOMMENDATION_REVIEW, IdentityPermission.RECOMMENDATION_MANAGE
        ),
    ),
    NavItem(
        id="db_onboarding",
        path="/db-onboarding",
        group="review",
        identity_any=_identity(
            IdentityPermission.ONBOARDING_MANAGE, IdentityPermission.ONBOARDING_REVIEW
        ),
    ),
    NavItem(
        id="semantic_review",
        path="/semantic-review",
        group="review",
        identity_any=_identity(
            IdentityPermission.CATALOG_MANAGE, IdentityPermission.CATALOG_REVIEW
        ),
    ),
    NavItem(
        id="tenant_admin",
        path="/tenant-admin",
        group="administration",
        identity_any=_identity(IdentityPermission.ADMIN_DASHBOARD_READ),
    ),
    NavItem(
        id="platform_admin",
        path="/platform-admin",
        group="administration",
        identity_any=_identity(IdentityPermission.PLATFORM_ADMIN),
    ),
)

_ITEMS_BY_ID: dict[str, NavItem] = {item.id: item for item in NAV_ITEMS}
_ITEMS_BY_PATH: dict[str, NavItem] = {item.path: item for item in NAV_ITEMS}


def nav_item_for_path(path: str) -> NavItem | None:
    """The screen registered at exactly `path`, or `None`. Used to decide
    whether a reported denied-route path is one this app actually has."""
    return _ITEMS_BY_PATH.get(path)


def nav_item_by_id(item_id: str) -> NavItem | None:
    return _ITEMS_BY_ID.get(item_id)


def is_item_visible(
    item: NavItem,
    *,
    agent_permissions: frozenset[AgentPermission],
    identity_permissions: frozenset[str],
) -> bool:
    """True if the caller holds at least one of the item's permissions."""
    if item.agent_any & agent_permissions:
        return True
    return bool(item.identity_any & identity_permissions)


@dataclass(frozen=True)
class CapabilityRule:
    """One named action the frontend may offer. A capability is granted by
    exactly one permission, so each button maps to one backend check."""

    name: str
    agent: AgentPermission | None = None
    identity: IdentityPermission | None = None


#: Every capability the frontend reads. A button that is not listed here has no
#: server-side meaning and must not be shown on the basis of a role name.
CAPABILITY_RULES: tuple[CapabilityRule, ...] = (
    CapabilityRule("ask", agent=AgentPermission.ASK),
    CapabilityRule("execute_sql", agent=AgentPermission.EXECUTE_SQL),
    CapabilityRule("manage_documents", agent=AgentPermission.DOCUMENTS_WRITE),
    CapabilityRule("refresh_schema", agent=AgentPermission.SCHEMA_REFRESH),
    CapabilityRule("manage_onboarding", identity=IdentityPermission.ONBOARDING_MANAGE),
    CapabilityRule("review_onboarding", identity=IdentityPermission.ONBOARDING_REVIEW),
    CapabilityRule("manage_catalog", identity=IdentityPermission.CATALOG_MANAGE),
    CapabilityRule("review_catalog", identity=IdentityPermission.CATALOG_REVIEW),
    CapabilityRule("review_recommendations", identity=IdentityPermission.RECOMMENDATION_REVIEW),
    CapabilityRule("manage_recommendations", identity=IdentityPermission.RECOMMENDATION_MANAGE),
    CapabilityRule("view_tenant_dashboard", identity=IdentityPermission.ADMIN_DASHBOARD_READ),
    CapabilityRule("manage_tenant_users", identity=IdentityPermission.USERS_ASSIGN_ROLES),
    CapabilityRule("platform_admin", identity=IdentityPermission.PLATFORM_ADMIN),
)


def resolve_capabilities(
    *,
    agent_permissions: frozenset[AgentPermission],
    identity_permissions: frozenset[str],
) -> dict[str, bool]:
    """Every capability in `CAPABILITY_RULES`, mapped to granted or not.

    Returns a complete dict (every name present) so the frontend never has to
    treat a missing key as meaning anything.
    """
    result: dict[str, bool] = {}
    for rule in CAPABILITY_RULES:
        granted = (rule.agent is not None and rule.agent in agent_permissions) or (
            rule.identity is not None and rule.identity.value in identity_permissions
        )
        result[rule.name] = granted
    return result


def resolve_navigation(
    *,
    agent_permissions: frozenset[AgentPermission],
    identity_permissions: frozenset[str],
) -> tuple[list[NavItem], dict[str, bool]]:
    """The screens visible to this caller, in registry order, plus the full
    capability map. Both come from the same two permission sets, so they
    cannot disagree."""
    visible = [
        item
        for item in NAV_ITEMS
        if is_item_visible(
            item,
            agent_permissions=agent_permissions,
            identity_permissions=identity_permissions,
        )
    ]
    capabilities = resolve_capabilities(
        agent_permissions=agent_permissions,
        identity_permissions=identity_permissions,
    )
    return visible, capabilities
