"""SQLAlchemy 2.0 declarative ORM models for the identity database.

The one genuinely new pattern in this codebase -- see `identity/__init__.py`'s
module docstring for why an ORM + real (Alembic) migrations are used here
specifically, rather than the raw-Core-plus-idempotent-`ensure_schema()`
convention every other DB-backed module in this repo follows.

23 tables, matching the schema this feature was specified against
(14 from the original build, plus `external_identities` added 2026-09-28
for Google sign-in, plus `conversation_shares`/`share_members`/
`share_links`/`share_audit_events` added 2026-09-28 for secure
conversation sharing, plus `onboarding_jobs`/`onboarding_review_items`/
`onboarding_artifacts` added 2026-10-01 for the client-database
onboarding engine, plus `semantic_catalog_entries` added 2026-10-01 for
the tenant-aware semantic catalog -- see those models' own docstrings,
`identity/share_policy.py`'s module docstring, `onboarding/policy.py`'s
module docstring, and `semantic/catalog_policy.py`'s module docstring
for the full designs):

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
- `external_identities` -- one row per linked external-provider identity
  (Google sign-in), `(provider, provider_subject)` unique and mapped to
  exactly one `user_id`. `users.password_hash` is nullable specifically to
  support a Google-only account with no local password at all -- see both
  models' own docstrings.
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
    # The account's tenant -- the single server-side source of truth every
    # tenant check in this codebase now resolves from (Prompt 20). Added
    # with a `server_default` precisely so the migration that introduces
    # it is non-destructive for an existing single-tenant deployment: every
    # pre-existing row lands in `security.tenancy.DEFAULT_TENANT_ID`, which
    # is exactly the value `resolve_actor_tenant_id` already returned for
    # them before this column existed, so no behavior changes on upgrade.
    # Never nullable: an account with no tenant would be an account no
    # deny-by-default ABAC check could reason about.
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        default="default",
        server_default="default",
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=True)
    # Nullable since 2026-09-28 (Google sign-in) -- a user provisioned via
    # `identity.repositories.external_identities.create_user_from_external_identity`
    # has no local password at all until they explicitly set one (see
    # `identity/repositories/users.py::create_user`'s own `password`
    # parameter, now optional). Never enable password login for a row with
    # `password_hash IS NULL` by accident -- `identity/security.py
    # ::verify_password` is never called with a `None` hash; every login-
    # path call site must check for this explicitly first.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
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
    external_identities: Mapped[list[ExternalIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
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


class ExternalIdentity(Base):
    """Maps one external-provider identity (Google sign-in, 2026-09-28; the
    shape is deliberately provider-generic so a second provider could reuse
    this same table later) to exactly one local `User`.

    `(provider, provider_subject)` is the actual identity key -- `sub` is
    Google's own stable, permanent subject identifier for one Google
    account, never the email/display name/picture, all of which a user can
    change at will on Google's side (see `security/google_oidc.py`'s own
    docstring for why `sub` and only `sub` is trusted as the durable
    identity). The unique constraint below is what makes "an identity
    already assigned to another user cannot be linked to a second one" a
    database-enforced invariant, not just an application-level check that a
    race condition could bypass -- `identity/repositories
    /external_identities.py::link_external_identity` relies on catching
    this constraint's `IntegrityError` for exactly that race.

    `email_at_link`/`email_verified_at_link` are a point-in-time audit
    snapshot only (what Google's token claimed at the moment of linking) --
    never re-read for authorization decisions after that, since a user's
    Google-side email can change independently of this row. The
    authoritative, current email for the account is always `User.email`.
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_subject", name="uq_external_identities_provider_subject"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_link: Mapped[str] = mapped_column(String(320), nullable=True)
    email_verified_at_link: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(back_populates="external_identities")


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
    # Defense in depth, not the primary boundary (Prompt 20): every
    # `identity.repositories.history` query already folds `user_id` into
    # its WHERE clause, and a user belongs to exactly one tenant, so a
    # cross-tenant read was already impossible through that path. Storing
    # the tenant on the row too means the repository layer can *also*
    # filter on it directly -- which is what turns "impossible because of
    # how the join happens to be written today" into an explicit,
    # independently-testable invariant that a future query shape can't
    # silently lose. Denormalized from `users.tenant_id` on purpose; set
    # once at creation and never changed (a user is never moved between
    # tenants by any code path in this repo -- see `docs/MULTI_TENANCY.md`).
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        default="default",
        server_default="default",
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


class ConversationShare(Base):
    """The one durable "this conversation is shared" record per conversation
    (`conversation_id` is `unique=True` -- there is never more than one; the
    owner updates it in place via `PATCH`, never creates a second). See
    `identity/share_policy.py`'s module docstring for the full RBAC/ABAC
    design this table is checked against, and `identity/repositories
    /shares.py` for every state transition.

    `tenant_id` is a **scoped, forward-compatible addition**, not a sign
    this app has become multi-tenant: `users`/`conversations` still have no
    tenant concept at all (see this module's own top-of-file docstring and
    `agent/rate_limit.py`'s existing "no-tenant-isolation" disclosure) --
    `security.tenancy.resolve_actor_tenant_id` returns one fixed constant
    for every real user in this deployment today. The column exists, and
    `identity.share_policy.authorize_share_action` genuinely enforces it,
    specifically so a future real multi-tenant retrofit only has to change
    that one resolver function, not this schema or the policy engine.

    `snapshot_message_sequence` is the server-side snapshot boundary (see
    "Sharing data model and snapshot boundary" in this feature's own spec):
    a viewer only ever sees `Prompt`/`AiOutput` rows with
    `sequence_number <= snapshot_message_sequence` for this conversation --
    a message sent after sharing was created/last updated is invisible
    until the owner explicitly re-shares (`PATCH .../share` with
    `refresh_snapshot=true`), never automatically.

    Never hard-deleted -- `status`/`revoked_at` are how sharing is turned
    off, so a conversation's own soft-delete (`Conversation.deleted_at`)
    doesn't need to cascade into this table at all: `identity.share_policy`
    independently re-checks `Conversation.deleted_at IS NULL` on every
    access, so a deleted conversation is unreachable via its share
    regardless of whether this row was proactively revoked too (defense in
    depth, not reliance on remembering to revoke on every deletion path).
    """

    __tablename__ = "conversation_shares"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    access_mode: Mapped[str] = mapped_column(
        Enum(
            "invite_only",
            "anyone_with_link",
            name="share_access_mode",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="invite_only",
    )
    default_permission: Mapped[str] = mapped_column(
        Enum("viewer", name="share_default_permission", native_enum=False, validate_strings=True),
        nullable=False,
        default="viewer",
    )
    status: Mapped[str] = mapped_column(
        Enum("active", "disabled", name="share_status", native_enum=False, validate_strings=True),
        nullable=False,
        default="active",
    )
    snapshot_message_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Default to a finite expiry, per this feature's own spec -- the owner
    # UI always shows this, and `identity.repositories.shares.create_share`
    # never leaves it unset without an explicit, deliberate owner choice.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optimistic concurrency -- every PATCH must supply the version it read
    # and increments this by exactly one; a stale write is rejected rather
    # than silently overwriting a concurrent settings change (see
    # `identity.repositories.shares.update_share`).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    members: Mapped[list[ShareMember]] = relationship(
        back_populates="share", cascade="all, delete-orphan"
    )
    links: Mapped[list[ShareLink]] = relationship(
        back_populates="share", cascade="all, delete-orphan"
    )


class ShareMember(Base):
    """One invited/accepted viewer of a `ConversationShare` -- **never the
    owner** (the owner is always `ConversationShare.owner_user_id`; the API
    layer synthesizes the owner's own row in a "people with access" list so
    there's exactly one place that distinction is made, not two competing
    sources of truth for who owns a share).

    `user_id` is null for a pending email invitation that hasn't been
    accepted by (or matched to) an account yet -- `invited_email` carries
    the target address in that state, and `invitation_token_hash` (never
    the raw token -- see `identity/security.py::hash_refresh_token`, reused
    directly rather than reinvented) is what `POST
    /share-invitations/{token}/accept` redeems, exactly once, to fill in
    `user_id` and flip `status` to `"active"`.
    """

    __tablename__ = "share_members"

    id: Mapped[uuid.UUID] = _uuid_pk()
    share_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversation_shares.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    invited_email: Mapped[str] = mapped_column(String(320), nullable=True)
    role: Mapped[str] = mapped_column(
        Enum("viewer", name="share_member_role", native_enum=False, validate_strings=True),
        nullable=False,
        default="viewer",
    )
    status: Mapped[str] = mapped_column(
        Enum(
            "pending",
            "active",
            "revoked",
            "expired",
            name="share_member_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="pending",
    )
    invitation_token_hash: Mapped[str] = mapped_column(String(64), nullable=True, unique=True)
    invitation_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # An optional, member-specific access expiry -- independent of the
    # share's own `expires_at` (a member can be granted a shorter window
    # than the share as a whole).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    share: Mapped[ConversationShare] = relationship(back_populates="members")


class ShareLink(Base):
    """One version of a share's "anyone with the link" bearer token.
    **Only `token_hash` (a SHA-256 hex digest, `identity.security
    .hash_refresh_token` reused directly) is ever stored** -- the raw token
    is generated (`identity.security.generate_refresh_token`, 48
    CSPRNG-random bytes, comfortably exceeding this feature's own 128-bit
    minimum), returned exactly once in the owner's own create/regenerate
    API response, and never persisted, logged, or included in any audit
    event's `safe_metadata` anywhere in this codebase.

    Regenerating a link **inserts a new row** (`token_version` incremented)
    and marks every prior row for the same `share_id` `revoked_at` in the
    same transaction, rather than mutating `token_hash` in place -- this is
    what makes "old tokens must fail even if not yet expired" trivially
    true (a superseded row's own `revoked_at` is checked independently of
    its `expires_at`) and keeps a genuine, queryable history of every link
    version ever issued for incident response.
    """

    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    share_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversation_shares.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    share: Mapped[ConversationShare] = relationship(back_populates="links")


class ShareAuditEvent(Base):
    """Privacy-safe event log for the sharing feature -- **never** a raw
    token, cookie, Authorization header, or private conversation/message
    text (`safe_metadata` is a bounded, redacted JSON blob the write path
    itself constructs from stable, non-sensitive fields only -- see
    `identity.share_audit.record_share_event`'s own docstring for the
    allowlist). `share_id` is `ON DELETE SET NULL` (mirroring `AuditLog`'s
    own `actor_user_id`/`subject_user_id` precedent above) so this table's
    own rows are never destroyed by anything happening to the share or
    conversation they describe -- the audit trail must be able to outlive
    both, which is exactly when an incident investigation needs it most.
    `conversation_id` is deliberately a bare column, not a foreign key, for
    the same reason.
    """

    __tablename__ = "share_audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    share_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversation_shares.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result: Mapped[str] = mapped_column(
        Enum(
            "allowed",
            "denied",
            "error",
            name="share_audit_result",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str] = mapped_column(String(100), nullable=True)
    safe_metadata: Mapped[dict] = mapped_column(_METADATA_JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class OnboardingJob(Base):
    """One client-database onboarding run -- Prompt 08
    (`08_ONBOARDING_ENGINE_CONTRACT.md`): discover -> profile -> classify
    PII -> infer semantics -> SME review -> build semantic contract ->
    generate golden questions -> evaluate -> publish, for a database not
    yet wired into `DB_CONNECTIONS`/`.env`.

    **Never stores a connection secret.** `db_type`/`db_host`/`db_port`/
    `db_name`/`db_user`/`db_schema` are persisted for audit/display only
    -- the actual password is supplied fresh on every API call that needs
    a live connection (`POST .../discover`, `POST .../publish`) and is
    never written to this row, a log line, or anywhere else. See
    `onboarding/jobs.py`'s own module docstring for the honest tradeoff
    this is: no fire-and-forget background worker that can silently
    resume a stage requiring a live connection without the caller
    supplying the secret again.

    `tenant_id` follows the exact scoped, forward-compatible pattern
    `ConversationShare.tenant_id` already established (see that model's
    own docstring) -- this app remains deliberately single-tenant
    platform-wide; only this table's own rows carry a real `tenant_id`
    with a real ABAC tenant-match check (`onboarding.policy
    .authorize_onboarding_action`).

    `discovery_summary` is a small, non-secret JSON rollup (table/view/
    column counts, relationship-candidate count, PII-flag count) for a
    cheap status display -- the full discovered detail lives in
    `OnboardingReviewItem`/`OnboardingArtifact` rows, not duplicated here.
    """

    __tablename__ = "onboarding_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    database_label: Mapped[str] = mapped_column(String(200), nullable=False)
    db_type: Mapped[str] = mapped_column(String(32), nullable=False)
    db_host: Mapped[str] = mapped_column(String(255), nullable=True)
    db_port: Mapped[int] = mapped_column(Integer, nullable=True)
    db_name: Mapped[str] = mapped_column(String(200), nullable=True)
    db_user: Mapped[str] = mapped_column(String(200), nullable=True)
    db_schema: Mapped[str] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "pending",
            "discovering",
            "awaiting_review",
            "publishing",
            "published",
            "failed",
            "cancelled",
            name="onboarding_job_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="pending",
    )
    current_stage: Mapped[str] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovery_summary: Mapped[dict] = mapped_column(_METADATA_JSON, nullable=True)
    # Optimistic concurrency, same contract as `ConversationShare.version`.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    review_items: Mapped[list[OnboardingReviewItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[OnboardingArtifact]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class OnboardingReviewItem(Base):
    """One SME-decidable item surfaced during discovery -- an inferred PII
    classification, an inferred/candidate relationship, a semantic label
    flagged ambiguous, or a candidate golden question. Every item starts
    `"pending"`; `payload["truth_level"]` is always
    `agent.provenance.DataTruthLevel.AI_INFERENCE.value` until an SME
    decides it (see `onboarding/policy.py`) -- this table is the SME
    review queue the acceptance criterion ("ambiguous business meaning is
    routed to SME review") names directly.

    `is_ambiguous` is set by `onboarding/semantic_inference.py`'s own
    ambiguity detection (low confidence, or more than one plausible
    label tied) -- an item can be surfaced for review without being
    ambiguous (e.g. a high-confidence PII match still needs a human
    sign-off before being written into `config/sensitive_columns.yaml`),
    but every *ambiguous* item is always surfaced.
    """

    __tablename__ = "onboarding_review_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("onboarding_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_type: Mapped[str] = mapped_column(
        Enum(
            "pii_classification",
            "relationship",
            "semantic_label",
            "golden_question",
            name="onboarding_review_item_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    table_name: Mapped[str] = mapped_column(String(200), nullable=True)
    column_name: Mapped[str] = mapped_column(String(200), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict] = mapped_column(_METADATA_JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    is_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision: Mapped[str] = mapped_column(
        Enum(
            "pending",
            "confirmed",
            "rejected",
            name="onboarding_review_decision",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="pending",
    )
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    job: Mapped[OnboardingJob] = relationship(back_populates="review_items")


class OnboardingArtifact(Base):
    """One produced artifact of a job -- a draft/final semantic contract
    (`config/table_descriptions.yaml`/`config/sensitive_columns.yaml`-
    shaped content built only from `"confirmed"` review items), the
    generated golden-question set, or an evaluation report. Versioned
    (`version`) rather than overwritten in place, so a job's full history
    of artifact revisions stays inspectable -- `onboarding/jobs.py` always
    inserts a new row rather than mutating an existing one.
    """

    __tablename__ = "onboarding_artifacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("onboarding_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(
        Enum(
            "semantic_contract",
            "golden_questions",
            "evaluation_report",
            name="onboarding_artifact_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    content: Mapped[dict] = mapped_column(_METADATA_JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()

    job: Mapped[OnboardingJob] = relationship(back_populates="artifacts")


class SemanticCatalogEntry(Base):
    """One **version** of one governed business concept -- Prompt 09
    (`09_SEMANTIC_CATALOG_CONTRACT.md`): a tenant-scoped, versioned
    catalog connecting technical metadata to business meaning (names,
    descriptions, grain, keys, relationships, domains, metrics,
    dimensions, synonyms, business rules, examples), with a real
    draft -> reviewed -> published -> superseded review workflow (see
    `semantic.catalog.CatalogStatus`).

    **One row per version, never mutated in place once published** --
    the direct fusion of `OnboardingArtifact`'s append-only versioning
    with `semantic.metrics.MetricDefinition.supersedes`'s chain: editing
    a published entry always creates a brand-new `DRAFT` row (a new
    `version` for the same `concept_key`), and publishing it flips the
    *previous* published row (if any) to `SUPERSEDED` and sets this new
    row's `supersedes_id` -- a concept's full version history stays
    inspectable, never overwritten. A `DRAFT` row is the one exception:
    it is mutable in place (`identity.repositories.semantic_catalog
    .update_draft_entry`) since nothing downstream depends on
    unpublished content yet.

    `concept_key` is the stable identifier grouping every version of
    "the same" concept (e.g. `"customer_lifetime_value"`) -- unique only
    together with `(tenant_id, database_id, concept_type, version)`, not
    on its own.

    `tenant_id` follows the exact scoped, forward-compatible pattern
    `OnboardingJob.tenant_id`/`ConversationShare.tenant_id` already
    established (see either model's own docstring) -- this app remains
    deliberately single-tenant platform-wide; only this table's own rows
    carry a real `tenant_id` with a real ABAC tenant-match check
    (`semantic.catalog_policy.authorize_catalog_action`).

    **Only a `PUBLISHED` row is ever rendered into a retrieval chunk**
    (`retrieval.chunking.business_concept_chunk_from_catalog_entry`,
    called only from the publish API route) -- a `DRAFT`/`REVIEWED` row
    structurally never reaches the LLM's prompt, which is what makes
    master-contract rule 10 ("never silently convert AI inference into
    confirmed business truth") true by construction here, not by a
    runtime check that could be forgotten.
    """

    __tablename__ = "semantic_catalog_entries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    database_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    concept_type: Mapped[str] = mapped_column(
        Enum(
            "entity",
            "metric",
            "dimension",
            "domain",
            name="semantic_catalog_concept_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        index=True,
    )
    concept_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    technical_name: Mapped[str] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    grain: Mapped[str] = mapped_column(String(500), nullable=True)
    keys: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    relationships: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    domain: Mapped[str] = mapped_column(String(200), nullable=True)
    synonyms: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    business_rules: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    examples: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(
        Enum(
            "draft",
            "reviewed",
            "published",
            "superseded",
            name="semantic_catalog_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="draft",
        index=True,
    )
    owner: Mapped[str] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Prompt 10 (10_GOVERNED_METRICS_CONTRACT.md): what makes a METRIC
    # entry *governing* rather than merely descriptive -- see
    # `semantic.catalog.CatalogEntrySnapshot`'s own docstring for why
    # `approved_expression`/`source_tables` are deliberately separate
    # fields from `technical_name`/`keys` above, not a reuse of either.
    approved_expression: Mapped[str] = mapped_column(Text, nullable=True)
    source_tables: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    filters: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    dimensions: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    aggregation: Mapped[str] = mapped_column(String(100), nullable=True)
    supersedes_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("semantic_catalog_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, nullable=True)
    published_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class RecommendationRecord(Base):
    """One persisted instance of a `recommendation.models.Recommendation`
    Prompt 17's `recommendation.engine.generate_recommendations` produced
    -- Prompt 18 (`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).

    **Content is immutable once created; only `status` ever changes.**
    Unlike `identity.models.SemanticCatalogEntry` (one row per *version*
    of evolving content), a recommendation's own finding/evidence/claim
    is a fact about a specific point in time -- it never gets "edited,"
    only judged. `status` (`recommendation.governance.RecommendationStatus`)
    is mutated in place by `identity/repositories/recommendation_governance
    .py::submit_feedback`, which also appends an immutable
    `RecommendationFeedbackEvent` row recording exactly what changed, by
    whom, and why -- the append-only audit trail this row's own mutable
    `status` column, by itself, could not provide.

    `evidence`/`limitations` are stored verbatim from the originating
    `Recommendation.model_dump()` -- the literal content a human's later
    feedback is about, preserved regardless of whether
    `recommendation.engine`'s own rules change afterward.
    `engine_version`/`evidence_version` are two independent, deliberately
    separate snapshots of "what produced this": `engine_version` is
    `Recommendation.engine_version` (`recommendation.models
    .RECOMMENDATION_ENGINE_VERSION` at generation time); `evidence_version`
    is a SHA-256 hex digest over the `evidence` JSON itself -- a concrete,
    comparable, content-addressable identifier for the specific evidence
    judged, independent of whichever analytics-engine version happened to
    be running (that version isn't separately recorded anywhere on
    `recommendation.models.Recommendation` itself today). Both are
    re-snapshotted onto every `RecommendationFeedbackEvent` row at the
    moment of that event -- Prompt 18's own "capture ... recommendation/
    evidence versions" requirement, applied per-event so a quality
    rollup can be sliced by exactly which version a piece of feedback was
    actually about.

    `tenant_id` follows the exact scoped, forward-compatible pattern
    `OnboardingJob.tenant_id`/`SemanticCatalogEntry.tenant_id` already
    established (see either model's own docstring) -- this app remains
    deliberately single-tenant platform-wide; only this table's own rows
    carry a real `tenant_id` with a real ABAC tenant-match check
    (`recommendation.governance_policy.authorize_recommendation_action`).

    `source_question`/`source_sql` are display-only context (what
    question/query this recommendation was about), not foreign keys into
    `identity.models.Conversation`/`Prompt` -- a recommendation can come
    from a chat turn, a future standalone report, or a batch recompute,
    and coupling this table to chat-history's own schema would be exactly
    the kind of premature cross-feature dependency this codebase avoids
    elsewhere (e.g. `OnboardingJob.discovery_summary`'s identical
    "display-only, no FK" choice).
    """

    __tablename__ = "recommendation_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    database_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(
        Enum(
            "performance",
            "anomaly",
            "revenue",
            "customer",
            "product",
            "operations",
            "data_quality",
            "security",
            "database_performance",
            name="recommendation_category",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=True,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        Enum(
            "next_question",
            "action",
            name="recommendation_kind",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    rule_or_model: Mapped[str] = mapped_column(String(200), nullable=True)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)
    affected_entity: Mapped[str] = mapped_column(String(500), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=True)
    measurable_impact: Mapped[str] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=True)
    evidence: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    limitations: Mapped[list] = mapped_column(_METADATA_JSON, nullable=False, default=list)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "generated",
            "reviewed",
            "accepted",
            "rejected",
            "partially_useful",
            "incorrect",
            "resolved",
            "expired",
            name="recommendation_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="generated",
        index=True,
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_question: Mapped[str] = mapped_column(Text, nullable=True)
    source_sql: Mapped[str] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Prompt 31 -- the one mutable field besides `status`: who is responsible
    # for acting on this recommendation. Changes are recorded as
    # `RecommendationFeedbackEvent(event_type="owner_assigned")`, never
    # silently overwritten. `NULL` = unassigned.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    feedback_events: Mapped[list[RecommendationFeedbackEvent]] = relationship(
        back_populates="record",
        cascade="all, delete-orphan",
        order_by="RecommendationFeedbackEvent.created_at",
    )


class RecommendationFeedbackEvent(Base):
    """One immutable feedback/lifecycle-transition event against a
    `RecommendationRecord` -- Prompt 18
    (`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`)'s append-only audit
    trail: "capture actor, reason, timestamp, recommendation/evidence
    versions," applied per-event rather than as a handful of mutable
    columns on the record itself (unlike `SemanticCatalogEntry`'s inline
    `reviewed_by_user_id`/`reviewed_at`/`review_notes`, which only ever
    remember the *most recent* review decision) -- a recommendation's
    quality can be judged more than once over its life (e.g. reviewed,
    then later resolved), and every one of those judgments stays
    inspectable here, never overwritten.

    `from_status` is `None` only for the very first event (the one
    `create_record` itself inserts, `to_status="generated"`) -- every
    later event always has a real `from_status`, since it's a transition
    *from* whatever the record's status already was.

    `actor_user_id`/`actor_label` are deliberately both optional and
    mutually a fallback for each other: a real human reviewer sets
    `actor_user_id` (and `actor_label` stays `None`); an automated
    transition (e.g. a future scheduled expiry sweep) sets `actor_label`
    (e.g. `"system:expiry_sweep"`) with `actor_user_id` left `None` --
    the audit trail always names *something*, never silently blank, the
    same "a system actor still gets a readable label" convention
    `security.audit_log`'s own structured events already follow informally
    via their `event_type` field.

    **This table is never read by anything that changes
    `recommendation.engine`'s own behavior.** Master-contract requirement
    ("feedback must not automatically alter production rules from a
    single event") is satisfied structurally: no code path in this
    codebase reads `RecommendationFeedbackEvent` rows and writes back to
    `config.settings.Settings` or any `recommendation.engine` rule
    threshold. This table is write-and-report only -- an aggregate
    rollup (`identity.repositories.recommendation_governance
    .quality_metrics_for_tenant`) is the sole, read-only consumer, for a
    future human-facing dashboard to act on deliberately.
    """

    __tablename__ = "recommendation_feedback_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("recommendation_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[str] = mapped_column(
        Enum(
            "generated",
            "reviewed",
            "accepted",
            "rejected",
            "partially_useful",
            "incorrect",
            "resolved",
            "expired",
            name="recommendation_feedback_from_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=True,
    )
    to_status: Mapped[str] = mapped_column(
        Enum(
            "generated",
            "reviewed",
            "accepted",
            "rejected",
            "partially_useful",
            "incorrect",
            "resolved",
            "expired",
            name="recommendation_feedback_to_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str] = mapped_column(String(200), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    recommendation_version: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Prompt 31 -- what kind of event this is. `status_change` (the only kind
    # before Prompt 31; from_status != to_status), `note` (from == to, the
    # reason field holds the note text), or `owner_assigned` (from == to,
    # `detail` holds the new owner). Keeps notes and ownership changes in the
    # same append-only trail without a second audit table.
    event_type: Mapped[str] = mapped_column(
        Enum(
            "status_change",
            "note",
            "owner_assigned",
            name="recommendation_feedback_event_type",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="status_change",
        server_default="status_change",
    )
    detail: Mapped[dict] = mapped_column(_METADATA_JSON, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    record: Mapped[RecommendationRecord] = relationship(back_populates="feedback_events")


class Tenant(Base):
    """One tenant -- the platform's first-class isolation boundary
    (Prompt 20, `20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`).

    Before this table existed, `tenant_id` already appeared on five tables
    (`ConversationShare`, `OnboardingJob`, `SemanticCatalogEntry`,
    `RecommendationRecord`, `RecommendationFeedbackEvent`) and four
    deny-by-default ABAC policy modules genuinely compared it -- but
    `security.tenancy.resolve_actor_tenant_id` returned one hardcoded
    constant for *every* user, so no two real callers could ever be in
    different tenants and every one of those comparisons was vacuous in
    practice. This table, plus `User.tenant_id` below, is what makes them
    load-bearing: an actor's tenant is now a persisted, server-controlled
    fact read from their own account row, never a value any client sends.

    `id` is a short, operator-chosen slug (not a UUID) deliberately: every
    pre-existing `tenant_id` column in this schema is already
    `String(64)`, and `security.tenancy.DEFAULT_TENANT_ID` (`"default"`)
    is already persisted in real rows written by Prompts 08/09/18. A UUID
    primary key here would have required rewriting those rows -- a
    destructive change this contract's rule 14 forbids without explicit
    approval -- for no isolation benefit, since the column is never
    client-supplied.

    `status` is the one genuinely behavioral field: a `"suspended"` tenant
    is refused at the tenant-resolution step
    (`security.tenancy.resolve_tenant_context`), which fails closed for
    every downstream check at once rather than needing each route to
    remember a status check of its own.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "active",
            "suspended",
            name="tenant_status",
            native_enum=False,
            validate_strings=True,
        ),
        nullable=False,
        default="active",
        server_default="active",
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", _METADATA_JSON, nullable=True)
