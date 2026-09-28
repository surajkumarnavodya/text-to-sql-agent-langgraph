"""google sign-in: external identities, nullable password_hash

Revision ID: a09ce853cb0d
Revises: a1f3c9d84e21
Create Date: 2026-09-28 00:00:00.000000

Adds what Google sign-in needs on top of the existing local-account schema
(see `identity/models.py::ExternalIdentity`'s own docstring for the full
design rationale):

- `external_identities` -- one row per linked external-provider identity,
  `(provider, provider_subject)` unique so an already-linked identity can
  never be attached to a second local user (a database-enforced invariant,
  not just an application-level check).
- `users.password_hash` relaxed to nullable -- a Google-only account has no
  local password at all until one is explicitly set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a09ce853cb0d"
down_revision: str | None = "a1f3c9d84e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `batch_alter_table` (not a bare `op.alter_column`) so this migration
    # also applies against the SQLite engine some dev/test tooling might
    # point Alembic at directly -- SQLite has no native ALTER COLUMN, and
    # batch mode transparently falls back to its copy-and-move strategy
    # there while still emitting a plain ALTER on every other backend
    # (PostgreSQL in production). The pytest suite itself never runs this
    # migration at all (it builds tables via `Base.metadata.create_all()`
    # against SQLite directly -- see `identity/models.py`'s own module
    # docstring and the prior migration's identical note), so this is
    # defense-in-depth, not something exercised by `pytest` today.
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=True)

    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_subject", sa.String(length=255), nullable=False),
        sa.Column("email_at_link", sa.String(length=320), nullable=True),
        sa.Column(
            "email_verified_at_link", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_subject", name="uq_external_identities_provider_subject"
        ),
    )
    op.create_index(op.f("ix_external_identities_user_id"), "external_identities", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_external_identities_user_id"), table_name="external_identities")
    op.drop_table("external_identities")

    # A Google-only account (password_hash IS NULL) cannot survive this
    # downgrade with a NOT NULL column -- there is no password to backfill.
    # Refusing to silently invent one; an operator running this downgrade
    # must first delete or otherwise handle any such rows.
    bind = op.get_bind()
    orphaned = bind.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE password_hash IS NULL")
    ).scalar()
    if orphaned:
        raise RuntimeError(
            f"Cannot downgrade: {orphaned} user(s) have no password_hash "
            "(Google-only accounts created after this migration was applied). "
            "Delete or assign a password to these accounts before downgrading."
        )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=False)
