"""SQL Server-backed storage for moderation decisions and ingested-asset
metadata (`moderation.media_assets`) -- the single dedupe/audit record for
everything either content-ingestion pipeline (`media/ingest.py` for
images/video, `rag/ingestion.py` for PDFs) has ever seen, keyed by content
hash.

Deliberately a separate connection from `DB_CONNECTIONS` *and* from
`rag_store_connection_string` -- see `Settings.moderation_store_connection_string`'s
docstring for why this isn't just reused from RAG (media search is
independently toggleable from document/policy RAG). Point both connection
strings at the same physical database if you want one metadata store; that's
a deployment choice, not a code-level coupling.

This table is purely additive: it never reads or writes `rag.documents`/
`rag.chunks` (`rag/store.py`'s own tables), which keep tracking what they
already tracked (upload status, chunk count for the Knowledge Sources UI,
sensitivity category) independently of moderation/dedupe concerns.

A rejected asset's row **never** carries the rejected content -- only its
hash, media type, timestamp, and which category(ies) triggered rejection
(`moderation_checks`). `vector_ids` is NULL for a rejected asset (nothing
was ever embedded/stored for it) and a JSON array of the actual Chroma/
`rag.chunks` ids for a passed one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cache
from typing import Literal

from sqlalchemy import Engine, create_engine, text

from config.settings import Settings, get_settings
from moderation.exceptions import ModerationNotConfiguredError

logger = logging.getLogger(__name__)

MediaType = Literal["image", "video", "pdf"]
ModerationStatus = Literal["passed", "rejected"]


@dataclass(frozen=True)
class MediaAssetRecord:
    """One row of `moderation.media_assets` -- what a dedupe lookup returns."""

    asset_id: str
    file_hash: str
    media_type: MediaType
    moderation_status: ModerationStatus
    moderation_checks: dict
    chunk_count: int
    vector_ids: list[str] | None


def _require_connection_string(settings: Settings) -> str:
    if not settings.moderation_store_connection_string:
        raise ModerationNotConfiguredError(
            "Content is being ingested (media search or document/policy RAG) "
            "but MODERATION_STORE_CONNECTION_STRING is not set in .env. Set it "
            "to a SQL Server connection string -- see .env.example's "
            "moderation section. This gate is mandatory, not optional, "
            "whenever ENABLE_MEDIA_SEARCH or ENABLE_DOCUMENT_RAG/"
            "ENABLE_POLICY_RAG is on."
        )
    return settings.moderation_store_connection_string.get_secret_value()


@cache
def _cached_moderation_engine(
    connection_string: str, pool_size: int, max_overflow: int, pool_recycle: int
) -> Engine:
    """Process-wide engine cache, mirroring `db.connection._cached_engine`/
    `rag.store._cached_rag_engine` -- with `pool_size`/`max_overflow`
    explicitly set (unlike either of those, which rely on SQLAlchemy's
    defaults of 5/10), since ingestion can run many concurrent DB writes
    (`Settings.media_ingest_workers` concurrent CLI workers, plus normal
    concurrent PDF-upload request traffic sharing this same pool -- see
    `Settings.moderation_store_pool_size`'s docstring for the sizing
    rationale). Cache key includes the pool kwargs (not just the connection
    string) so a `.env` change to any of them takes effect on process
    restart rather than silently reusing a stale pool configuration.
    """
    return create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def get_moderation_engine(settings: Settings | None = None) -> Engine:
    """Returns the (cached, pooled) engine for the moderation metadata store.

    Raises:
        ModerationNotConfiguredError: if `MODERATION_STORE_CONNECTION_STRING` is unset.
    """
    settings = settings or get_settings()
    connection_string = _require_connection_string(settings)
    return _cached_moderation_engine(
        connection_string,
        settings.moderation_store_pool_size,
        settings.moderation_store_max_overflow,
        settings.moderation_store_pool_recycle_seconds,
    )


def ensure_schema(engine: Engine) -> None:
    """Creates the `moderation` schema and its one table if they don't
    already exist -- idempotent, safe to call on every ingestion entry
    point, the same "just works on first use" posture `rag.store.ensure_schema`
    already has.
    """
    with engine.begin() as conn:
        conn.execute(text("IF SCHEMA_ID('moderation') IS NULL EXEC('CREATE SCHEMA moderation')"))
        conn.execute(
            text(
                """
                IF OBJECT_ID('moderation.media_assets') IS NULL
                CREATE TABLE moderation.media_assets (
                    asset_id UNIQUEIDENTIFIER NOT NULL
                        CONSTRAINT DF_moderation_media_assets_id DEFAULT NEWID(),
                    file_hash NVARCHAR(64) NOT NULL,
                    media_type NVARCHAR(20) NOT NULL,
                    source_path NVARCHAR(1000) NULL,
                    ingested_at DATETIME2 NOT NULL
                        CONSTRAINT DF_moderation_media_assets_ingested_at DEFAULT SYSUTCDATETIME(),
                    moderation_status NVARCHAR(20) NOT NULL,
                    moderation_checks NVARCHAR(MAX) NOT NULL,
                    chunk_count INT NOT NULL CONSTRAINT DF_moderation_media_assets_chunk_count DEFAULT 0,
                    vector_ids NVARCHAR(MAX) NULL,
                    CONSTRAINT PK_moderation_media_assets PRIMARY KEY (asset_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE object_id = OBJECT_ID('moderation.media_assets')
                    AND name = 'UQ_moderation_media_assets_file_hash'
                )
                CREATE UNIQUE INDEX UQ_moderation_media_assets_file_hash
                    ON moderation.media_assets (file_hash)
                """
            )
        )


def get_asset_by_hash(engine: Engine, file_hash: str) -> MediaAssetRecord | None:
    """The dedupe lookup every ingestion call starts with -- a hit means
    moderation and embedding are both skipped entirely (see `media/ingest.py`/
    `rag/ingestion.py`), regardless of whether that prior result was
    "passed" or "rejected"."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT asset_id, file_hash, media_type, moderation_status,
                       moderation_checks, chunk_count, vector_ids
                FROM moderation.media_assets
                WHERE file_hash = :file_hash
                """
            ),
            {"file_hash": file_hash},
        ).fetchone()
    if row is None:
        return None
    return MediaAssetRecord(
        asset_id=str(row.asset_id),
        file_hash=row.file_hash,
        media_type=row.media_type,
        moderation_status=row.moderation_status,
        moderation_checks=json.loads(row.moderation_checks),
        chunk_count=row.chunk_count,
        vector_ids=json.loads(row.vector_ids) if row.vector_ids else None,
    )


def record_asset(
    engine: Engine,
    file_hash: str,
    media_type: MediaType,
    moderation_status: ModerationStatus,
    moderation_checks: dict,
    *,
    source_path: str | None = None,
    chunk_count: int = 0,
    vector_ids: list[str] | None = None,
) -> str:
    """Upserts one `moderation.media_assets` row (by `file_hash`) and
    returns its `asset_id`.

    Called once per genuinely-new asset (never for a plain dedupe hit --
    see `get_asset_by_hash`, which short-circuits before this is reached),
    either right after a hard-reject (`vector_ids` always None,
    `moderation_checks` holds only category/severity metadata, never
    content) or right after a successful embed+store (`vector_ids` holds
    the real Chroma/`rag.chunks` ids just written).

    Deliberately delete-then-insert (not a bare `INSERT`, which would hit
    the unique index on `file_hash`) so a `force=True` re-run -- which
    intentionally bypasses the dedupe short-circuit to re-moderate/
    re-embed -- replaces the same content's old record rather than
    failing on a duplicate key. This mirrors `media/store.py`'s own
    "re-ingesting identical content is an idempotent upsert, not a
    duplicate" convention.
    """
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM moderation.media_assets WHERE file_hash = :file_hash"),
            {"file_hash": file_hash},
        )
        result = conn.execute(
            text(
                """
                INSERT INTO moderation.media_assets
                    (file_hash, media_type, source_path, moderation_status,
                     moderation_checks, chunk_count, vector_ids)
                OUTPUT inserted.asset_id
                VALUES (:file_hash, :media_type, :source_path, :moderation_status,
                        :moderation_checks, :chunk_count, :vector_ids)
                """
            ),
            {
                "file_hash": file_hash,
                "media_type": media_type,
                "source_path": source_path,
                "moderation_status": moderation_status,
                "moderation_checks": json.dumps(moderation_checks),
                "chunk_count": chunk_count,
                "vector_ids": json.dumps(vector_ids) if vector_ids is not None else None,
            },
        )
        return str(result.scalar_one())
