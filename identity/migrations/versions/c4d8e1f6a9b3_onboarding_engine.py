"""client-database onboarding engine: jobs, review items, artifacts

Revision ID: c4d8e1f6a9b3
Revises: b21f6a3c9d47
Create Date: 2026-10-01 00:00:00.000000

Adds the three tables `identity/models.py::OnboardingJob`/
`OnboardingReviewItem`/`OnboardingArtifact` document in full -- see each
model's own docstring for the design rationale (never persisting a
connection secret, scoped `tenant_id`, versioned artifacts). New RBAC
permission codes (`onboarding.manage`/`onboarding.review`) need no
migration of their own -- `identity.bootstrap.seed_rbac` is idempotent and
picks up `identity/rbac.py`'s updated `SEED_PERMISSIONS`/`SEED_ROLES`
automatically on the next app startup, the same as every prior permission
addition to this table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d8e1f6a9b3"
down_revision: str | None = "b21f6a3c9d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_DEFAULT = sa.text("(CURRENT_TIMESTAMP)")
_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "onboarding_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("database_label", sa.String(length=200), nullable=False),
        sa.Column("db_type", sa.String(length=32), nullable=False),
        sa.Column("db_host", sa.String(length=255), nullable=True),
        sa.Column("db_port", sa.Integer(), nullable=True),
        sa.Column("db_name", sa.String(length=200), nullable=True),
        sa.Column("db_user", sa.String(length=200), nullable=True),
        sa.Column("db_schema", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("current_stage", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovery_summary", _JSON_TYPE, nullable=True),
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
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_onboarding_jobs_tenant_id"), "onboarding_jobs", ["tenant_id"])
    op.create_index(
        op.f("ix_onboarding_jobs_created_by_user_id"), "onboarding_jobs", ["created_by_user_id"]
    )

    op.create_table(
        "onboarding_review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("table_name", sa.String(length=200), nullable=True),
        sa.Column("column_name", sa.String(length=200), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("payload", _JSON_TYPE, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("is_ambiguous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("decision", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["onboarding_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_onboarding_review_items_job_id"), "onboarding_review_items", ["job_id"]
    )

    op.create_table(
        "onboarding_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("content", _JSON_TYPE, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_TIMESTAMP_DEFAULT,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["onboarding_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_onboarding_artifacts_job_id"), "onboarding_artifacts", ["job_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_onboarding_artifacts_job_id"), table_name="onboarding_artifacts")
    op.drop_table("onboarding_artifacts")
    op.drop_index(op.f("ix_onboarding_review_items_job_id"), table_name="onboarding_review_items")
    op.drop_table("onboarding_review_items")
    op.drop_index(op.f("ix_onboarding_jobs_created_by_user_id"), table_name="onboarding_jobs")
    op.drop_index(op.f("ix_onboarding_jobs_tenant_id"), table_name="onboarding_jobs")
    op.drop_table("onboarding_jobs")
