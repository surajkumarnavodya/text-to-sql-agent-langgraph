"""Typed chunk model + deterministic ID/content-hash generation.

Every piece of business context this package can retrieve -- a table, a
column, a relationship, a glossary term, a metric, a curated SQL example, or
a documentation section -- is represented as one `Chunk`, not as seven
near-duplicate classes: `chunk_type` is the discriminator, and
type-specific structured fields (a metric's formula, a relationship's join
condition, ...) live in `extra`, a plain `dict`. This keeps one storage
shape (one Chroma collection, one upsert/query code path in
`vector_store.py`) while still giving every caller (the reranker, the
retrieval node, a test) a typed, documented field to filter/group on for
the fields that matter across *every* chunk type (source identity,
sensitivity, role scoping, versioning).

Stable IDs, not random UUIDs: `make_chunk_id` hashes a chunk's *identity*
(database, schema, object, type, column, version) -- never its content. Two
ingestion runs over an unchanged schema/knowledge file produce byte-identical
IDs, which is what makes `ingestion.py`'s upsert-by-id re-ingestion
idempotent rather than accumulating duplicates on every run. `content_hash`
is the separate, content-derived hash `ingestion.py` diffs against to decide
whether a chunk with an unchanged *identity* still needs re-embedding.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChunkType(str, Enum):
    """The seven typed-chunk categories this package retrieves.

    Deliberately a closed set (not a free-form string) -- `retriever.py`'s
    per-type top-k limits and `reranker.py`'s type-priority weighting both
    key off of this, and an unrecognized type would silently fall through
    both.
    """

    TABLE = "table"
    COLUMN = "column"
    RELATIONSHIP = "relationship"
    GLOSSARY = "glossary"
    METRIC = "metric"
    SQL_EXAMPLE = "sql_example"
    DOCUMENTATION = "documentation"


class Sensitivity(str, Enum):
    """Mirrors `rag.store.SensitivityCategory`'s three-tier shape (see that
    module's docstring) -- applied here to *business-context* chunks
    (a glossary term, a metric definition) rather than document chunks, but
    the same fail-closed principle: `retriever.py` never returns a
    `RESTRICTED` chunk to a caller whose `allowed_roles` don't clear it.
    """

    NORMAL = "normal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


def compute_content_hash(text: str, extra: dict[str, Any] | None = None) -> str:
    """Deterministic SHA-256 of a chunk's *content* -- text plus its typed extras.

    Used by `ingestion.py` to detect an unchanged chunk (same `chunk_id`,
    same `content_hash` as what's already stored) and skip re-embedding it --
    the core of idempotent re-ingestion. `json.dumps(..., sort_keys=True)`
    on `extra` guarantees the hash doesn't depend on dict insertion order.
    """
    canonical_extra = json.dumps(extra or {}, sort_keys=True, default=str)
    digest_input = f"{text}\x00{canonical_extra}".encode()
    return hashlib.sha256(digest_input).hexdigest()


def make_chunk_id(
    database_id: str,
    schema_name: str | None,
    object_name: str,
    chunk_type: ChunkType,
    column_name: str | None = None,
    version: int = 1,
) -> str:
    """Deterministic chunk ID from stable *identity* fields -- never content.

    Hash input, in order: database ID, schema name, object name, chunk type,
    column name, version -- exactly the field list this package's own design
    doc specifies, so a chunk's ID is reproducible from its identity alone
    across process restarts and separate ingestion runs (unlike a random
    UUID, which would make every re-ingestion look like a brand-new chunk to
    `vector_store.py`'s upsert-by-id logic, defeating idempotency entirely).
    """
    parts = [
        database_id,
        schema_name or "",
        object_name,
        chunk_type.value,
        column_name or "",
        str(version),
    ]
    digest_input = "\x00".join(parts).encode()
    return hashlib.sha256(digest_input).hexdigest()


class Chunk(BaseModel):
    """One retrievable unit of business context, of exactly one `chunk_type`.

    `text` is both what gets embedded (via `embeddings.py`) and what gets
    rendered into the SQL-generation prompt if this chunk is retrieved --
    `chunking.py` is responsible for making it self-contained and readable
    on its own, since the LLM never sees `extra` directly.

    `extra` carries whatever additional structured fields are specific to
    this chunk's `chunk_type` (e.g. a relationship's `source_table`/
    `target_table`/`join_condition`, a metric's `formula`/`grain`, a SQL
    example's `tables_used`/`validation_status`) -- see `chunking.py`'s
    per-type builder functions for exactly what each type populates.
    """

    model_config = ConfigDict(frozen=True, use_enum_values=False)

    chunk_id: str
    chunk_type: ChunkType
    text: str

    database_id: str
    schema_name: str | None = None
    table_name: str | None = None
    column_name: str | None = None

    source_id: str
    content_hash: str
    version: int = 1

    sensitivity: Sensitivity = Sensitivity.NORMAL
    tags: tuple[str, ...] = ()
    # Empty tuple means "visible regardless of caller role" -- the common
    # case for schema-derived chunks (table/column/relationship), which
    # carry no business-defined access restriction of their own. Non-empty
    # means the caller must hold at least one of these roles (see
    # `retriever.py`'s `_filter_by_role` -- checked against
    # `AgentState["caller_roles"]`, never a client-supplied filter).
    allowed_roles: tuple[str, ...] = ()

    embedding_model: str
    embedding_dimensions: int
    source_updated_at: str | None = None

    extra: dict[str, Any] = Field(default_factory=dict)

    def is_visible_to(self, caller_roles: tuple[str, ...]) -> bool:
        """Role-scoping check -- see `allowed_roles`'s docstring.

        Deliberately does not consider `sensitivity` on its own: a
        `RESTRICTED` chunk with no explicit `allowed_roles` is still
        visible to everyone by this check alone (schema-derived chunks are
        never marked restricted in practice, since `chunking.py` only sets
        `sensitivity` from `config/sensitive_columns.py`, which already has
        its own separate, stricter enforcement at `validate_sql_node` --
        see `docs/vector-retrieval-design.md`'s security section for why
        this layer is a *retrieval-time* hint, not the safety boundary).
        """
        if not self.allowed_roles:
            return True
        return any(role in self.allowed_roles for role in caller_roles)


class ScoredChunk(BaseModel):
    """One `Chunk` plus how it scored against a specific query.

    `vector_similarity` is the raw score from `vector_store.py`
    (0..1 for cosine, as configured -- see that module's docstring);
    `final_score` is what `reranker.py` computes on top of it (similarity +
    type-priority + exact-term-match bonus - diversity penalty). Retrieval
    results are always ordered by `final_score` once `reranker.py` has run;
    `vector_similarity` is kept alongside purely for observability/tests.
    """

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    vector_similarity: float
    final_score: float | None = None

    def to_context_dict(self) -> dict[str, Any]:
        """Renders this result as one entry of `AgentState["retrieved_context"]`.

        A plain, JSON-serializable dict (not the pydantic model itself) --
        `AgentState` is a `TypedDict`, and every other list-of-dict field on
        it (`TableSchema`, `AttemptRecord`, ...) is a plain dict shape too,
        not a pydantic model, so this matches that convention.
        """
        return {
            "chunk_id": self.chunk.chunk_id,
            "chunk_type": self.chunk.chunk_type.value,
            "text": self.chunk.text,
            "table_name": self.chunk.table_name,
            "column_name": self.chunk.column_name,
            "source_id": self.chunk.source_id,
            "tags": list(self.chunk.tags),
            "sensitivity": self.chunk.sensitivity.value,
            "vector_similarity": self.vector_similarity,
            "final_score": self.final_score,
        }
