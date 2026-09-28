"""Centralized, server-side RBAC + ABAC decision engine for conversation
sharing -- the single place every share-touching route, background job, and
resolver (`api/shares.py`, `identity/repositories/shares.py`'s projection
builder, attachment download) must call before doing anything. Mirrors
`agent/authz.py`'s own "pure policy, no FastAPI/database import" split
deliberately: this module never imports `fastapi`, `sqlalchemy`, or any
repository function, so it can be unit-tested with plain, hand-built model
instances (no DB, no HTTP) and can never accidentally develop a
side-effecting query buried inside a policy check.

**Deny-by-default at every branch.** An action this module doesn't
recognize, a role it doesn't recognize, a `None` where a value was
expected -- every one of those is a `deny`, never an `allow`. There is no
code path in this module that returns `allowed=True` without an explicit,
positive condition being met first.

## RBAC roles (who someone is, for this share)

- **Owner** (`share.owner_user_id == actor.id`) -- may create, update,
  invite, remove members, regenerate the link, revoke, and view/download
  its own share's projection (a preview of exactly what a viewer would
  see). The only role that may reach `_OWNER_ONLY_ACTIONS`.
- **Viewer** (`ShareMember.role == "viewer"`, `status == "active"`) -- may
  view the approved snapshot/sources and download approved attachments
  only. No other action.
- **Anonymous link viewer** -- only reachable when `share.access_mode ==
  "anyone_with_link"` *and* a currently-valid `ShareLink` is presented;
  never requires (or checks) tenant membership, since there is no
  authenticated identity to check it against.
- **Editor** is intentionally never modeled here at all -- per this
  feature's own spec, exposing an edit role on a shared conversation was
  explicitly out of scope ("do not expose unless every edit operation is
  separately designed and tested"), so there is no code path anywhere in
  this application that could grant it.

## ABAC conditions (ordered, first failing condition wins)

For every action: the parent conversation must not be soft-deleted. For an
owner-only action: the actor must be authenticated, be the recorded owner,
and belong to the share's own tenant (see `security.tenancy` for why this
tenant check exists and what it currently resolves to). For a viewer
action: the share itself must not be revoked/disabled/expired; if a
`ShareLink` is involved, that specific link must not be revoked/expired
either; a public link additionally requires `access_mode ==
"anyone_with_link"` and a presented link; an invite-only share additionally
requires the actor be authenticated, belong to the share's tenant, hold an
active (non-revoked, non-expired) `"viewer"`-role membership.

`reason` on every `AuthorizationDecision` is a **stable machine code**
(`"cross_tenant"`, `"share_expired"`, ...), never a sentence -- callers
(`api/shares.py`, `identity.share_audit`) use it both to pick a safe
user-facing message and as the structured `reason` field on a
`ShareAuditEvent`, and it must never itself carry anything private.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from identity.models import ConversationShare, ShareLink, ShareMember, User


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """`expires_at <= now`, normalizing a naive datetime to UTC first.

    A value read back from SQLite (this project's own test/dev engine, see
    `identity/models.py`'s module docstring) loses its timezone -- comparing
    it directly against an aware `now` raises `TypeError`. PostgreSQL's
    `TIMESTAMPTZ` (the real production column type) never has this problem,
    but this function must be correct against either backend, so it applies
    the same `assume-naive-means-UTC` normalization
    `identity.repositories.users._aware_utc` already established, rather
    than assuming its caller always hands it an aware value.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return now >= expires_at


class ShareAction(str, Enum):
    """One discrete capability being requested against one share. Deliberately
    fine-grained, mirroring `agent.authz.Permission`'s own reasoning: a
    coarse "manage" bucket couldn't distinguish "may revoke" from "may only
    view," which is exactly the distinction this whole feature exists to
    enforce."""

    VIEW = "view"
    DOWNLOAD_ATTACHMENT = "download_attachment"
    UPDATE_SETTINGS = "update_settings"
    INVITE_MEMBER = "invite_member"
    REMOVE_MEMBER = "remove_member"
    REGENERATE_LINK = "regenerate_link"
    REVOKE = "revoke"


_OWNER_ONLY_ACTIONS: frozenset[ShareAction] = frozenset(
    {
        ShareAction.UPDATE_SETTINGS,
        ShareAction.INVITE_MEMBER,
        ShareAction.REMOVE_MEMBER,
        ShareAction.REGENERATE_LINK,
        ShareAction.REVOKE,
    }
)
_VIEWER_ACTIONS: frozenset[ShareAction] = frozenset(
    {ShareAction.VIEW, ShareAction.DOWNLOAD_ATTACHMENT}
)

#: The only member role this feature grants today -- see this module's own
#: docstring for why "Editor" is never modeled. A stored role outside this
#: set (a future migration bug, a hand-edited row, a role type this version
#: of the code has never heard of) is treated as `"unknown_role"`, denied,
#: never silently mapped to the nearest known role.
_KNOWN_MEMBER_ROLES: frozenset[str] = frozenset({"viewer"})


@dataclass(frozen=True)
class AuthorizationDecision:
    """The outcome of one `authorize_share_action` call.

    Attributes:
        allowed: Whether the action may proceed.
        reason: A stable machine code -- see this module's own docstring.
            Always present, on both an allow and a deny (an allow's reason
            names *which* role granted it: `"owner"`, `"member"`,
            `"public_link"`).
    """

    allowed: bool
    reason: str


def _allow(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=True, reason=reason)


def _deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(allowed=False, reason=reason)


def authorize_share_action(
    *,
    actor: User | None,
    actor_tenant_id: str | None,
    share: ConversationShare | None,
    action: ShareAction,
    conversation_deleted: bool,
    member: ShareMember | None = None,
    link: ShareLink | None = None,
    now: datetime,
) -> AuthorizationDecision:
    """The one function every share-touching code path must call.

    Args:
        actor: The authenticated caller, or `None` for an anonymous
            "anyone with the link" viewer.
        actor_tenant_id: `security.tenancy.resolve_actor_tenant_id(actor)`
            -- passed in rather than resolved internally so this module
            never has to import that resolver itself (keeps this file's own
            dependency surface at zero, per its docstring).
        share: The `ConversationShare` row being acted on, or `None` if
            none was found (a missing/wrong id) -- always denied, with a
            reason (`"share_not_found"`) that callers must **not** use to
            distinguish "wrong id" from "you're not allowed to see this
            one" in any user-facing response (see `api/shares.py`'s own
            "never confirm another user's conversation exists" convention).
        action: The capability being requested.
        conversation_deleted: Whether the parent `Conversation` is currently
            soft-deleted (`Conversation.deleted_at is not None`) --
            resolved independently of `share.revoked_at`, so a conversation
            deletion denies access even if nothing proactively revoked its
            share row (see `ConversationShare`'s own docstring).
        member: The actor's own `ShareMember` row for this share, if one
            exists. Irrelevant (and ignored) for an owner or an anonymous
            link viewer.
        link: The `ShareLink` row the caller presented, if any. Only
            relevant for `_VIEWER_ACTIONS`.
        now: Caller-supplied clock (never `datetime.now()` internally) --
            keeps every expiry comparison deterministically testable.

    Returns:
        An `AuthorizationDecision`. Never raises for a normal denial --
        only a genuine programming error (an unrecognized `ShareAction`
        value, which cannot happen through the public `ShareAction` enum)
        would ever reach the final `_deny("unsupported_action")` branch.
    """
    if share is None:
        return _deny("share_not_found")
    if conversation_deleted:
        return _deny("conversation_deleted")

    is_owner = actor is not None and share.owner_user_id == actor.id

    if action in _OWNER_ONLY_ACTIONS:
        if actor is None:
            return _deny("authentication_required")
        if not is_owner:
            return _deny("not_owner")
        if actor_tenant_id is None or actor_tenant_id != share.tenant_id:
            return _deny("cross_tenant")
        return _allow("owner")

    if action in _VIEWER_ACTIONS:
        if is_owner:
            # The owner may always preview their own share exactly as a
            # viewer would see it -- still tenant-checked, defense in depth,
            # even though `owner_user_id` alone already scopes this in
            # every real deployment today.
            if actor_tenant_id is None or actor_tenant_id != share.tenant_id:
                return _deny("cross_tenant")
            return _allow("owner")

        if share.revoked_at is not None:
            return _deny("share_revoked")
        if share.status != "active":
            return _deny("share_disabled")
        if _is_expired(share.expires_at, now):
            return _deny("share_expired")

        if link is not None:
            if link.revoked_at is not None:
                return _deny("link_revoked")
            if _is_expired(link.expires_at, now):
                return _deny("link_expired")

        if share.access_mode == "anyone_with_link":
            if link is None:
                return _deny("link_required")
            return _allow("public_link")

        # invite_only: a real, active, correctly-tenanted membership is the
        # only remaining way in -- a presented link (if any) is irrelevant
        # here, since this share was never configured to trust one.
        if actor is None:
            return _deny("authentication_required")
        if actor_tenant_id is None or actor_tenant_id != share.tenant_id:
            return _deny("cross_tenant")
        if member is None:
            return _deny("not_member")
        if member.role not in _KNOWN_MEMBER_ROLES:
            return _deny("unknown_role")
        if member.status != "active":
            return _deny("member_not_active")
        if member.revoked_at is not None:
            return _deny("member_revoked")
        if _is_expired(member.expires_at, now):
            return _deny("member_expired")
        return _allow("member")

    return _deny("unsupported_action")  # pragma: no cover - unreachable via ShareAction
