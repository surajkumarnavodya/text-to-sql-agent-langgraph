"""chat history: ordering, archiving, and search support

Revision ID: a1f3c9d84e21
Revises: 9c7ccf95397a
Create Date: 2026-09-18 10:00:00.000000

Adds what `identity/repositories/history.py` needs to serve conversations/
messages as real, universal, server-side chat history (previously these
tables -- `conversations`/`prompts`/`ai_outputs` -- existed in the initial
schema but were never populated by any code path):

- `conversations.last_message_at` / `archived_at` -- see
  `identity/models.py::Conversation`'s own docstring for why these are
  separate columns rather than reusing `updated_at`/`status`.
- `prompts.sequence_number` / `ai_outputs.sequence_number` -- deterministic
  per-conversation message ordering (see `Prompt.sequence_number`'s own
  docstring), assigned server-side by `identity.repositories.history
  .append_turn`, never by a client-supplied value.
- PostgreSQL `pg_trgm` extension + GIN trigram indexes on the three
  searchable text columns (`conversations.title`, `prompts.final_content`,
  `ai_outputs.content`) -- accelerates the `ILIKE '%term%'` queries
  `identity/repositories/history.py::search_history` issues (a plain
  B-tree index cannot serve a leading-wildcard `LIKE`/`ILIKE` at all).
  Skipped entirely on a non-PostgreSQL bind (this repo's own SQLite test
  engine, `tests/test_identity_repository_users.py`'s own pattern) --
  `pg_trgm`/`CREATE INDEX ... USING GIN` are PostgreSQL-specific syntax
  with no SQLite equivalent, and the ORM-level `.ilike()` queries
  themselves already work correctly on SQLite without any index at all
  (see this repo's own `Base.metadata.create_all()`-based test setup,
  which never runs this migration in the first place -- this guard is
  defense-in-depth for a future test harness that does apply migrations,
  not something exercised by `pytest` today).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f3c9d84e21"
down_revision: str | None = "9c7ccf95397a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "prompts",
        sa.Column("sequence_number", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ai_outputs",
        sa.Column("sequence_number", sa.Integer(), nullable=False, server_default="0"),
    )
    # Backfill: this table has never been written to by any code path
    # before this feature (see this file's own module docstring), so a
    # sequence_number backfill has no real rows to touch today -- this
    # UPDATE is a documented no-op safety net, not dead weight, in case a
    # deployment somehow already has rows here.
    op.execute("UPDATE prompts SET sequence_number = 0 WHERE sequence_number IS NULL")
    op.execute("UPDATE ai_outputs SET sequence_number = 0 WHERE sequence_number IS NULL")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_conversations_title_trgm "
            "ON conversations USING GIN (title gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_prompts_final_content_trgm "
            "ON prompts USING GIN (final_content gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_ai_outputs_content_trgm "
            "ON ai_outputs USING GIN (content gin_trgm_ops)"
        )

    # Recommended composite indexes for the actual query shapes
    # identity/repositories/history.py issues (list-by-user-ordered-by-
    # recency, and per-conversation message ordering) -- conversations.user_id
    # and prompts/ai_outputs.conversation_id already have a plain index from
    # the initial migration; these add the *composite* shape the real
    # queries filter+sort on.
    op.create_index(
        "ix_conversations_user_last_message",
        "conversations",
        ["user_id", "last_message_at"],
    )
    op.create_index(
        "ix_prompts_conversation_sequence",
        "prompts",
        ["conversation_id", "sequence_number"],
    )
    op.create_index(
        "ix_ai_outputs_conversation_sequence",
        "ai_outputs",
        ["conversation_id", "sequence_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_outputs_conversation_sequence", table_name="ai_outputs")
    op.drop_index("ix_prompts_conversation_sequence", table_name="prompts")
    op.drop_index("ix_conversations_user_last_message", table_name="conversations")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_ai_outputs_content_trgm")
        op.execute("DROP INDEX IF EXISTS ix_prompts_final_content_trgm")
        op.execute("DROP INDEX IF EXISTS ix_conversations_title_trgm")
        # Deliberately does NOT `DROP EXTENSION pg_trgm` -- another database
        # object outside this migration's own scope could depend on it, and
        # dropping a shared extension is a decision an operator should make
        # explicitly, not something a single feature's downgrade does silently.

    op.drop_column("ai_outputs", "sequence_number")
    op.drop_column("prompts", "sequence_number")
    op.drop_column("conversations", "archived_at")
    op.drop_column("conversations", "last_message_at")
