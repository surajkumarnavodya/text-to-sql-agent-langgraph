"""recommendation actions: owner assignment + typed feedback events

Revision ID: b7e2d4a1c9f0
Revises: a4c7e2b9d6f5
Create Date: 2026-10-03 00:00:00.000000

Prompt 31 (`31_RECOMMENDATION_ACTION_DASHBOARD_CONTRACT.md`). Purely additive:

- `recommendation_records.owner_user_id` -- who is currently responsible for
  acting on a recommendation (nullable; `NULL` means unassigned).
- `recommendation_feedback_events.event_type` -- `status_change` (every row
  that existed before this migration, via the server default), `note`, or
  `owner_assigned`. Lets the one append-only audit trail also carry notes
  and ownership changes without inventing a second audit table.
- `recommendation_feedback_events.detail` -- nullable JSON for the typed
  payload of a non-status event (e.g. the new owner's id).

No existing row is rewritten, no column is dropped, and `downgrade()`
removes only what `upgrade()` added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7e2d4a1c9f0"
down_revision: str | None = "a4c7e2b9d6f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "recommendation_records",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_recommendation_records_owner_user_id_users",
        "recommendation_records",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_recommendation_records_owner_user_id"),
        "recommendation_records",
        ["owner_user_id"],
    )

    op.add_column(
        "recommendation_feedback_events",
        sa.Column(
            "event_type",
            sa.String(length=24),
            nullable=False,
            server_default="status_change",
        ),
    )
    op.add_column(
        "recommendation_feedback_events",
        sa.Column("detail", _JSON_TYPE, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recommendation_feedback_events", "detail")
    op.drop_column("recommendation_feedback_events", "event_type")
    op.drop_index(
        op.f("ix_recommendation_records_owner_user_id"), table_name="recommendation_records"
    )
    op.drop_constraint(
        "fk_recommendation_records_owner_user_id_users",
        "recommendation_records",
        type_="foreignkey",
    )
    op.drop_column("recommendation_records", "owner_user_id")
