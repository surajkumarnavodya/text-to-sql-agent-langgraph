"""Pydantic request/response models for `api/identity_auth.py` (Phase A).

Mirrors `api/schemas.py`'s own conventions exactly: request models get
`str_strip_whitespace=True` + `extra="forbid"`, response models get
`frozen=True`. Kept in a separate file (rather than folded into the
already-large `api/schemas.py`) since this is a self-contained new feature
surface with its own, fairly large set of shapes -- see
`identity/__init__.py`'s module docstring.

**`UserOut` never carries `password_hash`, ever** -- it's simply not one of
this model's fields, so there's no field to accidentally forget to
exclude (the same "the shape itself makes the leak impossible, not a
serialization-time filter" principle `rag.store.DocumentRecord`'s
`restricted_roles`/`uploaded_by` split already follows for a different
kind of sensitive field).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from identity.display_name import validate_display_name


class RegisterRequest(BaseModel):
    """`display_name` is **mandatory** -- see `identity/display_name.py`'s
    own docstring for the validation rules and why this is never used as an
    identity key. `min_length=1`/`max_length=200` here are just the outer
    request-shape bounds Pydantic enforces before the field validator even
    runs; the real min/max (2-100 chars, after Unicode normalization) is
    `validate_display_name`'s job."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=256)
    display_name: str = Field(..., min_length=1, max_length=200)
    username: str | None = Field(default=None, max_length=64)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return validate_display_name(value)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=1, max_length=256)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    token: str = Field(..., min_length=1, max_length=512)
    new_password: str = Field(..., min_length=1, max_length=256)


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    token: str = Field(..., min_length=1, max_length=512)


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: EmailStr


class GoogleNonceResponse(BaseModel):
    """`GET /auth/google/nonce` -- see `security/google_oidc.py`'s own
    module docstring for exactly what this nonce does and does not
    protect against."""

    model_config = ConfigDict(frozen=True)

    nonce: str


class GoogleSignInRequest(BaseModel):
    """`POST /auth/google`/`POST /auth/google/link` -- `credential` is the
    raw ID token string from Google Identity Services' JS callback
    (`{credential}` in its own response shape), never decoded/trusted
    client-side (see `security/google_oidc.py`). The length bound here is
    a first, cheap rejection of an obviously-malformed request before the
    real verification call -- `security.google_oidc.verify_google_id_token`
    enforces the authoritative bound independently."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    credential: str = Field(..., min_length=1, max_length=8192)


class LinkedIdentityOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    email_at_link: str | None
    created_at: datetime
    last_used_at: datetime


class LinkedIdentityListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    identities: list[LinkedIdentityOut]
    # Whether removing a linked identity is even possible right now for
    # this account -- the frontend uses this to disable the "unlink"
    # action instead of letting the caller find out via a failed request
    # (see `identity.repositories.external_identities
    # .unlink_external_identity`'s own "last sign-in method" guard).
    has_password: bool


class UserOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    username: str | None
    display_name: str | None
    # True only for an account created before display_name became mandatory
    # at sign-up (see identity/display_name.py) -- the frontend uses this
    # to show a one-time, non-blocking "complete your profile" prompt
    # (PATCH /auth/me) rather than inferring it from display_name being
    # null itself, so the "what counts as incomplete" rule lives in exactly
    # one place.
    needs_profile_completion: bool
    status: str
    is_email_verified: bool
    roles: list[str]
    created_at: datetime
    last_login_at: datetime | None


class TokenResponse(BaseModel):
    """Returned by `POST /auth/register`/`login`/`refresh`. The refresh
    token itself is **never** in this body -- it's set as a `Secure`,
    `HttpOnly`, `SameSite` cookie on the same response (see
    `api/identity_auth.py`'s own handlers), matching this feature's own
    "prefer refresh tokens in cookies for browser clients, never
    localStorage" requirement."""

    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class SessionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    device_name: str | None
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    is_current: bool


class SessionListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions: list[SessionOut]


class MessageResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str


class UpdateProfileRequest(BaseModel):
    """`PATCH /auth/me` -- today the only editable profile field is
    `display_name`. Used both for an ordinary profile edit and for the
    profile-completion prompt an account created before `display_name`
    became mandatory sees (see `api/identity_auth.py::update_profile`'s
    docstring) -- same validation either way, via the identical
    `validate_display_name` call `RegisterRequest` uses."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    display_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return validate_display_name(value)


# --- Chat history (identity/repositories/history.py, api/chat_history.py) ---


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    feature_type: str = Field(default="text_to_sql", max_length=30)


class UpdateConversationRequest(BaseModel):
    """`title`/`archived` are both optional -- only the fields actually
    present in the request are applied (see `identity.repositories.history
    .update_conversation`); omitting a field leaves it unchanged, it is
    never reset to a default."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    archived: bool | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    title: str | None
    feature_type: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None
    archived_at: datetime | None


class ConversationListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversations: list[ConversationOut]
    total: int
    limit: int
    offset: int


class CreateMessageRequest(BaseModel):
    """Direct message append -- mainly for API completeness/testing; the
    real, normal path a chat turn is persisted through is `POST /ask`
    itself (see `api/chat_persistence.py`), which always writes a
    question+answer pair together. This endpoint accepts a single message
    with an explicit `role`, useful for a caller integrating without going
    through `/ask` at all."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=20_000)


class MessageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    sequence_number: int
    created_at: datetime
    status: str | None
    model_name: str | None
    error_code: str | None
    metadata: dict | None = None


class MessageListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[MessageOut]
    total_turns: int
    limit: int
    offset: int


class SearchHitOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: uuid.UUID
    title: str | None
    matched_in: Literal["title", "message"]
    snippet: str
    message_id: uuid.UUID | None
    updated_at: datetime


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: list[SearchHitOut]
    total: int
    limit: int
    offset: int
    query: str
