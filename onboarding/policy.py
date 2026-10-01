"""Centralized RBAC + ABAC decision engine for the onboarding engine --
every route in `api/onboarding.py` must call
`authorize_onboarding_action` before doing anything, mirroring
`identity/share_policy.py`'s own "pure policy, no FastAPI/database
import" split exactly (this module is unit-testable with plain, hand-
built values -- no DB, no HTTP).

**Deny-by-default at every branch.** An action this module doesn't
recognize, a `None` where a value was expected -- every one of those is
a `deny`, never an `allow`.

## RBAC (what someone may do at all)

- `identity.rbac.Permission.ONBOARDING_MANAGE` -- create a job, run
  discovery, publish, cancel, retry. Granted to the `admin` role only
  (`identity/rbac.py`'s own `_ADMIN` set) -- creating a job means
  supplying real connection credentials and running discovery/profiling
  against a live database, an admin-tier action, not a reviewer one.
- `identity.rbac.Permission.ONBOARDING_REVIEW` -- decide (confirm/
  reject) a review item. Granted to `analyst` and `admin` -- a subject-
  matter reviewer needs domain judgment, not account administration.

## ABAC (which specific job)

Every action beyond creating a brand-new job requires the actor's own
`security.tenancy.resolve_actor_tenant_id` to match the target
`OnboardingJob.tenant_id` -- the exact same scoped, forward-compatible
pattern `identity.share_policy.authorize_share_action` already
established for conversation sharing (see `identity.models
.OnboardingJob`'s own docstring). A cross-tenant caller is denied with
the same `"cross_tenant"` reason code share_policy uses, never a
different one that might hint the job exists in a tenant it doesn't
belong to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from identity.rbac import Permission


class OnboardingAction(str, Enum):
    CREATE_JOB = "create_job"
    VIEW_JOB = "view_job"
    RUN_DISCOVERY = "run_discovery"
    DECIDE_REVIEW_ITEM = "decide_review_item"
    PUBLISH = "publish"
    CANCEL = "cancel"
    RETRY = "retry"


_MANAGE_ACTIONS: frozenset[OnboardingAction] = frozenset(
    {
        OnboardingAction.CREATE_JOB,
        OnboardingAction.RUN_DISCOVERY,
        OnboardingAction.PUBLISH,
        OnboardingAction.CANCEL,
        OnboardingAction.RETRY,
    }
)
# VIEW_JOB is deliberately readable by either permission -- a reviewer
# who can decide a job's review items must also be able to see the job
# itself (and the discovery summary that gives each item its context).
_VIEW_OR_REVIEW_ACTIONS: frozenset[OnboardingAction] = frozenset(
    {OnboardingAction.VIEW_JOB, OnboardingAction.DECIDE_REVIEW_ITEM}
)


@dataclass(frozen=True)
class AuthorizationDecision:
    """The outcome of one `authorize_onboarding_action` call.

    Attributes:
        allowed: Whether the action may proceed.
        reason: A stable machine code (never a sentence) -- mirrors
            `identity.share_policy.AuthorizationDecision`'s own contract.
    """

    allowed: bool
    reason: str


def _allow(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=True, reason=reason)


def _deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=False, reason=reason)


def authorize_onboarding_action(
    *,
    actor_permissions: frozenset[str],
    actor_tenant_id: str | None,
    job_tenant_id: str | None,
    action: OnboardingAction,
) -> AuthorizationDecision:
    """The one function every onboarding route must call.

    Args:
        actor_permissions: The caller's own DB-backed granted permissions,
            as plain strings (`identity.repositories.users
            .get_user_permissions`'s actual return shape) -- resolved by
            the caller, never by this module (keeps this file's own
            dependency surface at zero, per its docstring). `Permission`
            is itself a `str` subclass (`identity.rbac.Permission(str,
            Enum)`), so `Permission.ONBOARDING_MANAGE in actor_permissions`
            below compares correctly by value either way.
        actor_tenant_id: `security.tenancy.resolve_actor_tenant_id(user)`.
        job_tenant_id: The target `OnboardingJob.tenant_id`, or `None`
            for `CREATE_JOB` (there is no existing job to check a tenant
            against yet -- the new job is simply created with the
            actor's own tenant id).
        action: Which action is being attempted.

    Returns:
        An `AuthorizationDecision`. Every branch is a `deny` unless an
        explicit, positive condition is met first.
    """
    if action == OnboardingAction.CREATE_JOB:
        if Permission.ONBOARDING_MANAGE in actor_permissions:
            return _allow("onboarding_manage")
        return _deny("missing_permission")

    if job_tenant_id is None:
        return _deny("job_not_found")
    if actor_tenant_id is None or actor_tenant_id != job_tenant_id:
        return _deny("cross_tenant")

    if action in _MANAGE_ACTIONS:
        if Permission.ONBOARDING_MANAGE in actor_permissions:
            return _allow("onboarding_manage")
        return _deny("missing_permission")

    if action in _VIEW_OR_REVIEW_ACTIONS:
        if (
            Permission.ONBOARDING_MANAGE in actor_permissions
            or Permission.ONBOARDING_REVIEW in actor_permissions
        ):
            return _allow("onboarding_manage_or_review")
        return _deny("missing_permission")

    return _deny("unrecognized_action")
