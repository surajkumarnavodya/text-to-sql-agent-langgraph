"""Builds and refreshes the ChromaDB schema index from live schema introspection.

Chunking strategy: one chunk per table (its synthesized DDL text from
`db/schema_introspection.py`), not per column -- retrieval scores whole
tables against the question, and `agent.nodes.retrieve_schema_node`
concatenates the DDL of the retrieved chunks directly into the generation
prompt, so a "chunk" here should be exactly what we'd want to hand the LLM
for one table.

Deliberately NOT embedded here: hand-authored notes from `config/
table_descriptions.yaml`. Baking those into the Chroma document would freeze
them as of the last embeddings build -- editing that file to fix a wrong
note wouldn't take effect until someone reruns `build_embeddings.py`.
Instead, `agent.nodes.retrieve_schema_node` applies the *current* file's
notes (via `config.table_descriptions.apply_table_description()`) on top of
whatever DDL comes back from retrieval, fresh on every question. This
module only ever embeds the structural DDL (+ sampled values).

Cache invalidation: `db.schema_introspection.get_schema_fingerprint()`
hashes the introspected schema; that hash is stored alongside the Chroma
persist directory (`.schema_hash`). Re-embedding is skipped whenever the
hash matches, so refreshing the schema (e.g. the UI's "Refresh Schema"
button, or every app startup) doesn't redo embedding work unless the real
database's schema actually changed.
"""

from __future__ import annotations

import contextlib
import logging

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.utils import embedding_functions
from sqlalchemy import Engine

from config.settings import Settings, get_settings
from db.schema_introspection import TableSchemaInfo, get_schema_fingerprint, introspect_schema
from db.value_sampling import attach_sample_values

logger = logging.getLogger(__name__)

_HASH_FILENAME = ".schema_hash"


def _get_embedding_function(settings: Settings) -> embedding_functions.EmbeddingFunction:
    """Returns the embedding function configured by `EMBEDDING_MODEL_NAME`.

    The default ("all-MiniLM-L6-v2") uses Chroma's own bundled ONNX runtime
    embedding function -- no `sentence-transformers`/`torch` install
    required, which matters on a fresh Python version where heavy ML wheels
    may not yet be published. Any other model name falls back to
    `SentenceTransformerEmbeddingFunction`, which does require that extra.
    """
    if settings.embedding_model_name == "all-MiniLM-L6-v2":
        return embedding_functions.DefaultEmbeddingFunction()
    logger.warning(
        "Non-default embedding model '%s' requested; this requires "
        "sentence-transformers (and torch) to be installed.",
        settings.embedding_model_name,
    )
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=settings.embedding_model_name
    )


def get_chroma_client(settings: Settings | None = None) -> chromadb.ClientAPI:
    """Returns a Chroma client persisted to `Settings.chroma_persist_dir`."""
    settings = settings or get_settings()
    settings.chroma_persist_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(settings.chroma_persist_dir))


def get_collection(client: chromadb.ClientAPI, settings: Settings) -> Collection:
    """Gets or creates the schema-DDL collection with the configured embedding function."""
    return client.get_or_create_collection(
        name=settings.chroma_collection_name,
        embedding_function=_get_embedding_function(settings),
    )


def build_index(
    tables: list[TableSchemaInfo],
    settings: Settings | None = None,
    force: bool = False,
    fingerprint_tables: list[TableSchemaInfo] | None = None,
) -> int:
    """Builds (or refreshes) the Chroma collection from already-introspected tables.

    Takes `tables` rather than a database engine/connection: this function's
    only responsibility is embedding, not introspection -- callers (`scripts
    /build_embeddings.py`, `ui/app.py`) run `db.schema_introspection
    .introspect_schema()` first and pass the result in. That keeps this
    module testable without mocking a database connection.

    Args:
        tables: Tables to embed -- typically the output of
            `db.value_sampling.attach_sample_values()` (sample-value-enriched
            DDL) rather than raw `introspect_schema()` output, so the LLM
            sees real column values.
        force: If True, re-embeds even if the schema fingerprint hasn't changed.
        settings: Optional `Settings` override (mainly for tests).
        fingerprint_tables: Tables to compute the cache-invalidation hash
            from, if different from `tables`. Pass the *pre*-value-sampling
            tables here so the hash tracks schema *shape* changes only --
            not incidental data-value churn (e.g. a new distinct value
            appearing in a sampled column), which would otherwise force a
            re-embed on every build even though nothing schema-relevant
            changed. Defaults to `tables` for callers with no separate
            sampling step.

    Returns:
        The number of table chunks indexed (or already-cached, if skipped).

    Raises:
        ValueError: if `tables` is empty -- an empty schema means either the
            connection/schema filter is misconfigured or the database
            genuinely has no tables, and either way there's nothing useful
            to embed.
    """
    if not tables:
        raise ValueError(
            "No tables to index. Check DB_SCHEMA (if set) and that the "
            "connected database user can see the expected tables."
        )

    settings = settings or get_settings()
    current_hash = get_schema_fingerprint(
        fingerprint_tables if fingerprint_tables is not None else tables
    )
    hash_path = settings.chroma_persist_dir / _HASH_FILENAME

    if (
        not force
        and hash_path.exists()
        and hash_path.read_text(encoding="utf-8").strip() == current_hash
    ):
        logger.info(
            "Schema unchanged since last index build (%d tables); skipping re-embedding.",
            len(tables),
        )
        return len(tables)

    client = get_chroma_client(settings)
    # Collection may not exist yet on a first run; exact error type varies by
    # chromadb version, so suppress broadly rather than chasing it.
    with contextlib.suppress(Exception):
        client.delete_collection(settings.chroma_collection_name)
    collection = get_collection(client, settings)

    collection.add(
        ids=[table.table_name for table in tables],
        documents=[table.ddl for table in tables],
        metadatas=[
            {
                "table_name": table.table_name,
                # Comma-joined referred-table names -- lets retriever.py do
                # FK-adjacency bridge expansion (see
                # `embeddings/retriever.py::_expand_with_fk_bridges`) without
                # a live DB round-trip per question: this is the only
                # metadata it needs, since bridge detection only cares
                # "are these two tables directly FK-connected," not the
                # specific columns.
                "fk_targets": ",".join(sorted({fk.referred_table for fk in table.foreign_keys})),
            }
            for table in tables
        ],
    )

    settings.chroma_persist_dir.mkdir(parents=True, exist_ok=True)
    hash_path.write_text(current_hash, encoding="utf-8")
    logger.info(
        "Indexed %d table(s) into Chroma collection '%s'.",
        len(tables),
        settings.chroma_collection_name,
    )
    return len(tables)


def refresh_schema_index(
    engine: Engine, settings: Settings | None = None, force: bool = False
) -> list[TableSchemaInfo]:
    """Introspects, samples, and (re)indexes the schema in one call.

    Single source of truth for the introspect -> sample -> embed pipeline --
    previously duplicated between `scripts/build_embeddings.py` and
    `ui/app.py`'s schema initialization. The fingerprint used for cache
    invalidation is computed from the pre-value-sampling tables (see
    `build_index`'s `fingerprint_tables` parameter), so incidental data
    changes in a sampled column don't force a re-embed on their own.

    Args:
        engine: A read-only SQLAlchemy engine.
        settings: Optional `Settings` override (mainly for tests).
        force: If True, re-embeds even if the schema fingerprint hasn't
            changed since the last build.

    Returns:
        The sample-value-enriched tables (same shape `ui/app.py`'s "Discovered
        tables" panel and `_build_chart`'s key-column lookup expect).

    Raises:
        ValueError: if the database has no tables to index (see `build_index`).
    """
    settings = settings or get_settings()
    tables = introspect_schema(engine, schema=settings.db_schema)
    sampled_tables = attach_sample_values(engine, tables)
    build_index(sampled_tables, settings=settings, force=force, fingerprint_tables=tables)
    return sampled_tables
