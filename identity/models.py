"""SQLAlchemy 2.0 declarative ORM models for the identity database.

The one genuinely new pattern in this codebase -- see `identity/__init__.py`'s
module docstring for why an ORM + real (Alembic) migrations are used here
specifically, rather than the raw-Core-plus-idempotent-`ensure_schema()`
convention every other DB-backed module in this repo follows.

14 tables, matching the schema this feature was specified against:

- `users` / `roles` / `permissions` / `user_roles` / `role_permissions` --
  accounts and RBAC. `roles.name` is deliberately seeded with the same
  base role vocabulary `agent.authz.ROLE_PERMISSIONS` already knows
  (`viewer`/`user`/`analyst`/`admin`, see `identity/rbac.py`'s seed data)
  so a locally-authenticated user's roles feed straight into
  `security.oidc.AuthIdentity.roles` and every *existing* AI/RAG/SQL
  route's authorization check works completely unchanged -- this table
  does not replace `agent/authz.py`, it's a second, additive, more
  granular permission layer that only gates the *new* user/history/admin
  endpoints (`identity/rbac.py`'s own `Permission` codes, stored in
  `permissions`/`role_permissions`).
- `auth_sessions` / `signin_events` / `password_reset_tokens` /
  `email_verification_tokens` -- session and credential lifecycle.
  `auth_sessions.refresh_token_hash` stores only a SHA-256 hash of an
  opaque, high-entropy random refresh token (`identity/security.py`) --
  the raw token is never persisted anywhere, mirroring
  `password_reset_tokens`/`email_verification_tokens`'s own
  never-store-the-raw-token contract.
- `conversations` / `prompts` / `ai_outputs` / `voice_transcripts` -- AI
  activity history, strictly owned by `user_id` (see each repository
  function in `identity/repositories/history.py` for the ownership checks
  applied on every read/write).
- `audit_logs` -- a general user-action audit trail, deliberately **not**
  cascade-deleted when a user is removed (`actor_user_id`/
  `subject_user_id` are `ON DELETE SET NULL`, every other table's `user_id`
  is `ON DELETE CASCADE`) -- an audit record must be able to outlive the
  account it describes.

UUID primary keys are generated application-side (`uuid.uuid4`, not
Postgres's `gen_random_uuid()`) so this schema needs no `pgcrypto`/`uuid-ossp`
extension enabled on a fresh database. Every `metadata`-named column (this
feature's own spec's naming) is mapped to the Python attribute
`metadata_json` instead, since `metadata` is a reserved name on every
SQLAlchemy declarative model (the class-level `Base.metadata` registry) --
the actual database column is still literally named `metadata`.

**A new "Python 3.14 gotcha" (see `CLAUDE.md`'s own section for the other
two already hit in this codebase): a nullable column below is never
annotated `Mapped[X | None]`/`Mapped[Optional[X]]`.** SQLAlchemy 2.0.36's
declarative annotation resolution (`sqlalchemy.util.typing
.de_stringify_union_elements` -> `make_union_type`) crashes with
`TypeError: descriptor '__getitem__' requires a 'typing.Union' object but
received a 'tuple'` on *any* `Mapped[...]` union annotation under this
project's pinned Python 3.14 -- reproduced in three lines with no ORM
model of this codebase's own involved, regardless of `from __future__
import annotations`. Every nullable column here is instead annotated with
its bare (non-Optional) Python type and passed `nullable=True` explicitly
to `mapped_column(...)` -- SQLAlchemy infers nullability from the
*explicit* kwarg when present, never solely from the annotation, so this
is a real, correct, if slightly typing-lossy (mypy sees the attribute as
non-Optional) workaround, not a hidden bug. Revisit once a SQLAlchemy
release with confirmed Python 3.14 support for this path is out.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Native JSONB on PostgreSQL (production); plain JSON on SQLite (this
# repo's testing convention, since a real Postgres instance isn't required
# to run `pytest` -- see identity/repositories/ test files). `sqlalchemy
# .Uuid` (not the postgresql-dialect-specific `UUID`) is used for the same
# cross-dialect-portable reason: it renders as native `UUID` on Postgres
# and as a plain string column on SQLite automatically.
_METADATA_JSON = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    """Declarative base for every identity-database model. Deliberately
    its own base class, not shared with anything else in this repo -- no
    other module here uses the ORM at all (see this package's own
    docstring)."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # native_enum=False -> a VARCHAR + CHECK constraint, not a Postgres-native
    # ENUM type -- avoids the extra "ALTER TYPE ... ADD VALUE" migration
    # ceremony a native enum would need if a status is ever added later.
    status: Mapped[str] = mapped_column(
        Enum(
            "pending_verification",
            "active",
            "inactive",
            "suspended",
            name="user_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="active",
    )
    is_email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user", foreign_keys="UserRole.user_id", cascade="all, delete-orphan"
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    # Seeded roles (identity/rbac.py's SEED_ROLES) are marked True -- purely
    # informational today (nothing refuses to delete/edit a system role),
    # reserved for a future admin-UI guard against removing one of the base
    # roles agent/authz.py's ROLE_PERMISSIONS bridge depends on.
    is_system_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(back_populates="roles", foreign_keys=[user_id])


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class AuthSession(Base):
    """One refresh-token lineage. `session_family_id` is shared by every
    row produced by rotating the same original login -- reusing an
    already-rotated/revoked token's hash (`identity/repositories/sessions.py
    ::rotate_refresh_token`) revokes every session sharing that family,
    not just the one row the reused token matched."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_family_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    device_name: Mapped[str] = mapped_column(String(200), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str] = mapped_column(String(200), nullable=True)
    replaced_by_session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)


class SigninEvent(Base):
    __tablename__ = "signin_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Nullable: an event for an email that doesn't match any account at all
    # (a failed lookup) has no user_id to attach to -- see this column's own
    # spec note ("nullable for unknown email/failed lookup cases").
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    identifier_attempted: Mapped[str] = mapped_column(String(320), nullable=True)
    event_type: Mapped[str] = mapped_column(
        Enum(
            "login_success",
            "login_failed",
            "logout",
            "token_refresh",
            "session_revoked",
            "account_locked",
            "password_reset_requested",
            "password_reset_completed",
            "email_verification_sent",
            "email_verified",
            name="signin_event_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason_code: Mapped[str] = mapped_column(String(100), nullable=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=True)
    feature_type: Mapped[str] = mapped_column(
        Enum(
            "chat",
            "text_to_sql",
            "rag",
            "voice",
            "web_search",
            "document_search",
            name="conversation_feature_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )
    # Set by identity.repositories.history.append_turn on every persisted
    # prompt/ai_output pair -- a dedicated column (not just reading
    # `updated_at`) because `updated_at` also moves on a pure metadata edit
    # (a rename), which must not make an empty conversation look like it
    # has recent activity in the history panel's ordering.
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Distinct from `deleted_at` below -- an archived conversation is still
    # fully readable/searchable (chat-history-architecture.md's "keep
    # active, archived, deleted states distinct" requirement), it's only
    # hidden from the default conversation list.
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)


class Prompt(Base):
    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_prompt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prompts.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(
        Enum(
            "typed",
            "voice_raw",
            "voice_corrected",
            "system_generated",
            "imported",
            name="prompt_source_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    final_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(String(20), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    # Deterministic per-conversation ordering, assigned by
    # identity.repositories.history.append_turn (never the client) --
    # shares one counter with `ai_outputs.sequence_number` for the same
    # conversation, so a prompt/ai_output pair always sorts as
    # (question, answer) adjacent to each other regardless of how close
    # their `created_at` timestamps land.
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)


class AiOutput(Base):
    __tablename__ = "ai_outputs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prompts.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    output_type: Mapped[str] = mapped_column(
        Enum(
            "chat_answer",
            "sql_query",
            "sql_result_summary",
            "rag_answer",
            "voice_correction",
            "error",
            name="ai_output_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(50), nullable=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "completed",
            "failed",
            "cancelled",
            "blocked",
            name="ai_output_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    error_code: Mapped[str] = mapped_column(String(100), nullable=True)
    # See Prompt.sequence_number's docstring -- shares the same
    # per-conversation counter.
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)


class VoiceTranscript(Base):
    __tablename__ = "voice_transcripts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_transcript: Mapped[str] = mapped_column(Text, nullable=True)
    corrected_transcript: Mapped[str] = mapped_column(Text, nullable=True)
    final_transcript: Mapped[str] = mapped_column(Text, nullable=True)
    asr_provider: Mapped[str] = mapped_column(String(50), nullable=True)
    asr_model: Mapped[str] = mapped_column(String(100), nullable=True)
    asr_confidence: Mapped[float] = mapped_column(Float, nullable=True)
    correction_confidence: Mapped[float] = mapped_column(Float, nullable=True)
    material_change: Mapped[bool] = mapped_column(Boolean, nullable=True)
    requires_user_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=True)
    auto_submitted: Mapped[bool] = mapped_column(Boolean, nullable=True)
    correction_status: Mapped[str] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)


class AuditLog(Base):
    """Deliberately **not** cascade-deleted when a user is removed -- see
    this module's own docstring. `actor_user_id`/`subject_user_id` are
    `ON DELETE SET NULL`, unlike every other table's `user_id`."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str] = mapped_column(
        Enum(
            "success",
            "failure",
            "denied",
            name="audit_log_outcome",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    ip_address: Mapped[str] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=True)
    request_id: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)
