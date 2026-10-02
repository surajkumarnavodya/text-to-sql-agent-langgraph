"""governed recommendation lifecycle: recommendation_records, recommendation_feedback_events

Revision ID: f3a6b9c2d8e1
Revises: e8f1c3a6d9b2
Create Date: 2026-10-02 00:00:00.000000

Adds `identity/models.py::RecommendationRecord`/`RecommendationFeedbackEvent`
-- Prompt 18 (`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`). See those
models' own docstrings for the design rationale (immutable recommendation
content + mutable `status`, an append-only feedback/audit event log,
scoped `tenant_id`). New RBAC permission codes
(`recommendation.review`/`recommendation.manage`) need no migration of
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
revision: str = "f3a6b9c2d8e1"
down_revision: str | None = "e8f1c3a6d9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_DEFAULT = sa.text("(CURRENT_TIMESTAMP)")
_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "recommendation_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("database_id", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("rule_or_model", sa.String(length=200), nullable=True),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("affected_entity", sa.String(length=500), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("measurable_impact", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence", _JSON_TYPE, nullable=False),
        sa.Column("limitations", _JSON_TYPE, nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("evidence_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="generated"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_question", sa.Text(), nullable=True),
        sa.Column("source_sql", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recommendation_records_tenant_id"), "recommendation_records", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_recommendation_records_database_id"), "recommendation_records", ["database_id"]
    )
    op.create_index(
        op.f("ix_recommendation_records_category"), "recommendation_records", ["category"]
    )
    op.create_index(op.f("ix_recommendation_records_status"), "recommendation_records", ["status"])
    op.create_index(
        op.f("ix_recommendation_records_created_by_user_id"),
        "recommendation_records",
        ["created_by_user_id"],
    )

    op.create_table(
        "recommendation_feedback_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.String(length=200), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("recommendation_version", sa.String(length=32), nullable=False),
        sa.Column("evidence_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"], ["recommendation_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recommendation_feedback_events_recommendation_id"),
        "recommendation_feedback_events",
        ["recommendation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_recommendation_feedback_events_recommendation_id"),
        table_name="recommendation_feedback_events",
    )
    op.drop_table("recommendation_feedback_events")
    op.drop_index(
        op.f("ix_recommendation_records_created_by_user_id"), table_name="recommendation_records"
    )
    op.drop_index(op.f("ix_recommendation_records_status"), table_name="recommendation_records")
    op.drop_index(op.f("ix_recommendation_records_category"), table_name="recommendation_records")
    op.drop_index(
        op.f("ix_recommendation_records_database_id"), table_name="recommendation_records"
    )
    op.drop_index(op.f("ix_recommendation_records_tenant_id"), table_name="recommendation_records")
    op.drop_table("recommendation_records")
