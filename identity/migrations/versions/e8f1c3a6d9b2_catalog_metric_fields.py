"""governed metrics: approved_expression/source_tables/filters/dimensions/aggregation

Revision ID: e8f1c3a6d9b2
Revises: d5e9f2a7b4c1
Create Date: 2026-10-01 00:00:02.000000

Adds five columns to `semantic_catalog_entries` --
`approved_expression`/`source_tables`/`filters`/`dimensions`/
`aggregation` -- Prompt 10 (`10_GOVERNED_METRICS_CONTRACT.md`)'s
governed-metric fields. See `identity/models.py::SemanticCatalogEntry`'s
own docstring and `semantic.catalog.CatalogEntrySnapshot`'s for the
design rationale. All five are nullable/empty-default, so every existing
row (every Prompt-09 entry already in a database) is valid without a
backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8f1c3a6d9b2"
down_revision: str | None = "d5e9f2a7b4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "semantic_catalog_entries", sa.Column("approved_expression", sa.Text(), nullable=True)
    )
    op.add_column(
        "semantic_catalog_entries",
        sa.Column("source_tables", _JSON_TYPE, nullable=False, server_default="[]"),
    )
    op.add_column(
        "semantic_catalog_entries",
        sa.Column("filters", _JSON_TYPE, nullable=False, server_default="[]"),
    )
    op.add_column(
        "semantic_catalog_entries",
        sa.Column("dimensions", _JSON_TYPE, nullable=False, server_default="[]"),
    )
    op.add_column(
        "semantic_catalog_entries", sa.Column("aggregation", sa.String(length=100), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("semantic_catalog_entries", "aggregation")
    op.drop_column("semantic_catalog_entries", "dimensions")
    op.drop_column("semantic_catalog_entries", "filters")
    op.drop_column("semantic_catalog_entries", "source_tables")
    op.drop_column("semantic_catalog_entries", "approved_expression")
