"""Tenant resolution -- the single place this codebase answers "which
tenant is this caller in," and the only place that answer is ever produced.

**This module changed meaning in Prompt 20**
(`20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`). It used to be an
explicitly forward-compatible stub: `identity.models.User` had no
`tenant_id` column at all, and both resolver functions returned one
hardcoded constant for every caller, so the five `tenant_id` columns and
four deny-by-default ABAC policy modules that genuinely compared tenants
(`identity/share_policy.py`, `onboarding/policy.py`,
`semantic/catalog_policy.py`, `recommendation/governance_policy.py`) were
all comparing a constant against itself -- real code paths, but vacuous in
any real deployment. Tenancy is now a **persisted, server-controlled fact**:
`identity.models.Tenant` is a real table, `users.tenant_id` is a real
`RESTRICT` foreign key onto it, and every one of those comparisons is
load-bearing. No call site of either resolver had to change for that to
become true, which was the whole point of routing them through here.

Three rules this module exists to enforce:

1.  **A tenant id is never read from anything a client sends.** Not a
    request body, not a query parameter, not a header. The two resolvers
    below read it only from (a) an `identity.models.User` row this server
    loaded from its own database, or (b) a `security.oidc.AuthIdentity`
    whose `tenant_id` was itself set from a *signature-verified* source --
    this app's own JWT `tid` claim (minted server-side by
    `identity.security.create_access_token` from the user's own row) or an
    IdP-signed OIDC claim named by `Settings.oidc_tenant_claim`.
    `reject_client_tenant_override` is the belt-and-braces check that a
    request carrying a tenant-shaped header is refused outright rather than
    silently ignored, so an operator who later adds a proxy that *does*
    forward such a header finds out immediately.

2.  **Unknown means deny, never "default".** `resolve_tenant_context`
    fails closed for a tenant id with no row, a soft-deleted row, or a
    `"suspended"` row. It is deliberately the *one* gate that does this, so
    a suspension takes effect for every downstream check at once rather
    than needing each of the four policy modules to remember a status
    check of its own.

3.  **Resolution is per request, never cached.** A suspension or tenant
    reassignment must not wait for an access token to expire. The one
    place this trades off against a database round trip
    (`resolve_tenant_context`) is called only by routes that already hold
    an identity-database session for other reasons.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from identity.models import User
    from sqlalchemy.orm import Session

    from security.oidc import AuthIdentity

logger = logging.getLogger(__name__)

#: The tenant every pre-existing row in this schema belongs to, and the
#: tenant a brand-new account lands in unless an operator says otherwise.
#:
#: Persisted for real since Prompt 20 -- as a `tenants` row seeded by both
#: `identity/migrations/versions/a4c7e2b9d6f5_multi_tenant_boundary.py` (a
#: migrated database) and `identity.bootstrap.ensure_default_tenant` (a
#: freshly `create_all`-built one). It is still the value every existing
#: deployment resolves to, which is exactly why the Prompt 20 upgrade
#: changes no authorization outcome for anybody: every user row lands here,
#: and every historical `tenant_id` already held this string.
DEFAULT_TENANT_ID = "default"

#: Request headers that look like an attempt to assert a tenant from the
#: client side. Refused by `reject_client_tenant_override` -- see rule 1
#: above. Lowercase; header comparison is case-insensitive.
CLIENT_TENANT_HEADERS: frozenset[str] = frozenset(
    {"x-tenant-id", "x-tenant", "tenant-id", "x-tenant-override"}
)


class TenantResolutionError(Exception):
    """A tenant could not be resolved, or resolved to one that may not be
    used (missing, soft-deleted, or suspended).

    Carries a deliberately non-specific `safe_message`: a caller must not
    be able to tell "that tenant does not exist" from "that tenant is
    suspended" from "you are not in that tenant," for the same
    anti-enumeration reason every tenant-scoped route in this codebase maps
    a cross-tenant denial to the same 404 a genuinely nonexistent resource
    gets (see `api/semantic_catalog.py`'s own precedent).
    """

    safe_message = "Your account's tenant is not available. Please contact your administrator."


@dataclass(frozen=True)
class TenantContext:
    """A resolved, *verified-usable* tenant -- the proof-of-work token every
    tenant-scoped operation should carry rather than a bare `str`.

    Only `resolve_tenant_context` constructs one, and only after confirming
    the tenant exists, is not soft-deleted, and is `"active"`. A function
    that accepts a `TenantContext` therefore cannot be called with a
    suspended tenant's id by mistake, which a `tenant_id: str` parameter
    can't express.

    Attributes:
        tenant_id: The tenant's primary key (`identity.models.Tenant.id`) --
            an operator-chosen slug, never client-supplied.
        name: Display name, for logging and admin surfaces only; never an
            authorization input.
    """

    tenant_id: str
    name: str


def resolve_actor_tenant_id(user: User | None) -> str | None:
    """Returns the tenant a given user belongs to, for ABAC purposes.

    Reads `user.tenant_id` -- a real, server-written column since Prompt 20
    (it returned a hardcoded constant before that; see this module's own
    docstring). Falls back to `DEFAULT_TENANT_ID` only if the attribute is
    absent or `None`, which can happen for a lightweight test stand-in or a
    row read back from a database the Prompt 20 migration hasn't been
    applied to yet -- never a silent widening of access, since
    `DEFAULT_TENANT_ID` is the *most* restrictive answer available (it is
    simply where every pre-Prompt-20 row already lived).

    Returns `None` for an unauthenticated/anonymous actor (there is no
    tenant to resolve). Callers must treat `None` as "unknown," which every
    deny-by-default ABAC check in this codebase already does -- an unknown
    tenant never matches a resource's own `tenant_id`, so an anonymous
    caller can only ever reach a resource via a path that does not compare
    tenants at all (`identity.share_policy`'s `access_mode ==
    "anyone_with_link"` being the only such path).
    """
    if user is None:
        return None
    return getattr(user, "tenant_id", None) or DEFAULT_TENANT_ID


def resolve_tenant_id_for_identity(identity: AuthIdentity | None) -> str | None:
    """Same contract as `resolve_actor_tenant_id`, for the other real caller
    shape this codebase has: a request authenticated via
    `security.oidc.AuthIdentity` (every `Settings.auth_mode`) rather than a
    local-accounts `identity.models.User` ORM row.

    A sibling function rather than a reuse of `resolve_actor_tenant_id`,
    because the two types are genuinely different and not interchangeable:
    `api/shares.py`/`api/onboarding.py` always have a real `User` in hand,
    while `api/main.py`'s `/ask` handler only ever has an `AuthIdentity`.

    Reads `AuthIdentity.tenant_id`, which `api/auth.py` sets from a
    signature-verified source only:

      - `mode="local"`: this app's own JWT `tid` claim, minted by
        `identity.security.create_access_token` from the signing user's own
        `users.tenant_id` row. Server-controlled end to end.
      - `mode="oidc"`: the IdP-signed claim named by
        `Settings.oidc_tenant_claim`, if configured; `DEFAULT_TENANT_ID`
        otherwise. An OIDC claim is trusted here only because
        `security.oidc` has already verified the token's signature against
        the issuer's live keys -- it is the IdP's assertion, not the
        client's.
      - `mode="none"`/`"static_token"`: `DEFAULT_TENANT_ID`. Every caller
        under either mode is indistinguishable by design (see
        `security.oidc.real_caller_subject`), so there is no per-caller
        tenant to resolve and the single default tenant is the only honest
        answer. **This is the one deployment shape where tenant isolation
        is not a meaningful boundary**, and it is documented as such in
        `docs/MULTI_TENANCY.md` rather than papered over.

    Returns `None` for `identity is None` (no caller to resolve for).
    """
    if identity is None:
        return None
    return getattr(identity, "tenant_id", None) or DEFAULT_TENANT_ID


def resolve_tenant_context(session: Session, tenant_id: str | None) -> TenantContext:
    """Verifies `tenant_id` names a usable tenant and returns its
    `TenantContext`, raising `TenantResolutionError` otherwise.

    Fails closed on all four of: `tenant_id is None` (no resolvable
    caller), no such tenant row, a soft-deleted row, and a `"suspended"`
    row. This is the single chokepoint rule 2 in this module's docstring
    describes -- suspending a tenant via
    `identity.repositories.tenants.set_tenant_status` takes effect for
    every one of its users on their next request, across every
    tenant-scoped route at once, without any route needing a status check
    of its own.

    Reads the status live rather than caching it, deliberately: a
    suspension that only took effect once an access token expired would be
    a security control with an operator-visible lag.

    Every failure logs the real reason server-side at WARNING but raises
    the same generic `TenantResolutionError.safe_message` -- see that
    exception's own docstring for the anti-enumeration rationale.
    """
    from identity.repositories.tenants import get_tenant

    if tenant_id is None:
        logger.warning("[tenancy] tenant resolution refused -- no tenant for this caller")
        raise TenantResolutionError("No tenant could be resolved for this caller.")

    tenant = get_tenant(session, tenant_id)
    if tenant is None:
        logger.warning("[tenancy] tenant resolution refused -- tenant %r does not exist", tenant_id)
        raise TenantResolutionError(f"Tenant {tenant_id!r} does not exist.")
    if tenant.deleted_at is not None:
        logger.warning("[tenancy] tenant resolution refused -- tenant %r is deleted", tenant_id)
        raise TenantResolutionError(f"Tenant {tenant_id!r} is deleted.")
    if tenant.status != "active":
        logger.warning(
            "[tenancy] tenant resolution refused -- tenant %r has status %r",
            tenant_id,
            tenant.status,
        )
        raise TenantResolutionError(
            f"Tenant {tenant_id!r} is not active (status={tenant.status!r})."
        )

    return TenantContext(tenant_id=tenant.id, name=tenant.name)


def reject_client_tenant_override(headers: object) -> None:
    """Raises `TenantResolutionError` if the request carries any header in
    `CLIENT_TENANT_HEADERS` -- rule 1 in this module's docstring.

    Refusing is deliberately chosen over silently ignoring. Ignoring is
    safe *today* (nothing reads such a header), but it leaves no signal if
    a future reverse-proxy configuration, SDK, or well-meaning change
    starts forwarding one -- at which point "we ignore it" would be an
    assumption nobody re-verified. A hard refusal makes that situation a
    loud, immediate failure instead.

    `headers` is typed `object` rather than `starlette.datastructures
    .Headers` so this module stays importable with no web-framework
    dependency (matching `security/tenancy.py`'s existing TYPE_CHECKING-only
    import posture). Anything exposing a `__contains__` over lowercase
    header names works, which is exactly what both Starlette's `Headers`
    and a plain `dict` do.
    """
    for header in CLIENT_TENANT_HEADERS:
        try:
            present = header in headers  # type: ignore[operator]
        except TypeError:  # pragma: no cover - a non-container was passed
            return
        if present:
            logger.warning(
                "[tenancy] request refused -- client-supplied tenant header %r is never trusted",
                header,
            )
            raise TenantResolutionError(
                f"Request carried a client-supplied tenant header ({header!r}), which is never trusted."
            )
