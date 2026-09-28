"""Pydantic request/response models for `api/shares.py`. Mirrors
`identity/schemas.py`'s own conventions exactly: request models get
`str_strip_whitespace=True` + `extra="forbid"`, response models get
`frozen=True`. Kept in its own file for the same reason `identity/schemas.py`
itself was split out from `api/schemas.py` -- a self-contained feature
surface with its own, fairly large set of shapes.

**No response model here ever carries a raw share-link token except
`ShareLinkOut`, and only from the two endpoints
(`POST .../share`/`POST .../share/link/regenerate`) explicitly authorized
to mint one.** Every other response that references a link only ever
reports whether one exists (`link_available`), never its value.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

ShareAccessMode = Literal["invite_only", "anyone_with_link"]
ShareStatus = Literal["active", "disabled"]
ShareMemberStatus = Literal["pending", "active", "revoked", "expired"]


class CreateShareRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    access_mode: ShareAccessMode = "invite_only"
    expiry_days: int | None = Field(default=None, gt=0, le=365)


class UpdateShareRequest(BaseModel):
    """`version` is mandatory -- the optimistic-concurrency token the caller
    must have just read via `GET .../share`. A stale value is rejected
    (`409`, `ShareVersionConflictError`) rather than silently applied over
    whatever a concurrent update just set."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(..., ge=1)
    access_mode: ShareAccessMode | None = None
    expiry_days: int | None = Field(default=None, gt=0, le=365)
    status: ShareStatus | None = None
    refresh_snapshot: bool = False


class InviteMemberRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: EmailStr
    expiry_days: int | None = Field(default=None, gt=0, le=90)


class RevokeShareRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    version: int = Field(..., ge=1)


class ShareMemberOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    invited_email: str | None
    display_name: str | None
    role: Literal["viewer"]
    status: ShareMemberStatus
    expires_at: datetime | None
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ShareOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    access_mode: ShareAccessMode
    default_permission: Literal["viewer"]
    status: ShareStatus
    snapshot_message_sequence: int
    snapshot_captured_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    members: list[ShareMemberOut]
    # Whether an active "anyone with the link" link currently exists --
    # never the token/URL itself (see this module's own docstring).
    link_available: bool
    # The relative viewer path a member (or the owner, previewing) can open
    # directly, e.g. "/shared/<share-id>" -- never includes a bearer secret,
    # since reaching it via this path still requires an authenticated,
    # authorized session. The frontend prepends its own origin.
    member_view_path: str


class ShareLinkOut(BaseModel):
    """Returned **only** by `POST .../share` (when `access_mode ==
    "anyone_with_link"`) and `POST .../share/link/regenerate` -- the one
    place a raw, usable bearer path is ever handed back. `view_path` is a
    relative path (`/shared/<raw-token>`); the frontend, not this backend,
    turns it into an absolute URL using `window.location.origin`, so this
    server never has to trust a `Host`/`X-Forwarded-Host` header to build a
    link (closing an open-redirect/host-confusion class of bug before it
    could exist)."""

    model_config = ConfigDict(frozen=True)

    view_path: str
    expires_at: datetime | None


class InviteMemberResponse(BaseModel):
    """`invitation_path` is the relative frontend route
    (`/accept-invitation/<raw-token>`) the owner copies and sends the
    invitee out-of-band -- this app has no outbound email integration for
    invites (see `docs/SHARING_SECURITY.md`'s own disclosed limitation).
    `None` when this call resolved to an already-*active* member (a repeat
    "invite" of someone who already accepted) -- there is nothing left to
    send."""

    model_config = ConfigDict(frozen=True)

    member: ShareMemberOut
    invitation_path: str | None


class ShareResponse(BaseModel):
    """`POST .../share` and `POST .../share/link/regenerate`'s response --
    the share's own settings plus, only when a link now exists, the one-time
    `link`. `PATCH`/`GET .../share` reuse plain `ShareOut` (no `link` field
    at all), since neither of those ever mints or re-reveals a token."""

    model_config = ConfigDict(frozen=True)

    share: ShareOut
    link: ShareLinkOut | None = None


class AcceptInvitationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: uuid.UUID
    member_view_path: str


class ProjectedSourceOut(BaseModel):
    """A generic, already-redacted source contribution (document/policy/web/
    generation/media-search) -- deliberately a passthrough `dict`-shaped
    model (`extra="allow"`) rather than one schema per source type, since
    `identity.repositories.shares._PROJECTED_METADATA_FIELDS` is the real
    allowlist boundary; this model only needs to round-trip whatever that
    boundary already approved, not re-validate its internal shape."""

    model_config = ConfigDict(extra="allow", frozen=True)


class ProjectedTurnOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence_number: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    sources_used: list[str] = Field(default_factory=list)
    database: str | None = None
    model: str | None = None
    sql: str | None = None
    row_count: int | None = None
    insight: str | None = None
    synthesized_answer: str | None = None
    document_result: dict | None = None
    policy_result: dict | None = None
    web_result: dict | None = None
    generation_result: dict | None = None
    media_search_result: dict | None = None
    attachment_refs: list[dict] = Field(default_factory=list)
    result_snapshot: dict | None = None


class SharedConversationOut(BaseModel):
    """`GET /share-view/{ref}`'s response -- the complete, already-authorized,
    already-filtered payload a shared viewer renders. There is nothing for
    the frontend to further redact: every field here already passed
    `identity.repositories.shares.build_share_projection`'s allowlist."""

    model_config = ConfigDict(frozen=True)

    conversation_title: str | None
    feature_type: str
    snapshot_captured_at: datetime | None
    viewer_role: Literal["owner", "member", "public_link"]
    turns: list[ProjectedTurnOut]
