"""Centralized RBAC + ABAC decision engine for the semantic catalog --
every route in `api/semantic_catalog.py` must call
`authorize_catalog_action` before doing anything, mirroring
`onboarding/policy.py`'s own "pure policy, no FastAPI/database import"
split exactly (this module is unit-testable with plain, hand-built
values -- no DB, no HTTP).

**Deny-by-default at every branch.** An action this module doesn't
recognize, a `None` where a value was expected -- every one of those is
a `deny`, never an `allow`.

## RBAC (what someone may do at all)

- `identity.rbac.Permission.CATALOG_MANAGE` -- create an entry, edit a
  draft, publish. Granted to the `admin` role only (`identity/rbac.py`'s
  own `_ADMIN` set) -- publishing promotes an entry to
  `agent.provenance.DataTruthLevel.CONFIRMED_BUSINESS_TRUTH` and makes it
  live in retrieval, an admin-tier action, not a reviewer one (mirrors
  `onboarding.policy`'s own `ONBOARDING_MANAGE` precedent exactly).
- `identity.rbac.Permission.CATALOG_REVIEW` -- review (approve to
  `reviewed`) or request changes (send back to `draft`). Granted to
  `analyst` and `admin` -- a subject-matter reviewer needs domain
  judgment, not account administration.

## ABAC (which specific entry)

Every action beyond creating a brand-new entry requires the actor's own
`security.tenancy.resolve_actor_tenant_id` to match the target
`SemanticCatalogEntry.tenant_id` -- the exact same scoped pattern
`identity.share_policy`/`onboarding.policy` already established. A
cross-tenant caller is denied with the same `"cross_tenant"` reason code
both of those modules use, never a different one that might hint the
entry exists in a tenant it doesn't belong to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from identity.rbac import Permission


class CatalogAction(str, Enum):
    CREATE_ENTRY = "create_entry"
    VIEW_ENTRY = "view_entry"
    EDIT_DRAFT = "edit_draft"
    REVIEW = "review"
    REQUEST_CHANGES = "request_changes"
    PUBLISH = "publish"


_MANAGE_ACTIONS: frozenset[CatalogAction] = frozenset(
    {CatalogAction.CREATE_ENTRY, CatalogAction.EDIT_DRAFT, CatalogAction.PUBLISH}
)
# VIEW_ENTRY is deliberately readable by either permission -- a reviewer
# who can decide an entry's review state must also be able to see it.
_VIEW_OR_REVIEW_ACTIONS: frozenset[CatalogAction] = frozenset(
    {CatalogAction.VIEW_ENTRY, CatalogAction.REVIEW, CatalogAction.REQUEST_CHANGES}
)


@dataclass(frozen=True)
class AuthorizationDecision:
    """The outcome of one `authorize_catalog_action` call.

    Attributes:
        allowed: Whether the action may proceed.
        reason: A stable machine code (never a sentence) -- mirrors
            `onboarding.policy.AuthorizationDecision`'s own contract.
    """

    allowed: bool
    reason: str


def _allow(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=True, reason=reason)


def _deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=False, reason=reason)


def authorize_catalog_action(
    *,
    actor_permissions: frozenset[str],
    actor_tenant_id: str | None,
    entry_tenant_id: str | None,
    action: CatalogAction,
) -> AuthorizationDecision:
    """The one function every semantic-catalog route must call.

    Args:
        actor_permissions: The caller's own DB-backed granted permissions,
            as plain strings (`identity.repositories.users
            .get_user_permissions`'s actual return shape) -- resolved by
            the caller, never by this module.
        actor_tenant_id: `security.tenancy.resolve_actor_tenant_id(user)`.
        entry_tenant_id: The target `SemanticCatalogEntry.tenant_id`, or
            `None` for `CREATE_ENTRY` (there is no existing entry to check
            a tenant against yet -- the new entry is simply created with
            the actor's own tenant id).
        action: Which action is being attempted.

    Returns:
        An `AuthorizationDecision`. Every branch is a `deny` unless an
        explicit, positive condition is met first.
    """
    if action == CatalogAction.CREATE_ENTRY:
        if Permission.CATALOG_MANAGE in actor_permissions:
            return _allow("catalog_manage")
        return _deny("missing_permission")

    if entry_tenant_id is None:
        return _deny("entry_not_found")
    if actor_tenant_id is None or actor_tenant_id != entry_tenant_id:
        return _deny("cross_tenant")

    if action in _MANAGE_ACTIONS:
        if Permission.CATALOG_MANAGE in actor_permissions:
            return _allow("catalog_manage")
        return _deny("missing_permission")

    if action in _VIEW_OR_REVIEW_ACTIONS:
        if (
            Permission.CATALOG_MANAGE in actor_permissions
            or Permission.CATALOG_REVIEW in actor_permissions
        ):
            return _allow("catalog_manage_or_review")
        return _deny("missing_permission")

    return _deny("unrecognized_action")
