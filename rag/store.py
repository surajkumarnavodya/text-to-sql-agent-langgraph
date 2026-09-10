"""SQL Server-backed storage for document/policy chunks and their embeddings.

Uses SQL Server's native `VECTOR` column type (SQL Server 2025+ / Azure SQL
-- confirmed against the actual target instance before this was written, not
assumed; see `VECTOR_DISTANCE('cosine', ...)` below) rather than a second
vector-search product. Two tables, one schema (`rag`), shared by both the
"documents" and "policies" collections -- collection is a column
(`documents.collection`), not a separate table pair, since the two
collections are structurally identical and only differ in access-sensitivity
handling (see `sensitivity_category` below).

Schema:
    rag.documents -- one row per ingested PDF: filename, which collection,
        upload/processing status, chunk count, and (for "policies" only) a
        hand-set sensitivity category used to gate generation in
        `rag/graph.py`.
    rag.chunks -- one row per chunk: text, position, embedding (VECTOR(384)
        -- fixed to the default embedding model's dimensionality; changing
        `EMBEDDING_MODEL_NAME`/`RAG_EMBEDDING_MODEL_NAME` to a model with a
        different output size requires migrating this column's width too,
        a deliberate, documented limitation rather than a dynamic schema).

Deliberately a separate connection from `DB_CONNECTIONS` (`Settings.databases`)
-- see `Settings.rag_store_connection_string`'s docstring for why chunk/
embedding storage shouldn't share a schema or connection pool with a
configured business database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cache
from typing import Literal

from sqlalchemy import Engine, create_engine, text

from config.settings import ConfigurationError, Settings, get_settings

logger = logging.getLogger(__name__)

Collection = Literal["documents", "policies"]
DocumentStatus = Literal["processing", "ready", "failed"]

# The three categories signed off on for restricted-content treatment in
# the "policies" collection (see docs/ARCHITECTURE.md's multi-source
# section once it lands, and the sign-off in this project's own history --
# compensation & pay, disciplinary/HR case content, legal/litigation).
# None means "no restricted category" -- the common case for both
# "documents" uploads (never restricted) and most policy uploads.
SensitivityCategory = Literal["compensation", "disciplinary", "legal"] | None

_VALID_SENSITIVITY_CATEGORIES: frozenset[str] = frozenset({"compensation", "disciplinary", "legal"})

# Fixed to all-MiniLM-L6-v2's (and Chroma's DefaultEmbeddingFunction's, which
# is the same model) output size -- see this module's docstring.
EMBEDDING_DIMENSIONS = 384

# A 384-float JSON array is long enough (~7000+ characters) that pyodbc
# binds it as SQL_WLONGVARCHAR (ntext) rather than SQL_WVARCHAR (nvarchar) --
# a driver-level heuristic based on string length, not something this code
# controls per-parameter. SQL Server's VECTOR CAST only accepts a
# varchar/nvarchar source, not ntext ("Explicit conversion from data type
# ntext to vector is not allowed") -- confirmed against the real error, not
# assumed. Casting through NVARCHAR(MAX) first forces the intermediate type
# pyodbc's automatic binding skips, and *that* cast to VECTOR is accepted.
_VECTOR_CAST = f"CAST(CAST(:embedding AS NVARCHAR(MAX)) AS VECTOR({EMBEDDING_DIMENSIONS}))"


class RagStoreNotConfiguredError(ConfigurationError):
    """Raised when document/policy RAG is enabled but `RAG_STORE_CONNECTION_STRING` isn't set.

    A distinct subclass (not just a bare `ConfigurationError`) so callers
    that want to degrade gracefully (e.g. the router's availability check)
    can catch this specifically rather than any configuration problem.
    """


@dataclass(frozen=True)
class DocumentRecord:
    """One ingested PDF, as listed in the "Knowledge Sources" management view."""

    id: str
    filename: str
    collection: Collection
    sensitivity_category: str | None
    upload_date: str
    status: DocumentStatus
    chunk_count: int
    error_message: str | None


@dataclass(frozen=True)
class ChunkResult:
    """One retrieved chunk, with enough provenance to cite it in a generated answer."""

    chunk_text: str
    similarity: float
    document_id: str
    filename: str
    chunk_index: int
    page_number: int | None
    sensitivity_category: str | None


def _require_connection_string(settings: Settings) -> str:
    if not settings.rag_store_connection_string:
        raise RagStoreNotConfiguredError(
            "Document/policy RAG is enabled (ENABLE_DOCUMENT_RAG or "
            "ENABLE_POLICY_RAG) but RAG_STORE_CONNECTION_STRING is not set in "
            ".env. Set it to a SQL Server 2025+/Azure SQL connection string -- "
            "see .env.example's RAG section."
        )
    return str(settings.rag_store_connection_string)


@cache
def _cached_rag_engine(connection_string: str) -> Engine:
    """Process-wide engine cache, mirroring `db.connection._cached_engine`."""
    return create_engine(connection_string, pool_pre_ping=True, pool_recycle=1800)


def get_rag_engine(settings: Settings | None = None) -> Engine:
    """Returns the (cached, pooled) engine for the RAG store connection.

    Raises:
        RagStoreNotConfiguredError: if `RAG_STORE_CONNECTION_STRING` is unset.
    """
    settings = settings or get_settings()
    connection_string = _require_connection_string(settings)
    return _cached_rag_engine(connection_string)


def ensure_schema(engine: Engine) -> None:
    """Creates the `rag` schema and its two tables if they don't already exist.

    Idempotent (`IF ... IS NULL CREATE ...`) -- safe to call on every
    ingestion/retrieval entry point rather than requiring a separate manual
    migration step, the same "just works on first use" posture
    `db.schema_introspection` and `embeddings.schema_indexer` already have
    for their own storage.
    """
    with engine.begin() as conn:
        conn.execute(text("IF SCHEMA_ID('rag') IS NULL EXEC('CREATE SCHEMA rag')"))
        conn.execute(
            text(
                """
                IF OBJECT_ID('rag.documents') IS NULL
                CREATE TABLE rag.documents (
                    id UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_rag_documents_id DEFAULT NEWID(),
                    filename NVARCHAR(400) NOT NULL,
                    collection NVARCHAR(50) NOT NULL,
                    sensitivity_category NVARCHAR(50) NULL,
                    upload_date DATETIME2 NOT NULL
                        CONSTRAINT DF_rag_documents_upload_date DEFAULT SYSUTCDATETIME(),
                    status NVARCHAR(20) NOT NULL
                        CONSTRAINT DF_rag_documents_status DEFAULT 'processing',
                    chunk_count INT NOT NULL CONSTRAINT DF_rag_documents_chunk_count DEFAULT 0,
                    error_message NVARCHAR(MAX) NULL,
                    source_metadata NVARCHAR(MAX) NULL,
                    CONSTRAINT PK_rag_documents PRIMARY KEY (id)
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                IF OBJECT_ID('rag.chunks') IS NULL
                CREATE TABLE rag.chunks (
                    id UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_rag_chunks_id DEFAULT NEWID(),
                    document_id UNIQUEIDENTIFIER NOT NULL,
                    chunk_index INT NOT NULL,
                    chunk_text NVARCHAR(MAX) NOT NULL,
                    embedding VECTOR({EMBEDDING_DIMENSIONS}) NOT NULL,
                    page_number INT NULL,
                    metadata NVARCHAR(MAX) NULL,
                    CONSTRAINT PK_rag_chunks PRIMARY KEY (id),
                    CONSTRAINT FK_rag_chunks_document FOREIGN KEY (document_id)
                        REFERENCES rag.documents(id) ON DELETE CASCADE
                )
                """
            )
        )


def insert_document(
    engine: Engine,
    filename: str,
    collection: Collection,
    sensitivity_category: SensitivityCategory = None,
    source_metadata: dict | None = None,
) -> str:
    """Inserts a new `rag.documents` row with status "processing" and returns its id.

    Raises:
        ValueError: if `sensitivity_category` isn't one of the reviewed
            categories (or None) -- fails fast rather than silently storing
            an uncategorized value that `rag/graph.py`'s restriction check
            would then never match.
    """
    if (
        sensitivity_category is not None
        and sensitivity_category not in _VALID_SENSITIVITY_CATEGORIES
    ):
        raise ValueError(
            f"sensitivity_category={sensitivity_category!r} is not one of "
            f"{sorted(_VALID_SENSITIVITY_CATEGORIES)} (or None)."
        )
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO rag.documents (filename, collection, sensitivity_category, source_metadata)
                OUTPUT inserted.id
                VALUES (:filename, :collection, :sensitivity_category, :source_metadata)
                """
            ),
            {
                "filename": filename,
                "collection": collection,
                "sensitivity_category": sensitivity_category,
                "source_metadata": json.dumps(source_metadata) if source_metadata else None,
            },
        )
        return str(result.scalar_one())


def update_document_status(
    engine: Engine,
    document_id: str,
    status: DocumentStatus,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Updates a document's status after ingestion succeeds or fails."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE rag.documents
                SET status = :status,
                    chunk_count = COALESCE(:chunk_count, chunk_count),
                    error_message = :error_message
                WHERE id = :document_id
                """
            ),
            {
                "status": status,
                "chunk_count": chunk_count,
                "error_message": error_message,
                "document_id": document_id,
            },
        )


def insert_chunks(
    engine: Engine,
    document_id: str,
    chunks: list[tuple[str, int, list[float], int | None]],
) -> None:
    """Bulk-inserts chunks for one document.

    Args:
        chunks: `(chunk_text, chunk_index, embedding, page_number)` tuples,
            already embedded (see `rag/ingestion.py`) -- this function is
            pure storage, it never calls an embedding model itself.
    """
    with engine.begin() as conn:
        for chunk_text_value, chunk_index, embedding, page_number in chunks:
            conn.execute(
                text(
                    f"""
                    INSERT INTO rag.chunks (document_id, chunk_index, chunk_text, embedding, page_number)
                    VALUES (:document_id, :chunk_index, :chunk_text,
                            {_VECTOR_CAST}, :page_number)
                    """
                ),
                {
                    "document_id": document_id,
                    "chunk_index": chunk_index,
                    "chunk_text": chunk_text_value,
                    "embedding": json.dumps(embedding),
                    "page_number": page_number,
                },
            )


def list_documents(engine: Engine, collection: Collection | None = None) -> list[DocumentRecord]:
    """Lists ingested documents, newest first -- the management view's data source."""
    query = """
        SELECT id, filename, collection, sensitivity_category, upload_date,
               status, chunk_count, error_message
        FROM rag.documents
    """
    params: dict[str, str] = {}
    if collection is not None:
        query += " WHERE collection = :collection"
        params["collection"] = collection
    query += " ORDER BY upload_date DESC"

    with engine.connect() as conn:
        rows = conn.execute(text(query), params).fetchall()
    return [
        DocumentRecord(
            id=str(row.id),
            filename=row.filename,
            collection=row.collection,
            sensitivity_category=row.sensitivity_category,
            upload_date=str(row.upload_date),
            status=row.status,
            chunk_count=row.chunk_count,
            error_message=row.error_message,
        )
        for row in rows
    ]


def delete_document(engine: Engine, document_id: str) -> None:
    """Deletes a document and its chunks (ON DELETE CASCADE handles the chunks)."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM rag.documents WHERE id = :document_id"), {"document_id": document_id}
        )


def similarity_search(
    engine: Engine,
    collection: Collection,
    query_embedding: list[float],
    top_k: int,
) -> list[ChunkResult]:
    """Top-k chunks in `collection` by cosine similarity to `query_embedding`.

    Only chunks belonging to a `status = 'ready'` document are searched -- a
    still-processing or failed ingestion never contributes a half-indexed
    chunk to a live question.

    `VECTOR_DISTANCE('cosine', ...)` returns a *distance* (0 = identical);
    `similarity = 1 - distance` mirrors `embeddings/retriever.py`'s own
    cosine-distance-to-similarity convention for schema retrieval, so a
    "higher is better" score means the same thing in both retrieval paths.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT TOP (:top_k)
                    c.chunk_text, c.chunk_index, c.page_number,
                    d.id AS document_id, d.filename, d.sensitivity_category,
                    VECTOR_DISTANCE('cosine', c.embedding, {_VECTOR_CAST}) AS distance
                FROM rag.chunks c
                JOIN rag.documents d ON d.id = c.document_id
                WHERE d.collection = :collection AND d.status = 'ready'
                ORDER BY distance ASC
                """
            ),
            {
                "top_k": top_k,
                "collection": collection,
                "embedding": json.dumps(query_embedding),
            },
        ).fetchall()
    return [
        ChunkResult(
            chunk_text=row.chunk_text,
            similarity=1.0 - float(row.distance),
            document_id=str(row.document_id),
            filename=row.filename,
            chunk_index=row.chunk_index,
            page_number=row.page_number,
            sensitivity_category=row.sensitivity_category,
        )
        for row in rows
    ]
