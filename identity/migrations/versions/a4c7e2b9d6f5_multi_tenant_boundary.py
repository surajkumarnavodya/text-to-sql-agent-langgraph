"""multi-tenant boundary: tenants table, users.tenant_id, conversations.tenant_id

Revision ID: a4c7e2b9d6f5
Revises: f3a6b9c2d8e1
Create Date: 2026-10-02 00:00:00.000000

Adds `identity/models.py::Tenant` plus the two `tenant_id` columns that
make tenancy a real, server-resolved fact rather than a hardcoded constant
-- Prompt 20 (`20_MULTI_TENANT_ENTERPRISE_ARCHITECTURE_CONTRACT.md`). See
those models' own docstrings for the design rationale.

**Deliberately non-destructive**, per the master contract's own rule
against destructive schema changes without explicit approval:

  - `tenants` is seeded with exactly one row, `security.tenancy
    .DEFAULT_TENANT_ID` (`"default"`), *before* either column is added --
    so the two new foreign keys are satisfiable at the moment they're
    created, with no window in which an existing row violates them.
  - Both columns are added `NOT NULL` with `server_default='default'`,
    which is precisely the value `security.tenancy
    .resolve_actor_tenant_id` already returned for every user before this
    migration existed. An existing single-tenant deployment therefore
    lands in a state byte-for-byte equivalent to its current behavior --
    the upgrade changes no authorization outcome for anybody.
  - The five pre-existing `tenant_id` columns (`conversation_shares`,
    `onboarding_jobs`, `semantic_catalog_entries`,
    `recommendation_records`, `recommendation_feedback_events`) are
    **left exactly as they are**: plain `String(64)`, no foreign key.
    Adding an FK to them would require every historical row's value to
    already exist in `tenants`, which is true today only by luck (they
    all hold `"default"`), and a failed constraint creation mid-upgrade
    on a real deployment is a worse outcome than the modest integrity
    gain. `docs/MULTI_TENANCY.md` records this as a known limitation.

No RBAC migration is needed -- `identity.bootstrap.seed_rbac` is
idempotent and picks up `identity/rbac.py`'s updated
`SEED_PERMISSIONS`/`SEED_ROLES` on the next app startup, the same as every
prior permission addition.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a4c7e2b9d6f5"
down_revision: str | None = "f3a6b9c2d8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_DEFAULT = sa.text("(CURRENT_TIMESTAMP)")
_JSON_TYPE = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")

#: Must stay in sync with `security.tenancy.DEFAULT_TENANT_ID`. Spelled
#: literally here rather than imported: a migration has to keep describing
#: the schema change it actually applied even if that constant is later
#: renamed in application code.
_DEFAULT_TENANT_ID = "default"


def upgrade() -> None:
    tenants = op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_TIMESTAMP_DEFAULT,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_TIMESTAMP_DEFAULT,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", _JSON_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Seeded before the FK columns below exist, so there is never a moment
    # at which an existing `users`/`conversations` row references a missing
    # tenant. Mirrors `identity.bootstrap.ensure_default_tenant`, which
    # keeps a freshly `create_all`-built database (tests) consistent with a
    # migrated one.
    op.bulk_insert(
        tenants,
        [{"id": _DEFAULT_TENANT_ID, "name": "Default", "status": "active", "metadata": None}],
    )

    for table in ("users", "conversations"):
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default=_DEFAULT_TENANT_ID,
            ),
        )
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False)
        # `batch_alter_table`, not a bare `op.create_foreign_key`: SQLite has
        # no `ALTER TABLE ... ADD CONSTRAINT` at all, so the plain form raises
        # `NotImplementedError` and takes the *whole* migration chain down on
        # a SQLite target. That matters even though production is always
        # PostgreSQL (`AUTH_DATABASE_URL`) -- every earlier migration in this
        # chain is deliberately SQLite-compatible (see their
        # `postgresql.JSONB(...).with_variant(sa.JSON(), "sqlite")` type
        # choices), so anyone pointing the chain at a throwaway SQLite file to
        # inspect the resulting schema can keep doing that. On PostgreSQL
        # batch mode emits exactly the same single `ALTER TABLE ... ADD
        # CONSTRAINT`, so this is not a behavior change for the real target.
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_foreign_key(
                f"fk_{table}_tenant_id_tenants",
                "tenants",
                ["tenant_id"],
                ["id"],
                ondelete="RESTRICT",
            )


def downgrade() -> None:
    for table in ("conversations", "users"):
        # Batch mode here for the same reason as `upgrade` above -- SQLite has
        # no `ALTER TABLE ... DROP CONSTRAINT` either.
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_tenant_id_tenants", type_="foreignkey")
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_column(table, "tenant_id")

    op.drop_table("tenants")
