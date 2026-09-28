"""A **scoped, forward-compatible** tenant-resolution hook for the
conversation-sharing feature (`identity/share_policy.py`,
`identity/repositories/shares.py`) -- not a claim that this application has
become multi-tenant.

This codebase is explicitly single-tenant today: `identity.models.User`/
`Conversation` have no `tenant_id` column at all, and `agent/rate_limit.py`
already discloses "no-auth/no-tenant-isolation" as a known, accepted
architectural fact for this deployment shape (see that module's own
docstring). Retrofitting real multi-tenancy across the whole application
(a `Tenant` model, `tenant_id` foreign keys on every existing table,
tenant-aware rate limiting/RBAC) is a separate, much larger project than
"add secure conversation sharing" and was deliberately out of scope for
this feature -- see `docs/SHARING_SECURITY.md`'s own "Tenant scope" note
for the explicit decision record.

Instead, only the *new* sharing tables (`conversation_shares` and, via it,
every ABAC check `identity.share_policy.authorize_share_action` performs)
carry a `tenant_id` column, and this module is the **one function** that
resolves what an actor's tenant is. Today it always returns the same
constant for every real user in this deployment, so production behavior
is completely unchanged -- but the ABAC engine genuinely enforces a
tenant-match condition using whatever this function returns, which is
what makes "cross-tenant member cannot view even with a valid-looking ID"
a real, exercised code path today (see
`tests/test_share_policy.py::TestCrossTenantIsolation`), not a check that
only starts existing once real multi-tenancy is eventually built. A future
multi-tenant retrofit only ever needs to change the body of
`resolve_actor_tenant_id` (e.g. to read a real `users.tenant_id` column)
-- no change to the policy engine, the schema shape, or any call site that
already threads a tenant id through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from identity.models import User

#: The single tenant every real user in this deployment belongs to today.
#: Never persisted -- it exists purely so `ConversationShare.tenant_id` and
#: every ABAC tenant comparison have a concrete, stable value to compare
#: against, without requiring a schema change anywhere else.
DEFAULT_TENANT_ID = "default"


def resolve_actor_tenant_id(user: User | None) -> str | None:
    """Returns the tenant a given user belongs to, for ABAC purposes only.

    Returns `None` for an unauthenticated/anonymous actor (there is no
    tenant to resolve) -- callers must treat `None` as "unknown," which
    `identity.share_policy.authorize_share_action`'s deny-by-default
    posture already does (an unknown tenant never matches a share's own
    `tenant_id`, so an anonymous caller can only ever reach a share via its
    `access_mode == "anyone_with_link"` path, which does not compare
    tenants at all -- see that module's own docstring).

    Always `DEFAULT_TENANT_ID` for a real, non-`None` user today -- see
    this module's own docstring for why, and for what a real multi-tenant
    deployment would need to change here instead of anywhere else.
    """
    if user is None:
        return None
    return DEFAULT_TENANT_ID
