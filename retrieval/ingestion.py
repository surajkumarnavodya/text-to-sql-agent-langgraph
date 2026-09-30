"""Idempotent discover -> chunk -> hash -> embed -> upsert -> delete-stale pipeline.

`run_ingestion()` is safe to run repeatedly (a cron job, a CI step, a
developer re-running it after editing a knowledge YAML file): every chunk
has a deterministic `chunk_id` (`retrieval.models.make_chunk_id`), so a
second run over unchanged schema/knowledge content upserts the exact same
IDs with the exact same `content_hash` -- detected and skipped, not
re-embedded, not duplicated (see `_diff_chunks`).

**Full-collection deletion only happens in `rebuild_collection()`**, never
in `run_ingestion()` -- normal ingestion only ever deletes the specific
stale chunk IDs a source no longer produces (e.g. a documentation file that
shrank from 5 sections to 3, or a glossary term that was removed). This
matters because `run_ingestion()` is meant to be safe to run unattended and
often; a normal run that silently wiped the whole index first would make a
transient failure (a misconfigured `RETRIEVAL_KNOWLEDGE_DIR` pointing at an
empty directory, say) catastrophic instead of a no-op. `rebuild_collection()`
is the explicit, operator-initiated "start over" command
(`scripts/rebuild_index.py`) for when the collection's shape itself needs
to change (e.g. switching `RETRIEVAL_SIMILARITY_METRIC`, which is fixed at
collection-creation time in Chroma -- same caveat
`embeddings/schema_indexer.py`'s own docstring notes for its collection).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from config.sensitive_columns import load_sensitive_columns
from config.settings import Settings, get_settings
from config.table_descriptions import load_table_descriptions
from db.connection import get_connection, get_read_only_engine
from db.relationship_inference import (
    InferredRelationship,
    infer_relationships,
    verify_candidates_with_data,
)
from db.schema_introspection import TableSchemaInfo, introspect_schema
from retrieval.chunking import (
    column_chunks_from_schema,
    documentation_chunks_from_text,
    glossary_chunks_from_yaml,
    inferred_relationship_chunks_from_schema,
    load_documentation_files,
    metric_chunks_from_yaml,
    relationship_chunks_from_schema,
    sql_example_chunks_from_yaml,
    table_chunks_from_schema,
)
from retrieval.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    get_embedding_provider,
    validate_dimensions,
)
from retrieval.models import Chunk
from retrieval.vector_store import VectorStore, VectorStoreError, get_vector_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionSummary:
    """The report every ingestion run produces -- printed by
    `scripts/ingest_schema.py`/`scripts/rebuild_index.py`, and asserted
    against directly in `tests/test_ingestion.py`."""

    database_id: str
    dry_run: bool
    discovered_records: int
    generated_chunks: int
    inserted: int
    updated: int
    skipped: int
    deleted: int
    failures: int
    duration_seconds: float
    counts_by_type: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def build_schema_chunks(
    tables: list[TableSchemaInfo],
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_name: str | None = None,
    *,
    settings: Settings | None = None,
    relationship_candidates: list[InferredRelationship] | None = None,
) -> list[Chunk]:
    """Table + column + relationship chunks from already-introspected tables.

    Takes `tables` rather than an engine -- same reasoning
    `embeddings.schema_indexer.build_index` gives for its own signature:
    keeps this function (and its tests) independent of a real database
    connection. The one exception is `relationship_candidates` (Prompt 07)
    -- when the caller has already computed data-verified candidates
    (`run_ingestion`, when it has both a live `engine` and
    `enable_relationship_data_verification=True`), passing them here
    avoids recomputing structural-only candidates and losing that
    refinement; when omitted (the default, and every existing caller's
    behavior), candidates are computed here from `tables` alone --
    structural only, still no query, still fully testable without a
    connection.
    """
    settings = settings or get_settings()
    descriptions = load_table_descriptions()
    sensitive_columns = load_sensitive_columns()
    column_notes = {name: desc.column_notes for name, desc in descriptions.items()}

    chunks: list[Chunk] = []
    chunks.extend(
        table_chunks_from_schema(
            tables, database_id, embedding_model, embedding_dimensions, schema_name, descriptions
        )
    )
    chunks.extend(
        column_chunks_from_schema(
            tables,
            database_id,
            embedding_model,
            embedding_dimensions,
            schema_name,
            column_notes,
            sensitive_columns,
        )
    )
    chunks.extend(
        relationship_chunks_from_schema(
            tables, database_id, embedding_model, embedding_dimensions, schema_name
        )
    )
    if settings.enable_relationship_inference:
        candidates = (
            relationship_candidates
            if relationship_candidates is not None
            else infer_relationships(
                tables, min_confidence=settings.relationship_inference_min_confidence
            )
        )
        chunks.extend(
            inferred_relationship_chunks_from_schema(
                candidates, database_id, embedding_model, embedding_dimensions, schema_name
            )
        )
    return chunks


def build_knowledge_chunks(
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    knowledge_dir: Path,
    documentation_chunk_chars: int,
    documentation_chunk_overlap_chars: int,
) -> list[Chunk]:
    """Glossary + metric + SQL-example + documentation chunks from
    `data/knowledge/*` (or `Settings.retrieval_knowledge_dir` override)."""
    chunks: list[Chunk] = []
    chunks.extend(
        glossary_chunks_from_yaml(
            knowledge_dir / "glossary.yaml", database_id, embedding_model, embedding_dimensions
        )
    )
    chunks.extend(
        metric_chunks_from_yaml(
            knowledge_dir / "metrics.yaml", database_id, embedding_model, embedding_dimensions
        )
    )
    chunks.extend(
        sql_example_chunks_from_yaml(
            knowledge_dir / "sql_examples.yaml", database_id, embedding_model, embedding_dimensions
        )
    )
    for parent_document_id, title, content, source_path in load_documentation_files(
        knowledge_dir / "documentation"
    ):
        chunks.extend(
            documentation_chunks_from_text(
                parent_document_id=parent_document_id,
                title=title,
                content=content,
                source_path=source_path,
                database_id=database_id,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                chunk_chars=documentation_chunk_chars,
                overlap_chars=documentation_chunk_overlap_chars,
            )
        )
    return chunks


def _count_discovered_records(tables: list[TableSchemaInfo], knowledge_dir: Path) -> int:
    """A best-effort "how many source records did this run look at" count,
    for the ingestion summary -- distinct from `generated_chunks` (schema
    tables produce one chunk per table *and* one per column *and* one per
    FK from the same discovered table record; a documentation file produces
    several chunks from one discovered file)."""
    from retrieval.chunking import _load_yaml_list  # local import, test seam

    count = len(tables)
    count += len(_load_yaml_list(knowledge_dir / "glossary.yaml", "terms"))
    count += len(_load_yaml_list(knowledge_dir / "metrics.yaml", "metrics"))
    count += len(_load_yaml_list(knowledge_dir / "sql_examples.yaml", "examples"))
    count += len(load_documentation_files(knowledge_dir / "documentation"))
    return count


def _embed_in_batches(
    provider: EmbeddingProvider, texts: list[str], batch_size: int
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(provider.embed_batch(batch))
    return vectors


def _diff_chunks(
    chunks: list[Chunk], existing_hashes: dict[str, str]
) -> tuple[list[Chunk], list[Chunk], int, list[str]]:
    """Splits `chunks` into (to_insert, to_update, skipped_count, stale_ids).

    `stale_ids` is every previously-stored chunk ID that no longer appears
    in `chunks` at all -- deleted by the caller via `VectorStore
    .delete_by_ids`, never a full-collection wipe (see this module's
    docstring).
    """
    new_by_id = {c.chunk_id: c for c in chunks}
    to_insert = [c for cid, c in new_by_id.items() if cid not in existing_hashes]
    to_update = [
        c
        for cid, c in new_by_id.items()
        if cid in existing_hashes and existing_hashes[cid] != c.content_hash
    ]
    skipped = len(new_by_id) - len(to_insert) - len(to_update)
    stale_ids = [cid for cid in existing_hashes if cid not in new_by_id]
    return to_insert, to_update, skipped, stale_ids


def run_ingestion(
    database_id: str,
    settings: Settings,
    dry_run: bool = False,
    vector_store: VectorStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    tables: list[TableSchemaInfo] | None = None,
) -> IngestionSummary:
    """Runs one full ingestion pass for `database_id`.

    Args:
        database_id: Which configured database (`Settings.databases[i].name`)
            to introspect and build the collection for.
        settings: Application settings.
        dry_run: If True, computes and returns the same summary but makes
            no embedding calls and no vector-store writes at all (`inserted`/
            `updated`/`deleted` reflect what *would* happen).
        vector_store: Optional override (mainly for tests).
        embedding_provider: Optional override (mainly for tests).
        tables: Optional pre-introspected tables (mainly for tests) --
            defaults to live-introspecting `database_id` via
            `db.connection`/`db.schema_introspection`, the same read-only
            path `embeddings/schema_indexer.py` already uses.

    Returns:
        An `IngestionSummary`. Never raises for an embedding/vector-store
        failure during the embed/upsert step (recorded in `failures`/
        `errors` instead) -- but *does* raise if the database itself can't
        be introspected at all (`tables` is None and the connection fails),
        since that's a genuine "nothing to ingest" configuration problem the
        caller (a CLI script) should surface directly, not silently skip.
    """
    start = time.perf_counter()
    errors: list[str] = []

    provider = embedding_provider or get_embedding_provider(settings)
    validate_dimensions(provider, settings.retrieval_embedding_dimensions)
    store = vector_store or get_vector_store(settings)
    store.create_collection_if_missing(database_id)

    schema_name: str | None = None
    engine = None
    if tables is None:
        connection_config = get_connection(settings, database_id)
        schema_name = connection_config.db_schema
        engine = get_read_only_engine(connection_config)
        tables = introspect_schema(engine, schema=schema_name)

    knowledge_dir = settings.retrieval_knowledge_dir
    discovered_records = _count_discovered_records(tables, knowledge_dir)

    # Prompt 07 (07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md): the data-driven
    # verification pass needs a live engine, which only exists here when
    # `tables` was live-introspected above (never for a caller -- mainly
    # tests -- that passed pre-built `tables` in directly). Structural-only
    # inference (build_schema_chunks's own default) still applies either
    # way; this only adds the optional refinement on top when both the
    # engine and the opt-in flag are available.
    relationship_candidates: list[InferredRelationship] | None = None
    if engine is not None and settings.enable_relationship_inference:
        relationship_candidates = infer_relationships(
            tables, min_confidence=settings.relationship_inference_min_confidence
        )
        if settings.enable_relationship_data_verification:
            relationship_candidates = verify_candidates_with_data(
                relationship_candidates,
                engine,
                schema=schema_name,
                sample_size=settings.relationship_data_verification_sample_size,
            )

    chunks = build_schema_chunks(
        tables,
        database_id,
        provider.model_name,
        provider.dimensions,
        schema_name,
        settings=settings,
        relationship_candidates=relationship_candidates,
    )
    chunks.extend(
        build_knowledge_chunks(
            database_id,
            provider.model_name,
            provider.dimensions,
            knowledge_dir,
            settings.retrieval_documentation_chunk_chars,
            settings.retrieval_documentation_chunk_overlap_chars,
        )
    )
    counts_by_type: dict[str, int] = {}
    for chunk in chunks:
        counts_by_type[chunk.chunk_type.value] = counts_by_type.get(chunk.chunk_type.value, 0) + 1

    try:
        existing_hashes = store.list_chunk_hashes(database_id)
    except VectorStoreError as exc:
        errors.append(str(exc))
        existing_hashes = {}

    to_insert, to_update, skipped, stale_ids = _diff_chunks(chunks, existing_hashes)

    if dry_run:
        duration = time.perf_counter() - start
        logger.info(
            "[ingestion] DRY RUN database=%r would insert=%d update=%d skip=%d delete=%d",
            database_id,
            len(to_insert),
            len(to_update),
            skipped,
            len(stale_ids),
        )
        return IngestionSummary(
            database_id=database_id,
            dry_run=True,
            discovered_records=discovered_records,
            generated_chunks=len(chunks),
            inserted=len(to_insert),
            updated=len(to_update),
            skipped=skipped,
            deleted=len(stale_ids),
            failures=0,
            duration_seconds=round(duration, 3),
            counts_by_type=counts_by_type,
            errors=errors,
        )

    to_embed = to_insert + to_update
    failures = 0
    inserted = 0
    updated = 0
    if to_embed:
        try:
            vectors = _embed_in_batches(
                provider, [c.text for c in to_embed], settings.retrieval_embedding_batch_size
            )
            store.upsert_documents(database_id, to_embed, vectors)
            inserted = len(to_insert)
            updated = len(to_update)
        except (EmbeddingError, VectorStoreError) as exc:
            failures = len(to_embed)
            errors.append(str(exc))
            logger.error(
                "[ingestion] embed/upsert failed for database %r (%d chunk(s)): %s",
                database_id,
                len(to_embed),
                exc,
            )

    deleted = 0
    if stale_ids:
        try:
            deleted = store.delete_by_ids(database_id, stale_ids)
        except VectorStoreError as exc:
            errors.append(str(exc))
            logger.error(
                "[ingestion] failed to delete %d stale chunk(s) for database %r: %s",
                len(stale_ids),
                database_id,
                exc,
            )

    duration = time.perf_counter() - start
    logger.info(
        "[ingestion] database=%r discovered=%d chunks=%d inserted=%d updated=%d "
        "skipped=%d deleted=%d failures=%d duration=%.2fs",
        database_id,
        discovered_records,
        len(chunks),
        inserted,
        updated,
        skipped,
        deleted,
        failures,
        duration,
    )
    return IngestionSummary(
        database_id=database_id,
        dry_run=False,
        discovered_records=discovered_records,
        generated_chunks=len(chunks),
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        deleted=deleted,
        failures=failures,
        duration_seconds=round(duration, 3),
        counts_by_type=counts_by_type,
        errors=errors,
    )


def rebuild_collection(
    database_id: str,
    settings: Settings,
    vector_store: VectorStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    tables: list[TableSchemaInfo] | None = None,
) -> IngestionSummary:
    """Explicit full rebuild: drops `database_id`'s entire collection, then
    runs a fresh `run_ingestion` against the now-empty collection (so every
    chunk is reported as `inserted`, never `updated`/`skipped`).

    Reserved for `scripts/rebuild_index.py` -- never called by
    `run_ingestion()` itself. See this module's docstring for why normal
    ingestion must never do this on its own.
    """
    store = vector_store or get_vector_store(settings)
    store.drop_collection(database_id)
    logger.warning("[ingestion] REBUILD: dropped entire collection for database %r", database_id)
    return run_ingestion(
        database_id,
        settings,
        dry_run=False,
        vector_store=store,
        embedding_provider=embedding_provider,
        tables=tables,
    )
