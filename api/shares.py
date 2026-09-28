"""Secure conversation sharing -- the full REST surface for creating,
managing, and viewing a read-only snapshot of a conversation.

**Every route here does exactly one authorization dance**: resolve the
`ConversationShare` row by id (never trusting a query filter alone to
scope it -- see `identity.repositories.shares.get_share_by_conversation_id`'s
own docstring), then call `identity.share_policy.authorize_share_action`
and act only on an `allowed=True` decision. No route re-derives its own
ad hoc ownership/membership logic.

A shared conversation is a **read-only projection, never a live query
channel**: nothing in this file ever calls into `agent.graph`,
`agent.orchestrator`, `rag/`, `search/`, `media_gen/`, or any embedding/
retrieval code. `GET /share-view/{ref}` only ever reads already-persisted,
already-redacted `Prompt`/`AiOutput` rows through
`identity.repositories.shares.build_share_projection`.

**Anonymous-reachable routes never confirm whether a share exists.**
`GET /share-view/{ref}` and its attachment sibling collapse every denial reason
(not-found, expired, revoked, wrong tenant, not a member) into the same
generic 404 -- only a 429 (rate-limited) is ever distinguishable, since
that alone reveals nothing about the target's existence.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from identity.exceptions import (
    InvalidTokenError,
    ShareInvitationEmailMismatchError,
    ShareVersionConflictError,
    TokenAlreadyUsedError,
    TokenExpiredError,
)
from identity.models import Conversation, User
from identity.rbac import Permission as IdentityPermission
from identity.repositories.history import get_conversation
from identity.repositories.shares import (
    ProjectedTurn,
    accept_invitation,
    build_share_projection,
    count_active_or_pending_members,
    create_share,
    get_member_by_id,
    get_member_for_user,
    get_share_by_conversation_for_owner,
    get_share_by_conversation_id,
    get_share_by_id,
    invite_member,
    list_members,
    reactivate_share,
    record_link_used,
    regenerate_link,
    remove_member,
    resolve_link_by_raw_token,
    revoke_share,
    update_share,
)
from identity.repositories.users import get_user_by_id
from identity.security import looks_like_local_token, validate_local_token
from identity.share_audit import record_share_event
from identity.share_policy import ShareAction, authorize_share_action
from identity.share_schemas import (
    AcceptInvitationResponse,
    CreateShareRequest,
    InviteMemberRequest,
    InviteMemberResponse,
    ProjectedTurnOut,
    RevokeShareRequest,
    SharedConversationOut,
    ShareLinkOut,
    ShareMemberOut,
    ShareOut,
    ShareResponse,
    UpdateShareRequest,
)
from sqlalchemy.orm import Session

from agent.rate_limit import get_share_invite_limiter, get_share_link_access_limiter
from api.identity_authz import get_identity_db, require_identity_permission, require_local_user
from config.settings import Settings, get_settings
from security.client_ip import resolve_client_ip
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)
router = APIRouter()

_BEARER_PREFIX = "Bearer "

#: Every denial from `identity.share_policy` maps to this one message on
#: the anonymous/link-reachable surface -- see this module's own docstring.
_GENERIC_SHARE_UNAVAILABLE = "This shared conversation is unavailable."
_PRIVATE_NO_STORE = "private, no-store"


#: **Real bug, found only by live-testing the actual running app, not by
#: `TestClient` alone**: `response.headers[...] = ...` set on the
#: dependency-injected `Response` object only ever reaches the client when
#: the route *returns normally* -- raising `HTTPException` makes FastAPI
#: build a completely separate response object, silently discarding
#: whatever this function already wrote to `response.headers`. Every
#: `HTTPException` this module raises therefore carries its own `headers=`
#: explicitly (this constant), rather than relying on the per-route
#: `response.headers[...] =` lines above ever applying to an error path --
#: precisely the security-critical path (a denial) for an anonymous-facing
#: endpoint, where a stale/default cache/referrer header would matter most.
_SAFE_ERROR_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Conversation not found.",
        headers=_SAFE_ERROR_HEADERS,
    )


def _no_share_yet() -> HTTPException:
    # Only ever raised after the caller has already proven ownership of the
    # conversation itself (via `get_conversation(user_id=...)`), so
    # confirming "no share exists yet" here carries no enumeration risk --
    # see api/chat_history.py's own `_not_found` for the contrasting case
    # where ownership has *not* yet been established.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No share exists for this conversation yet.",
        headers=_SAFE_ERROR_HEADERS,
    )


def _share_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=_GENERIC_SHARE_UNAVAILABLE,
        headers=_SAFE_ERROR_HEADERS,
    )


def _rate_limited() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Please try again later.",
        headers={**_SAFE_ERROR_HEADERS, "Retry-After": "60"},
    )


def _parse_uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _member_out(session: Session, member) -> ShareMemberOut:  # noqa: ANN001
    display_name = None
    if member.user_id is not None:
        user = get_user_by_id(session, member.user_id)
        if user is not None:
            display_name = user.display_name
    return ShareMemberOut(
        id=member.id,
        user_id=member.user_id,
        invited_email=member.invited_email,
        display_name=display_name,
        role=member.role,
        status=member.status,
        expires_at=member.expires_at,
        accepted_at=member.accepted_at,
        revoked_at=member.revoked_at,
        created_at=member.created_at,
    )


def _share_out(session: Session, share) -> ShareOut:  # noqa: ANN001
    members = list_members(session, share=share)
    has_active_link = any(link.revoked_at is None for link in share.links)
    return ShareOut(
        id=share.id,
        conversation_id=share.conversation_id,
        access_mode=share.access_mode,
        default_permission=share.default_permission,
        status=share.status,
        snapshot_message_sequence=share.snapshot_message_sequence,
        snapshot_captured_at=share.snapshot_captured_at,
        expires_at=share.expires_at,
        revoked_at=share.revoked_at,
        version=share.version,
        created_at=share.created_at,
        updated_at=share.updated_at,
        members=[_member_out(session, m) for m in members],
        link_available=has_active_link,
        member_view_path=f"/shared/{share.id}",
    )


def _projected_turn_out(turn: ProjectedTurn) -> ProjectedTurnOut:
    extra = turn.extra
    return ProjectedTurnOut(
        sequence_number=turn.sequence_number,
        role=turn.role,  # type: ignore[arg-type]
        content=turn.content,
        created_at=turn.created_at,
        sources_used=list(extra.get("sources_used") or []),
        database=extra.get("database"),
        model=extra.get("model"),
        sql=extra.get("sql"),
        row_count=extra.get("row_count"),
        insight=extra.get("insight"),
        synthesized_answer=extra.get("synthesized_answer"),
        document_result=extra.get("document_result"),
        policy_result=extra.get("policy_result"),
        web_result=extra.get("web_result"),
        generation_result=extra.get("generation_result"),
        media_search_result=extra.get("media_search_result"),
        attachment_refs=list(extra.get("attachment_refs") or []),
        result_snapshot=extra.get("result_snapshot"),
    )


def _resolve_optional_local_actor(
    session: Session, authorization: str | None, settings: Settings
) -> User | None:
    """Best-effort, non-raising identity resolution for the anonymous-
    reachable `/share-view/{ref}` surface -- an absent, malformed, or invalid
    token simply resolves to `None` (anonymous), never a 401, since a valid
    "anyone with the link" visitor is expected to have no session at all.
    Only ever resolves a *locally*-issued token (this feature's own
    conversations/sharing are local-auth-only, matching
    `api/chat_history.py`'s identical scope) -- an OIDC/static-token bearer
    is treated as anonymous here, not rejected.
    """
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        return None
    token = authorization[len(_BEARER_PREFIX) :]
    if not looks_like_local_token(token, settings):
        return None
    try:
        claims = validate_local_token(token, settings)
    except Exception:  # noqa: BLE001 - any validation failure -> anonymous, never a 401 here
        return None
    parsed = _parse_uuid_or_none(claims.subject)
    if parsed is None:
        return None
    return get_user_by_id(session, parsed)


def _conversation_deleted(session: Session, share) -> bool:  # noqa: ANN001
    conversation = session.get(Conversation, share.conversation_id)
    return conversation is None or conversation.deleted_at is not None


# --- Owner-management routes --------------------------------------------------


@router.post(
    "/conversations/{conversation_id}/share",
    response_model=ShareResponse,
    status_code=status.HTTP_200_OK,
)
def create_share_route(
    conversation_id: str,
    payload: CreateShareRequest,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.SHARES_CREATE_OWN)
    ),
) -> ShareResponse:
    """Idempotent: a conversation already has at most one share row (see
    `ConversationShare`'s own `unique=True` on `conversation_id`) -- calling
    this again simply returns the existing one rather than erroring or
    creating a duplicate."""
    # A plain call, not `Depends(get_settings)` -- FastAPI resolves a
    # `Depends(...)` default's callable *once*, at route-definition time,
    # which is exactly what makes it untestable against a monkeypatched
    # `get_settings` reference. Every other settings-dependent route in
    # this codebase (`api/chat_history.py`) already calls this as a plain
    # statement for the same reason.
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    if not settings.enable_conversation_sharing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    user, session = user_and_session
    parsed_id = _parse_uuid_or_none(conversation_id)
    if parsed_id is None:
        raise _not_found()
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    if conversation is None:
        raise _not_found()

    if payload.access_mode == "anyone_with_link" and not settings.share_public_links_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Public link sharing is not enabled on this server.",
        )

    existing = get_share_by_conversation_for_owner(
        session, conversation_id=parsed_id, owner_user_id=user.id
    )
    reactivated = False
    if existing is not None and existing.revoked_at is None:
        return ShareResponse(share=_share_out(session, existing))
    if existing is not None:
        # A previously-revoked share for this conversation -- re-sharing is
        # a fresh grant, not a resurrection of stale settings (see
        # `identity.repositories.shares.reactivate_share`'s own docstring
        # for why simply flipping `status` back could never be enough).
        share = reactivate_share(
            session,
            share=existing,
            conversation=conversation,
            access_mode=payload.access_mode,
            expiry_days=payload.expiry_days or settings.share_default_expiry_days,
        )
        reactivated = True
    else:
        tenant_id = resolve_actor_tenant_id(user)
        if tenant_id is None:
            # Cannot happen for an authenticated `User` (see
            # `security.tenancy.resolve_actor_tenant_id`'s own docstring --
            # `None` is only ever returned for an anonymous caller) -- a
            # real `raise`, not a bare `assert`, so this stays a hard
            # failure even under `-O` optimized bytecode.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not resolve a tenant for this account.",
            )
        share = create_share(
            session,
            conversation=conversation,
            owner=user,
            tenant_id=tenant_id,
            access_mode=payload.access_mode,
            expiry_days=payload.expiry_days or settings.share_default_expiry_days,
        )

    link_out: ShareLinkOut | None = None
    if share.access_mode == "anyone_with_link":
        link, raw_token = regenerate_link(session, share=share)
        link_out = ShareLinkOut(view_path=f"/shared/{raw_token}", expires_at=link.expires_at)

    record_share_event(
        session,
        share_id=share.id,
        conversation_id=conversation.id,
        actor_user_id=user.id,
        event_type="share_created",
        result="allowed",
        safe_metadata={
            "access_mode": share.access_mode,
            "has_link": link_out is not None,
            "reactivated": reactivated,
        },
    )
    return ShareResponse(share=_share_out(session, share), link=link_out)


@router.get("/conversations/{conversation_id}/share", response_model=ShareOut)
def get_share_route(
    conversation_id: str,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> ShareOut:
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    parsed_id = _parse_uuid_or_none(conversation_id)
    if parsed_id is None:
        raise _not_found()
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    if conversation is None:
        raise _not_found()
    share = get_share_by_conversation_for_owner(
        session, conversation_id=parsed_id, owner_user_id=user.id
    )
    if share is None:
        raise _no_share_yet()
    return _share_out(session, share)


@router.patch("/conversations/{conversation_id}/share", response_model=ShareOut)
def update_share_route(
    conversation_id: str,
    payload: UpdateShareRequest,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.SHARES_MANAGE_OWN)
    ),
) -> ShareOut:
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    parsed_id = _parse_uuid_or_none(conversation_id)
    if parsed_id is None:
        raise _not_found()
    share = get_share_by_conversation_id(session, conversation_id=parsed_id)
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    decision = authorize_share_action(
        actor=user,
        actor_tenant_id=resolve_actor_tenant_id(user),
        share=share,
        action=ShareAction.UPDATE_SETTINGS,
        conversation_deleted=conversation is None,
        now=datetime.now(UTC),
    )
    if not decision.allowed or share is None:
        record_share_event(
            session,
            share_id=share.id if share else None,
            conversation_id=parsed_id,
            actor_user_id=user.id,
            event_type="idor_attempt" if share is not None else "view_denied",
            result="denied",
            reason=decision.reason,
        )
        raise _not_found()

    if payload.access_mode == "anyone_with_link" and not settings.share_public_links_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Public link sharing is not enabled on this server.",
        )

    try:
        updated = update_share(
            session,
            share=share,
            expected_version=payload.version,
            access_mode=payload.access_mode,
            expiry_days=payload.expiry_days,
            status=payload.status,
            refresh_snapshot=payload.refresh_snapshot,
            conversation=conversation,
        )
    except ShareVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message) from exc

    record_share_event(
        session,
        share_id=updated.id,
        conversation_id=updated.conversation_id,
        actor_user_id=user.id,
        event_type="share_updated",
        result="allowed",
        safe_metadata={
            "access_mode": updated.access_mode,
            "status": updated.status,
            "refreshed_snapshot": payload.refresh_snapshot,
        },
    )
    return _share_out(session, updated)


@router.post("/conversations/{conversation_id}/share/revoke", response_model=ShareOut)
def revoke_share_route(
    conversation_id: str,
    payload: RevokeShareRequest,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.SHARES_MANAGE_OWN)
    ),
) -> ShareOut:
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    parsed_id = _parse_uuid_or_none(conversation_id)
    if parsed_id is None:
        raise _not_found()
    share = get_share_by_conversation_id(session, conversation_id=parsed_id)
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    decision = authorize_share_action(
        actor=user,
        actor_tenant_id=resolve_actor_tenant_id(user),
        share=share,
        action=ShareAction.REVOKE,
        conversation_deleted=conversation is None,
        now=datetime.now(UTC),
    )
    if not decision.allowed or share is None:
        raise _not_found()

    try:
        revoked = revoke_share(session, share=share, expected_version=payload.version)
    except ShareVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.safe_message) from exc

    record_share_event(
        session,
        share_id=revoked.id,
        conversation_id=revoked.conversation_id,
        actor_user_id=user.id,
        event_type="share_revoked",
        result="allowed",
    )
    return _share_out(session, revoked)


@router.post("/conversations/{conversation_id}/share/link/regenerate", response_model=ShareResponse)
def regenerate_link_route(
    conversation_id: str,
    request: Request,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.SHARES_MANAGE_OWN)
    ),
) -> ShareResponse:
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    limiter = get_share_invite_limiter(str(user.id), settings.share_invite_rate_limit_per_hour)
    if not limiter.check().allowed:
        raise _rate_limited()

    parsed_id = _parse_uuid_or_none(conversation_id)
    if parsed_id is None:
        raise _not_found()
    share = get_share_by_conversation_id(session, conversation_id=parsed_id)
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    decision = authorize_share_action(
        actor=user,
        actor_tenant_id=resolve_actor_tenant_id(user),
        share=share,
        action=ShareAction.REGENERATE_LINK,
        conversation_deleted=conversation is None,
        now=datetime.now(UTC),
    )
    if not decision.allowed or share is None:
        raise _not_found()
    if share.access_mode != "anyone_with_link":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enable 'anyone with the link' access before regenerating a link.",
        )

    link, raw_token = regenerate_link(session, share=share)
    record_share_event(
        session,
        share_id=share.id,
        conversation_id=share.conversation_id,
        actor_user_id=user.id,
        event_type="link_regenerated",
        result="allowed",
        safe_metadata={"token_version": link.token_version},
    )
    return ShareResponse(
        share=_share_out(session, share),
        link=ShareLinkOut(view_path=f"/shared/{raw_token}", expires_at=link.expires_at),
    )


@router.post(
    "/conversations/{conversation_id}/share/members/invite", response_model=InviteMemberResponse
)
def invite_member_route(
    conversation_id: str,
    payload: InviteMemberRequest,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.SHARES_MANAGE_OWN)
    ),
) -> InviteMemberResponse:
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    limiter = get_share_invite_limiter(str(user.id), settings.share_invite_rate_limit_per_hour)
    if not limiter.check().allowed:
        raise _rate_limited()

    parsed_id = _parse_uuid_or_none(conversation_id)
    if parsed_id is None:
        raise _not_found()
    share = get_share_by_conversation_id(session, conversation_id=parsed_id)
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    decision = authorize_share_action(
        actor=user,
        actor_tenant_id=resolve_actor_tenant_id(user),
        share=share,
        action=ShareAction.INVITE_MEMBER,
        conversation_deleted=conversation is None,
        now=datetime.now(UTC),
    )
    if not decision.allowed or share is None:
        raise _not_found()

    if (
        count_active_or_pending_members(session, share=share)
        >= settings.share_max_members_per_conversation
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This conversation has reached its maximum number of shared members.",
        )

    member, raw_token = invite_member(
        session,
        share=share,
        invited_email=payload.email,
        expiry_days=payload.expiry_days or settings.share_invitation_expiry_days,
    )
    record_share_event(
        session,
        share_id=share.id,
        conversation_id=share.conversation_id,
        actor_user_id=user.id,
        event_type="member_invited",
        result="allowed",
        safe_metadata={"member_status": member.status},
    )
    invitation_path = None if member.status == "active" else f"/accept-invitation/{raw_token}"
    return InviteMemberResponse(
        member=_member_out(session, member), invitation_path=invitation_path
    )


@router.delete(
    "/conversations/{conversation_id}/share/members/{member_id}", response_model=ShareOut
)
def remove_member_route(
    conversation_id: str,
    member_id: str,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(
        require_identity_permission(IdentityPermission.SHARES_MANAGE_OWN)
    ),
) -> ShareOut:
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    parsed_id = _parse_uuid_or_none(conversation_id)
    parsed_member_id = _parse_uuid_or_none(member_id)
    if parsed_id is None or parsed_member_id is None:
        raise _not_found()
    share = get_share_by_conversation_id(session, conversation_id=parsed_id)
    conversation = get_conversation(session, conversation_id=parsed_id, user_id=user.id)
    decision = authorize_share_action(
        actor=user,
        actor_tenant_id=resolve_actor_tenant_id(user),
        share=share,
        action=ShareAction.REMOVE_MEMBER,
        conversation_deleted=conversation is None,
        now=datetime.now(UTC),
    )
    if not decision.allowed or share is None:
        raise _not_found()

    member = get_member_by_id(session, share_id=share.id, member_id=parsed_member_id)
    if member is None:
        # A wrong/foreign member id under a share the caller does own --
        # still a plain 404, never confirming whether that id exists
        # elsewhere (e.g. under a *different* share).
        raise _not_found()

    remove_member(session, member=member)
    record_share_event(
        session,
        share_id=share.id,
        conversation_id=share.conversation_id,
        actor_user_id=user.id,
        event_type="member_removed",
        result="allowed",
        safe_metadata={"member_id": str(member.id)},
    )
    return _share_out(session, share)


# --- Invitation acceptance -----------------------------------------------------


@router.post("/share-invitations/{opaque_token}/accept", response_model=AcceptInvitationResponse)
def accept_invitation_route(
    opaque_token: str,
    response: Response,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> AcceptInvitationResponse:
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    user, session = user_and_session
    limiter = get_share_invite_limiter(str(user.id), settings.share_invite_rate_limit_per_hour)
    if not limiter.check().allowed:
        raise _rate_limited()

    try:
        member = accept_invitation(session, raw_token=opaque_token, accepting_user=user)
    except (InvalidTokenError, TokenExpiredError, TokenAlreadyUsedError) as exc:
        record_share_event(
            session,
            share_id=None,
            conversation_id=None,
            actor_user_id=user.id,
            event_type="invitation_rejected",
            result="denied",
            reason=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.safe_message
        ) from exc
    except ShareInvitationEmailMismatchError as exc:
        record_share_event(
            session,
            share_id=None,
            conversation_id=None,
            actor_user_id=user.id,
            event_type="invitation_rejected",
            result="denied",
            reason="email_mismatch",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.safe_message) from exc

    share = get_share_by_id(session, share_id=member.share_id)
    record_share_event(
        session,
        share_id=member.share_id,
        conversation_id=share.conversation_id if share else None,
        actor_user_id=user.id,
        event_type="invitation_accepted",
        result="allowed",
    )
    return AcceptInvitationResponse(
        conversation_id=share.conversation_id if share else uuid.UUID(int=0),
        member_view_path=f"/shared/{member.share_id}",
    )


# --- Anonymous/member viewer surface -------------------------------------------


@router.get("/share-view/{ref}", response_model=SharedConversationOut)
def get_shared_conversation_route(
    ref: str,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_identity_db),
) -> SharedConversationOut:
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not settings.enable_conversation_sharing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found.", headers=_SAFE_ERROR_HEADERS
        )

    client_ip = resolve_client_ip(request, settings)
    if (
        not get_share_link_access_limiter(
            client_ip, settings.share_link_access_rate_limit_per_minute
        )
        .check()
        .allowed
    ):
        raise _rate_limited()

    actor = _resolve_optional_local_actor(session, authorization, settings)
    now = datetime.now(UTC)

    parsed_share_id = _parse_uuid_or_none(ref)
    if parsed_share_id is not None:
        share = get_share_by_id(session, share_id=parsed_share_id)
        link = None
        member = (
            get_member_for_user(session, share_id=share.id, user_id=actor.id)
            if share is not None and actor is not None
            else None
        )
    else:
        resolved = resolve_link_by_raw_token(session, raw_token=ref)
        share, link = resolved if resolved is not None else (None, None)
        member = None

    conversation_deleted = _conversation_deleted(session, share) if share is not None else True
    decision = authorize_share_action(
        actor=actor,
        actor_tenant_id=resolve_actor_tenant_id(actor),
        share=share,
        action=ShareAction.VIEW,
        conversation_deleted=conversation_deleted,
        member=member,
        link=link,
        now=now,
    )

    if not decision.allowed:
        record_share_event(
            session,
            share_id=share.id if share else None,
            conversation_id=share.conversation_id if share else None,
            actor_user_id=actor.id if actor else None,
            event_type="view_denied",
            result="denied",
            reason=decision.reason,
            safe_metadata={"client_ip_present": True},
        )
        raise _share_unavailable()

    if share is None:
        # Cannot happen: `decision.allowed` is only ever True when `share`
        # is non-`None` (see `identity.share_policy.authorize_share_action`'s
        # own `share is None -> deny("share_not_found")` guard) -- a real
        # `raise`, not a bare `assert`, so this stays a hard failure even
        # under `-O` optimized bytecode.
        raise _share_unavailable()
    if link is not None:
        record_link_used(session, link=link)
    projection = build_share_projection(session, share=share)
    record_share_event(
        session,
        share_id=share.id,
        conversation_id=share.conversation_id,
        actor_user_id=actor.id if actor else None,
        event_type="view_allowed",
        result="allowed",
        reason=decision.reason,
    )
    return SharedConversationOut(
        conversation_title=projection.conversation_title,
        feature_type=projection.feature_type,
        snapshot_captured_at=projection.snapshot_captured_at,
        viewer_role=decision.reason,  # type: ignore[arg-type]
        turns=[_projected_turn_out(t) for t in projection.turns],
    )


@router.get("/share-view/{ref}/attachments/{attachment_id}")
def get_shared_attachment_route(
    ref: str,
    attachment_id: str,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_identity_db),
) -> Response:
    """Re-derives full share authorization from scratch on every call --
    never trusts that a prior `GET /share-view/{ref}` succeeded, and never
    treats `attachment_id` as self-authorizing just because the caller can
    view the share at all: it must also be one of the specific attachments
    the snapshot's own approved turns actually reference (see
    `ShareProjection.approved_attachment_ids`'s own docstring)."""
    settings = get_settings()
    response.headers["Cache-Control"] = _PRIVATE_NO_STORE
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not settings.enable_conversation_sharing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found.", headers=_SAFE_ERROR_HEADERS
        )

    client_ip = resolve_client_ip(request, settings)
    if (
        not get_share_link_access_limiter(
            client_ip, settings.share_link_access_rate_limit_per_minute
        )
        .check()
        .allowed
    ):
        raise _rate_limited()

    actor = _resolve_optional_local_actor(session, authorization, settings)
    now = datetime.now(UTC)

    parsed_share_id = _parse_uuid_or_none(ref)
    if parsed_share_id is not None:
        share = get_share_by_id(session, share_id=parsed_share_id)
        link = None
        member = (
            get_member_for_user(session, share_id=share.id, user_id=actor.id)
            if share is not None and actor is not None
            else None
        )
    else:
        resolved = resolve_link_by_raw_token(session, raw_token=ref)
        share, link = resolved if resolved is not None else (None, None)
        member = None

    conversation_deleted = _conversation_deleted(session, share) if share is not None else True
    decision = authorize_share_action(
        actor=actor,
        actor_tenant_id=resolve_actor_tenant_id(actor),
        share=share,
        action=ShareAction.DOWNLOAD_ATTACHMENT,
        conversation_deleted=conversation_deleted,
        member=member,
        link=link,
        now=now,
    )
    if not decision.allowed:
        record_share_event(
            session,
            share_id=share.id if share else None,
            conversation_id=share.conversation_id if share else None,
            actor_user_id=actor.id if actor else None,
            event_type="download_denied",
            result="denied",
            reason=decision.reason,
        )
        raise _share_unavailable()

    if share is None:
        # See the identical guard in `get_shared_conversation_route` above.
        raise _share_unavailable()
    projection = build_share_projection(session, share=share)
    if attachment_id not in projection.approved_attachment_ids:
        # The share itself is viewable, but this specific attachment id was
        # never part of its approved snapshot -- a real IDOR attempt
        # (guessing/reusing an unrelated attachment id), distinct from an
        # ordinary "not approved yet" denial.
        record_share_event(
            session,
            share_id=share.id,
            conversation_id=share.conversation_id,
            actor_user_id=actor.id if actor else None,
            event_type="idor_attempt",
            result="denied",
            reason="attachment_not_in_snapshot",
        )
        raise _share_unavailable()

    from attachments.store import get_default_attachment_store

    store = get_default_attachment_store()
    item = store.get(attachment_id, owner_subject=str(share.owner_user_id))
    if item is None or item.local_path is None:
        record_share_event(
            session,
            share_id=share.id,
            conversation_id=share.conversation_id,
            actor_user_id=actor.id if actor else None,
            event_type="download_denied",
            result="denied",
            reason="attachment_unavailable",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This attachment is no longer available.",
            headers=_SAFE_ERROR_HEADERS,
        )

    with open(item.local_path, "rb") as handle:
        content = handle.read()

    record_share_event(
        session,
        share_id=share.id,
        conversation_id=share.conversation_id,
        actor_user_id=actor.id if actor else None,
        event_type="download_allowed",
        result="allowed",
        safe_metadata={"media_type": item.media_type},
    )
    return Response(
        content=content,
        media_type=item.media_type,
        headers={
            "Cache-Control": _PRIVATE_NO_STORE,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
