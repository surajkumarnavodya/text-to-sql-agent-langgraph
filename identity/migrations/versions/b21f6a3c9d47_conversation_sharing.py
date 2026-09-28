"""secure conversation sharing: shares, members, links, audit events

Revision ID: b21f6a3c9d47
Revises: a09ce853cb0d
Create Date: 2026-09-28 00:00:00.000000

Adds the four tables `identity/models.py::ConversationShare`/`ShareMember`/
`ShareLink`/`ShareAuditEvent` document in full -- see each model's own
docstring for the design rationale (snapshot boundary, hash-only token
storage, scoped `tenant_id`, SET NULL audit-trail survival). New RBAC
permission codes (`shares.create_own`/`shares.manage_own`/`shares.view`)
need no migration of their own -- `identity.bootstrap.seed_rbac` is
idempotent and picks up `identity/rbac.py`'s updated `SEED_PERMISSIONS`/
`SEED_ROLES` automatically on the next app startup, the same as every
prior permission addition to this table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b21f6a3c9d47"
down_revision: str | None = "a09ce853cb0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_DEFAULT = sa.text("(CURRENT_TIMESTAMP)")


def upgrade() -> None:
    op.create_table(
        "conversation_shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "access_mode", sa.String(length=32), nullable=False, server_default="invite_only"
        ),
        sa.Column(
            "default_permission", sa.String(length=32), nullable=False, server_default="viewer"
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("snapshot_message_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_conversation_shares_conversation_id"),
    )
    op.create_index(
        op.f("ix_conversation_shares_owner_user_id"), "conversation_shares", ["owner_user_id"]
    )
    op.create_index(op.f("ix_conversation_shares_tenant_id"), "conversation_shares", ["tenant_id"])

    op.create_table(
        "share_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("share_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("invited_email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="viewer"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("invitation_token_hash", sa.String(length=64), nullable=True),
        sa.Column("invitation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["share_id"], ["conversation_shares.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invitation_token_hash", name="uq_share_members_invitation_token_hash"),
    )
    op.create_index(op.f("ix_share_members_share_id"), "share_members", ["share_id"])
    op.create_index(op.f("ix_share_members_user_id"), "share_members", ["user_id"])

    op.create_table(
        "share_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("share_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["share_id"], ["conversation_shares.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_share_links_token_hash"),
    )
    op.create_index(op.f("ix_share_links_share_id"), "share_links", ["share_id"])
    op.create_index(op.f("ix_share_links_token_hash"), "share_links", ["token_hash"])

    op.create_table(
        "share_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("share_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column(
            "safe_metadata",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["share_id"], ["conversation_shares.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_share_audit_events_share_id"), "share_audit_events", ["share_id"])
    op.create_index(
        op.f("ix_share_audit_events_conversation_id"), "share_audit_events", ["conversation_id"]
    )
    op.create_index(
        op.f("ix_share_audit_events_actor_user_id"), "share_audit_events", ["actor_user_id"]
    )
    op.create_index(op.f("ix_share_audit_events_event_type"), "share_audit_events", ["event_type"])
    op.create_index(op.f("ix_share_audit_events_created_at"), "share_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("share_audit_events")
    op.drop_index(op.f("ix_share_links_token_hash"), table_name="share_links")
    op.drop_index(op.f("ix_share_links_share_id"), table_name="share_links")
    op.drop_table("share_links")
    op.drop_index(op.f("ix_share_members_user_id"), table_name="share_members")
    op.drop_index(op.f("ix_share_members_share_id"), table_name="share_members")
    op.drop_table("share_members")
    op.drop_index(op.f("ix_conversation_shares_tenant_id"), table_name="conversation_shares")
    op.drop_index(op.f("ix_conversation_shares_owner_user_id"), table_name="conversation_shares")
    op.drop_table("conversation_shares")
