"""Centralized RBAC + ABAC decision engine for the governed recommendation
lifecycle -- every route in `api/recommendation_governance.py` must call
`authorize_recommendation_action` before doing anything, mirroring
`semantic/catalog_policy.py`'s own "pure policy, no FastAPI/database
import" split exactly (this module is unit-testable with plain,
hand-built values -- no DB, no HTTP). Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).

**Deny-by-default at every branch.** An action this module doesn't
recognize, a `None` where a value was expected -- every one of those is
a `deny`, never an `allow`.

## RBAC (what someone may do at all)

- `identity.rbac.Permission.RECOMMENDATION_REVIEW` -- view a persisted
  recommendation, submit lifecycle feedback (a REVIEWED/ACCEPTED/
  REJECTED/PARTIALLY_USEFUL/INCORRECT verdict), or mark one RESOLVED.
  Granted to `analyst` and `admin` -- judging recommendation quality is
  domain judgment, not account administration (the identical rationale
  `onboarding.policy`/`semantic.catalog_policy` already give for their
  own `*_REVIEW` permission).
- `identity.rbac.Permission.RECOMMENDATION_MANAGE` -- force-expire a
  recommendation. Granted to the `admin` role only -- an administrative
  override of the lifecycle, not a quality judgment.

## ABAC (which specific record)

Every action beyond listing requires the actor's own
`security.tenancy.resolve_actor_tenant_id`/`resolve_tenant_id_for_identity`
to match the target `RecommendationRecord.tenant_id` -- the exact same
scoped pattern `identity.share_policy`/`onboarding.policy`/
`semantic.catalog_policy` already established. A cross-tenant caller is
denied with the same `"cross_tenant"` reason code all three use, never a
different one that might hint the record exists in a tenant it doesn't
belong to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from identity.rbac import Permission


class RecommendationAction(str, Enum):
    VIEW = "view"
    SUBMIT_FEEDBACK = "submit_feedback"
    MARK_RESOLVED = "mark_resolved"
    EXPIRE = "expire"


_REVIEW_OR_MANAGE_ACTIONS: frozenset[RecommendationAction] = frozenset(
    {
        RecommendationAction.VIEW,
        RecommendationAction.SUBMIT_FEEDBACK,
        RecommendationAction.MARK_RESOLVED,
    }
)
_MANAGE_ONLY_ACTIONS: frozenset[RecommendationAction] = frozenset({RecommendationAction.EXPIRE})


@dataclass(frozen=True)
class AuthorizationDecision:
    """The outcome of one `authorize_recommendation_action` call.

    Attributes:
        allowed: Whether the action may proceed.
        reason: A stable machine code (never a sentence) -- mirrors
            `semantic.catalog_policy.AuthorizationDecision`'s own contract.
    """

    allowed: bool
    reason: str


def _allow(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=True, reason=reason)


def _deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=False, reason=reason)


def authorize_recommendation_action(
    *,
    actor_permissions: frozenset[str],
    actor_tenant_id: str | None,
    record_tenant_id: str | None,
    action: RecommendationAction,
) -> AuthorizationDecision:
    """The one function every recommendation-governance route must call.

    Args:
        actor_permissions: The caller's own DB-backed granted permissions,
            as plain strings (`identity.repositories.users
            .get_user_permissions`'s actual return shape) -- resolved by
            the caller, never by this module.
        actor_tenant_id: `security.tenancy.resolve_actor_tenant_id(user)`.
        record_tenant_id: The target `RecommendationRecord.tenant_id`.
            Unlike `semantic.catalog_policy`'s `CREATE_ENTRY` action, there
            is no action here with no existing record to check against --
            a `RecommendationRecord` is only ever created by the live
            `/ask` pipeline itself (`api/recommendation_persistence.py`),
            never through this policy-gated API surface, so every action
            this module recognizes always has a real target record.
        action: Which action is being attempted.

    Returns:
        An `AuthorizationDecision`. Every branch is a `deny` unless an
        explicit, positive condition is met first.
    """
    if record_tenant_id is None:
        return _deny("record_not_found")
    if actor_tenant_id is None or actor_tenant_id != record_tenant_id:
        return _deny("cross_tenant")

    if action in _MANAGE_ONLY_ACTIONS:
        if Permission.RECOMMENDATION_MANAGE in actor_permissions:
            return _allow("recommendation_manage")
        return _deny("missing_permission")

    if action in _REVIEW_OR_MANAGE_ACTIONS:
        if (
            Permission.RECOMMENDATION_REVIEW in actor_permissions
            or Permission.RECOMMENDATION_MANAGE in actor_permissions
        ):
            return _allow("recommendation_review_or_manage")
        return _deny("missing_permission")

    return _deny("unrecognized_action")
