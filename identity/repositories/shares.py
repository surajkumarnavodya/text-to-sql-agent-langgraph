"""Repository layer for secure conversation sharing -- every state
transition (`ConversationShare`/`ShareMember`/`ShareLink`) and the
backend-only projection builder that turns a share into what a viewer is
actually allowed to see.

**This module never makes an authorization decision itself** -- every
function here is a plain data operation; `identity.share_policy
.authorize_share_action` is the only place a caller may or may not do
something is decided, and every route in `api/shares.py` calls that
*before* calling into this module for the corresponding read/write. The one
partial exception is `build_share_projection`, which does apply a
source/field allowlist (see its own docstring) -- that's data *filtering*,
not an access decision about whether the caller may see the share at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from identity.exceptions import (
    InvalidTokenError,
    ShareInvitationEmailMismatchError,
    ShareVersionConflictError,
    TokenAlreadyUsedError,
    TokenExpiredError,
)
from identity.models import (
    AiOutput,
    Conversation,
    ConversationShare,
    Prompt,
    ShareLink,
    ShareMember,
    User,
)
from identity.repositories.users import get_user_by_email, normalize_email
from identity.security import generate_refresh_token, hash_refresh_token


def _aware_utc(value: datetime | None) -> datetime | None:
    """See `identity.repositories.users._aware_utc`'s docstring -- same
    SQLite-vs-PostgreSQL timezone-round-tripping normalization, duplicated
    here rather than imported (a private helper of another module) to keep
    each repository file self-contained, the same small-deliberate-
    duplication precedent `attachments/pdf_processor.py` already
    establishes for its own OCR fallback."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _current_max_sequence(session: Session, conversation_id: uuid.UUID) -> int:
    prompt_max = session.scalar(
        select(func.max(Prompt.sequence_number)).where(Prompt.conversation_id == conversation_id)
    )
    output_max = session.scalar(
        select(func.max(AiOutput.sequence_number)).where(
            AiOutput.conversation_id == conversation_id
        )
    )
    return max(prompt_max or 0, output_max or 0)


# --- ConversationShare lifecycle -------------------------------------------------


def create_share(
    session: Session,
    *,
    conversation: Conversation,
    owner: User,
    tenant_id: str,
    access_mode: str = "invite_only",
    expiry_days: int,
) -> ConversationShare:
    """Creates the one `ConversationShare` row for `conversation` (there is
    never more than one -- see that model's own `unique=True` on
    `conversation_id`), capturing the snapshot boundary at the conversation's
    *current* max message sequence number. A message added after this call
    is invisible to any viewer until an explicit `update_share(...,
    refresh_snapshot=True)`.
    """
    now = datetime.now(UTC)
    share = ConversationShare(
        conversation_id=conversation.id,
        owner_user_id=owner.id,
        tenant_id=tenant_id,
        access_mode=access_mode,
        status="active",
        snapshot_message_sequence=_current_max_sequence(session, conversation.id),
        snapshot_captured_at=now,
        expires_at=now + timedelta(days=expiry_days),
        version=1,
    )
    session.add(share)
    session.commit()
    session.refresh(share)
    return share


def get_share_by_conversation_for_owner(
    session: Session, *, conversation_id: uuid.UUID, owner_user_id: uuid.UUID
) -> ConversationShare | None:
    """Owner-scoped lookup -- `owner_user_id` is folded into the query
    itself (never checked after an unscoped fetch), the same
    "a wrong/forged id reads as not-found, never as found-but-forbidden"
    principle `identity.repositories.history.get_conversation` already
    applies."""
    return session.scalar(
        select(ConversationShare).where(
            ConversationShare.conversation_id == conversation_id,
            ConversationShare.owner_user_id == owner_user_id,
        )
    )


def get_share_by_conversation_id(
    session: Session, *, conversation_id: uuid.UUID
) -> ConversationShare | None:
    """Unscoped-by-owner lookup (unlike `get_share_by_conversation_for_owner`)
    -- used by every owner-management route in `api/shares.py` alongside an
    explicit `identity.share_policy.authorize_share_action` call, so the
    *policy engine*, not this query's own `WHERE` clause, is what decides
    whether the caller may act on the row it returns (see this module's own
    docstring: never trust an object id, or a query filter alone, as
    authorization)."""
    return session.scalar(
        select(ConversationShare).where(ConversationShare.conversation_id == conversation_id)
    )


def get_share_by_id(session: Session, *, share_id: uuid.UUID) -> ConversationShare | None:
    """Unscoped lookup by primary key -- callers (`api/shares.py`) must
    always run the result through `identity.share_policy
    .authorize_share_action` before using it for anything; this function
    itself makes no ownership/membership decision."""
    return session.get(ConversationShare, share_id)


def update_share(
    session: Session,
    *,
    share: ConversationShare,
    expected_version: int,
    access_mode: str | None = None,
    expiry_days: int | None = None,
    status: str | None = None,
    refresh_snapshot: bool = False,
    conversation: Conversation | None = None,
) -> ConversationShare:
    """Applies whichever of the optional fields is not `None`, under
    optimistic concurrency control.

    Raises:
        ShareVersionConflictError: `expected_version` no longer matches
            `share.version` -- a concurrent update (another browser tab,
            another owner session) landed first between this caller's own
            read and this write.
    """
    if share.version != expected_version:
        raise ShareVersionConflictError(
            f"Share {share.id} is at version {share.version}, caller expected {expected_version}."
        )

    if access_mode is not None:
        share.access_mode = access_mode
        if access_mode != "anyone_with_link":
            # Switching away from a public link immediately revokes every
            # currently-active link for this share -- a stale bearer token
            # must not keep working just because the owner flipped the
            # setting back rather than clicking "revoke" explicitly.
            now = datetime.now(UTC)
            for link in session.scalars(
                select(ShareLink).where(
                    ShareLink.share_id == share.id, ShareLink.revoked_at.is_(None)
                )
            ).all():
                link.revoked_at = now
    if expiry_days is not None:
        share.expires_at = datetime.now(UTC) + timedelta(days=expiry_days)
    if status is not None:
        share.status = status
    if refresh_snapshot:
        if conversation is None or conversation.id != share.conversation_id:
            raise ValueError("refresh_snapshot=True requires the matching Conversation instance.")
        share.snapshot_message_sequence = _current_max_sequence(session, conversation.id)
        share.snapshot_captured_at = datetime.now(UTC)

    share.version += 1
    session.commit()
    session.refresh(share)
    return share


def reactivate_share(
    session: Session,
    *,
    share: ConversationShare,
    conversation: Conversation,
    access_mode: str,
    expiry_days: int,
) -> ConversationShare:
    """Re-shares a conversation whose `ConversationShare` row was previously
    revoked -- **the only function that ever clears `revoked_at`**, and only
    ever called from `POST .../share`'s own idempotent-create path (never
    from `PATCH .../share`, whose public `UpdateShareRequest` schema has no
    way to express "un-revoke" at all).

    Without this, `create_share_route`'s idempotent "a share already exists,
    return it" behavior would keep resurfacing a dead, permanently-revoked
    row forever -- `ConversationShare.conversation_id`'s own `unique=True`
    means there is nowhere else for a fresh share to live, and
    `identity.share_policy.authorize_share_action` treats a non-`None`
    `revoked_at` as an unconditional, permanent viewer denial regardless of
    `status` (see that module's own docstring) -- so simply flipping
    `status` back to `"active"` via `update_share` alone could never make
    the conversation viewable again either. Re-sharing is therefore
    logically a fresh grant: it also recomputes the snapshot boundary at
    the conversation's *current* state, exactly like `create_share` does
    for a genuinely new share, rather than resurrecting whatever stale
    snapshot the revoked row happened to have.
    """
    now = datetime.now(UTC)
    share.revoked_at = None  # type: ignore[assignment]
    share.status = "active"
    share.access_mode = access_mode
    share.snapshot_message_sequence = _current_max_sequence(session, conversation.id)
    share.snapshot_captured_at = now
    share.expires_at = now + timedelta(days=expiry_days)
    share.version += 1
    session.commit()
    session.refresh(share)
    return share


def revoke_share(
    session: Session, *, share: ConversationShare, expected_version: int
) -> ConversationShare:
    """Turns sharing off entirely -- `status="disabled"` plus `revoked_at`,
    and revokes every currently-active link (defense in depth: `status`
    alone already denies every viewer action via `identity.share_policy`,
    but a superseded link's own `revoked_at` means a downstream cache or a
    delayed request sees the same denial through a second, independent
    check)."""
    if share.version != expected_version:
        raise ShareVersionConflictError(
            f"Share {share.id} is at version {share.version}, caller expected {expected_version}."
        )
    now = datetime.now(UTC)
    share.status = "disabled"
    share.revoked_at = now
    for link in session.scalars(
        select(ShareLink).where(ShareLink.share_id == share.id, ShareLink.revoked_at.is_(None))
    ).all():
        link.revoked_at = now
    share.version += 1
    session.commit()
    session.refresh(share)
    return share


# --- ShareLink lifecycle -----------------------------------------------------


def regenerate_link(session: Session, *, share: ConversationShare) -> tuple[ShareLink, str]:
    """Revokes every currently-active link for `share` and issues a brand
    new one, returning `(link_row, raw_token)`.

    **`raw_token` must only ever be handed back in the owner's own
    synchronous API response** -- never logged, never stored (only its
    SHA-256 hash is persisted, via `identity.security.hash_refresh_token`,
    reused directly rather than reinvented -- see that function's own
    docstring for why a fast hash, not Argon2id, is the right tool for an
    already-high-entropy opaque token), and never included in any
    `ShareAuditEvent.safe_metadata`.
    """
    now = datetime.now(UTC)
    existing = session.scalars(
        select(ShareLink).where(ShareLink.share_id == share.id, ShareLink.revoked_at.is_(None))
    ).all()
    next_version = 1
    for link in existing:
        link.revoked_at = now
        next_version = max(next_version, link.token_version + 1)

    raw_token = generate_refresh_token()
    link = ShareLink(
        share_id=share.id,
        token_hash=hash_refresh_token(raw_token),
        token_version=next_version,
        expires_at=share.expires_at,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link, raw_token


def resolve_link_by_raw_token(
    session: Session, *, raw_token: str
) -> tuple[ConversationShare, ShareLink] | None:
    """Looks up the `(share, link)` pair for a presented bearer token, or
    `None` if no `ShareLink` row matches its hash at all -- an indexed
    equality lookup on a stored hash, the identical shape
    `identity.repositories.tokens.redeem_password_reset_token` already uses
    for its own opaque tokens (no separate constant-time-compare loop is
    needed here: the token itself is the high-entropy secret, and looking
    it up by its own hash in an index is the standard, accepted approach
    for this class of design -- see `ShareLink`'s own docstring).

    Does **not** itself check `expires_at`/`revoked_at` -- that's
    `identity.share_policy.authorize_share_action`'s job, given this same
    `link` object, so there is exactly one place expiry/revocation logic
    lives for both the owner-facing and the token-resolution path.
    """
    token_hash = hash_refresh_token(raw_token)
    link = session.scalar(select(ShareLink).where(ShareLink.token_hash == token_hash))
    if link is None:
        return None
    share = session.get(ConversationShare, link.share_id)
    if share is None:
        return None
    return share, link


def record_link_used(session: Session, *, link: ShareLink) -> None:
    link.last_used_at = datetime.now(UTC)
    session.commit()


# --- ShareMember / invitations ------------------------------------------------


def list_members(session: Session, *, share: ConversationShare) -> list[ShareMember]:
    return list(
        session.scalars(
            select(ShareMember)
            .where(ShareMember.share_id == share.id)
            .order_by(ShareMember.created_at.asc())
        ).all()
    )


def count_active_or_pending_members(session: Session, *, share: ConversationShare) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(ShareMember)
            .where(
                ShareMember.share_id == share.id,
                ShareMember.status.in_(("pending", "active")),
            )
        )
        or 0
    )


def invite_member(
    session: Session, *, share: ConversationShare, invited_email: str, expiry_days: int
) -> tuple[ShareMember, str]:
    """Invites `invited_email` to view `share`, returning `(member_row,
    raw_invitation_token)`.

    Never reveals whether `invited_email` already has an account -- the
    email is matched to an existing `User` immediately (so the owner's own
    "people with access" list can show a real name once accepted), but the
    *response shape* to this call is identical whether a match was found or
    not, and the eventual accept step (`accept_invitation`) is what
    actually verifies the accepting caller controls that address (see this
    module's own docstring and `ShareInvitationEmailMismatchError`).

    Idempotent for a repeat invite to the same still-pending/active email:
    rotates a fresh token (extending its expiry) on the *existing* row
    rather than creating a duplicate member entry.
    """
    normalized = normalize_email(invited_email)
    now = datetime.now(UTC)
    raw_token = generate_refresh_token()
    token_hash = hash_refresh_token(raw_token)
    expires_at = now + timedelta(days=expiry_days)

    existing = session.scalar(
        select(ShareMember).where(
            ShareMember.share_id == share.id,
            ShareMember.invited_email == normalized,
            ShareMember.status.in_(("pending", "active")),
        )
    )
    if existing is not None:
        existing.invitation_token_hash = token_hash
        existing.invitation_expires_at = expires_at
        session.commit()
        session.refresh(existing)
        return existing, raw_token

    matched_user = get_user_by_email(session, normalized)
    member = ShareMember(
        share_id=share.id,
        user_id=matched_user.id if matched_user is not None else None,
        invited_email=normalized,
        role="viewer",
        status="pending",
        invitation_token_hash=token_hash,
        invitation_expires_at=expires_at,
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    return member, raw_token


def get_member_by_id(
    session: Session, *, share_id: uuid.UUID, member_id: uuid.UUID
) -> ShareMember | None:
    return session.scalar(
        select(ShareMember).where(ShareMember.id == member_id, ShareMember.share_id == share_id)
    )


def get_member_for_user(
    session: Session, *, share_id: uuid.UUID, user_id: uuid.UUID
) -> ShareMember | None:
    return session.scalar(
        select(ShareMember).where(ShareMember.share_id == share_id, ShareMember.user_id == user_id)
    )


def remove_member(session: Session, *, member: ShareMember) -> None:
    member.status = "revoked"
    member.revoked_at = datetime.now(UTC)
    session.commit()


def accept_invitation(session: Session, *, raw_token: str, accepting_user: User) -> ShareMember:
    """Redeems a pending invitation for `accepting_user`, flipping it to
    `"active"`.

    Raises:
        InvalidTokenError: no pending invitation matches this token.
        TokenExpiredError: the invitation existed but is past
            `invitation_expires_at`.
        TokenAlreadyUsedError: the invitation was already accepted (or
            revoked) once.
        ShareInvitationEmailMismatchError: the token is real and live, but
            `accepting_user`'s own email does not match the address it was
            issued to (or, if the invitation had already been matched to a
            *different* account at invite time, that account doesn't match
            the caller) -- see that exception's own docstring.
    """
    token_hash = hash_refresh_token(raw_token)
    member = session.scalar(
        select(ShareMember).where(ShareMember.invitation_token_hash == token_hash)
    )
    if member is None:
        raise InvalidTokenError("No share invitation matches this value.")
    if member.status == "active":
        raise TokenAlreadyUsedError(f"Share invitation {member.id} was already accepted.")
    if member.status != "pending":
        raise InvalidTokenError("This share invitation is no longer valid.")
    expires_at = _aware_utc(member.invitation_expires_at)
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise TokenExpiredError(f"Share invitation {member.id} has expired.")

    expected_user_id = member.user_id
    if expected_user_id is not None:
        if expected_user_id != accepting_user.id:
            raise ShareInvitationEmailMismatchError()
    elif (
        member.invited_email is not None
        and normalize_email(accepting_user.email) != member.invited_email
    ):
        raise ShareInvitationEmailMismatchError()

    member.user_id = accepting_user.id
    member.status = "active"
    member.accepted_at = datetime.now(UTC)
    session.commit()
    session.refresh(member)
    return member


# --- Projection: the only thing a viewer is ever shown ------------------------

#: `AiOutput.metadata_json` keys that are safe to surface in a shared,
#: read-only projection -- see `api/chat_persistence.py::_build_history_metadata`
#: for the full shape this is filtering. Everything NOT in this list is
#: internal operational detail (retry counts, query plans, schema-table
#: hints, raw rejection/failure text, rate-limit/clarification prompts) --
#: excluded categorically, never selectively redacted field-by-field, so a
#: newly-added metadata field defaults to *not* appearing in a share until
#: someone deliberately adds it here.
_PROJECTED_METADATA_FIELDS: tuple[str, ...] = (
    "sources_used",
    "database",
    "model",
    "sql",
    "row_count",
    "insight",
    "synthesized_answer",
    "document_result",
    "policy_result",
    "web_result",
    "generation_result",
    "media_search_result",
    "attachment_refs",
    "result_snapshot",
)


@dataclass(frozen=True)
class ProjectedTurn:
    """One message, exactly as a shared-view visitor may see it -- see
    `_PROJECTED_METADATA_FIELDS` for what an assistant turn's `extra` dict
    is allowed to contain. `content` is always this app's own pre-existing
    safe display/search text (`identity.repositories.history.MessageRow
    .content`, itself already `_answer_text_for_history`'s redacted,
    user-facing string) -- never raw agent state, a stack trace, or a tool
    trace, since that text was already safe enough for this app's own
    OWNER-facing reloaded-conversation view before sharing ever existed.
    """

    sequence_number: int
    role: str
    content: str
    created_at: datetime
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ShareProjection:
    """The full backend-computed, already-filtered payload
    `GET /share-view/{ref}` serializes -- nothing downstream of this needs to
    apply any further redaction. `approved_attachment_ids` is the hard
    allowlist `GET /share-view/{ref}/attachments/{id}` checks membership
    against before ever touching `attachments.store` -- an attachment id
    that isn't in this set is treated exactly like one that doesn't exist,
    regardless of whether the caller can otherwise view this share."""

    conversation_title: str | None
    feature_type: str
    snapshot_captured_at: datetime | None
    turns: list[ProjectedTurn]
    approved_attachment_ids: frozenset[str]


def build_share_projection(session: Session, *, share: ConversationShare) -> ShareProjection:
    """Builds the read-only projection for an already-authorized viewer.

    **Callers must call `identity.share_policy.authorize_share_action`
    first** -- this function performs no authorization check of its own
    (see this module's own docstring); it only ever bounds *what a message
    within the snapshot may disclose*, given that the caller is already
    allowed to see the share at all.

    Never invokes the SQL pipeline, a tool, a model, or any retrieval
    call -- every field returned here is a plain read of already-persisted,
    already-redacted `Prompt`/`AiOutput` rows (see `api/chat_persistence
    .py`'s own docstring for why `AiOutput.metadata_json` was already safe
    to show the *owner*; this function narrows that further for a
    *third-party viewer*, per `_PROJECTED_METADATA_FIELDS`).
    """
    conversation = session.get(Conversation, share.conversation_id)
    prompts = session.scalars(
        select(Prompt).where(
            Prompt.conversation_id == share.conversation_id,
            Prompt.sequence_number <= share.snapshot_message_sequence,
            Prompt.deleted_at.is_(None),
        )
    ).all()
    outputs = session.scalars(
        select(AiOutput).where(
            AiOutput.conversation_id == share.conversation_id,
            AiOutput.sequence_number <= share.snapshot_message_sequence,
        )
    ).all()

    turns: list[ProjectedTurn] = [
        ProjectedTurn(
            sequence_number=p.sequence_number,
            role="user",
            content=p.final_content,
            created_at=p.created_at,
        )
        for p in prompts
    ]
    approved_attachment_ids: set[str] = set()
    for output in outputs:
        raw_metadata = output.metadata_json or {}
        extra = {
            key: raw_metadata[key] for key in _PROJECTED_METADATA_FIELDS if key in raw_metadata
        }
        for ref in extra.get("attachment_refs") or []:
            attachment_id = ref.get("attachment_id") if isinstance(ref, dict) else None
            if attachment_id:
                approved_attachment_ids.add(attachment_id)
        turns.append(
            ProjectedTurn(
                sequence_number=output.sequence_number,
                role="assistant",
                content=output.content,
                created_at=output.created_at,
                extra=extra,
            )
        )
    turns.sort(key=lambda t: t.sequence_number)

    return ShareProjection(
        conversation_title=conversation.title if conversation is not None else None,
        feature_type=conversation.feature_type if conversation is not None else "chat",
        snapshot_captured_at=share.snapshot_captured_at,
        turns=turns,
        approved_attachment_ids=frozenset(approved_attachment_ids),
    )
