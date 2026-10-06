"""semantic intelligence findings

Revision ID: a1f5c9e3b7d2
Revises: b7e2d4a1c9f0
Create Date: 2026-10-06 00:00:00.000000

Adds `identity/models.py::SemanticFinding` -- Prompt 35
(semantic-intelligence review queue). Additive only: one new table with a
unique `(tenant_id, database_id, finding_key)` constraint and tenant/database
indexes. Nothing existing is altered, so the migration is safe on a populated
database and its downgrade drops only the table it created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1f5c9e3b7d2"
down_revision: str | None = "b7e2d4a1c9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "semantic_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("database_id", sa.String(length=200), nullable=False),
        sa.Column("finding_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", _JSON_TYPE, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "truth_level", sa.String(length=32), nullable=False, server_default="ai_inference"
        ),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("risk_tier", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("history", _JSON_TYPE, nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "database_id", "finding_key", name="uq_semantic_findings_scope_key"
        ),
    )
    op.create_index("ix_semantic_findings_tenant_id", "semantic_findings", ["tenant_id"])
    op.create_index("ix_semantic_findings_database_id", "semantic_findings", ["database_id"])


def downgrade() -> None:
    op.drop_index("ix_semantic_findings_database_id", table_name="semantic_findings")
    op.drop_index("ix_semantic_findings_tenant_id", table_name="semantic_findings")
    op.drop_table("semantic_findings")
