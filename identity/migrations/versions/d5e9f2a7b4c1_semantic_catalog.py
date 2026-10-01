"""tenant-aware semantic catalog: semantic_catalog_entries

Revision ID: d5e9f2a7b4c1
Revises: c4d8e1f6a9b3
Create Date: 2026-10-01 00:00:01.000000

Adds `identity/models.py::SemanticCatalogEntry` -- see that model's own
docstring for the design rationale (one row per version, scoped
`tenant_id`, append-only supersession chain via `supersedes_id`). New RBAC
permission codes (`catalog.manage`/`catalog.review`) need no migration of
their own -- `identity.bootstrap.seed_rbac` is idempotent and picks up
`identity/rbac.py`'s updated `SEED_PERMISSIONS`/`SEED_ROLES` automatically
on the next app startup, the same as every prior permission addition to
this table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d5e9f2a7b4c1"
down_revision: str | None = "c4d8e1f6a9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_DEFAULT = sa.text("(CURRENT_TIMESTAMP)")
_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "semantic_catalog_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("database_id", sa.String(length=200), nullable=False),
        sa.Column("concept_type", sa.String(length=16), nullable=False),
        sa.Column("concept_key", sa.String(length=200), nullable=False),
        sa.Column("business_name", sa.String(length=200), nullable=False),
        sa.Column("technical_name", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("grain", sa.String(length=500), nullable=True),
        sa.Column("keys", _JSON_TYPE, nullable=False),
        sa.Column("relationships", _JSON_TYPE, nullable=False),
        sa.Column("domain", sa.String(length=200), nullable=True),
        sa.Column("synonyms", _JSON_TYPE, nullable=False),
        sa.Column("business_rules", _JSON_TYPE, nullable=False),
        sa.Column("examples", _JSON_TYPE, nullable=False),
        sa.Column("evidence", _JSON_TYPE, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["semantic_catalog_entries.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_semantic_catalog_entries_tenant_id"), "semantic_catalog_entries", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_semantic_catalog_entries_database_id"),
        "semantic_catalog_entries",
        ["database_id"],
    )
    op.create_index(
        op.f("ix_semantic_catalog_entries_concept_type"),
        "semantic_catalog_entries",
        ["concept_type"],
    )
    op.create_index(
        op.f("ix_semantic_catalog_entries_concept_key"),
        "semantic_catalog_entries",
        ["concept_key"],
    )
    op.create_index(
        op.f("ix_semantic_catalog_entries_status"), "semantic_catalog_entries", ["status"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_semantic_catalog_entries_status"), table_name="semantic_catalog_entries")
    op.drop_index(
        op.f("ix_semantic_catalog_entries_concept_key"), table_name="semantic_catalog_entries"
    )
    op.drop_index(
        op.f("ix_semantic_catalog_entries_concept_type"), table_name="semantic_catalog_entries"
    )
    op.drop_index(
        op.f("ix_semantic_catalog_entries_database_id"), table_name="semantic_catalog_entries"
    )
    op.drop_index(
        op.f("ix_semantic_catalog_entries_tenant_id"), table_name="semantic_catalog_entries"
    )
    op.drop_table("semantic_catalog_entries")
