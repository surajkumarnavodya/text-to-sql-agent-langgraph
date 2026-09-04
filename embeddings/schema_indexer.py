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

Tried and reverted: embedding the description text directly (so a
business-term question like "which territory sold the most Bikes?" could
vector-match a table whose columns say nothing about "Bikes" but whose
purpose note does). Measured live against this project's own benchmark,
this did more harm than good: hand-written prose from one table's note can
coincidentally share vocabulary with an unrelated question -- concretely,
`FactCallCenter`'s purpose text includes the phrase "orders placed" (part
of its own, unrelated call-center-metrics description), which caused it to
vector-match "How many orders were placed in January 2012?" ahead of the
actually-relevant `FactInternetSales`/`DimDate`, a straight-up retrieval
miss. Business terminology is better handled at the *lexical* level (see
`embeddings/retriever.py`'s table/column-name bonus) than by folding
free-form prose into the embedding text.

Cache invalidation: `db.schema_introspection.get_schema_fingerprint()`
hashes the introspected schema; that hash is stored alongside the Chroma
persist directory, one file per configured database (`.schema_hash__
<db_name>` -- see `_hash_filename`). Re-embedding is skipped whenever the
hash matches, so refreshing the schema (e.g. the UI's "Refresh Schema"
button, or every app startup) doesn't redo embedding work unless that
database's schema actually changed.

Multi-database support: every configured database (`Settings.databases`)
gets its own Chroma collection (`get_collection`'s `db_name` param -- see
its docstring for why a shared collection isn't used) and its own
fingerprint file, so indexing/refreshing one database never touches
another's index. `refresh_all_schema_indexes()` is the entry point that
loops every configured database; `embeddings/retriever.py::select_database`
is what decides, per question, which database's collection to actually
query.
"""

from __future__ import annotations

import contextlib
import logging

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.utils import embedding_functions
from sqlalchemy import Engine

from config.settings import Settings, get_settings
from db.connection import get_connection, get_read_only_engine
from db.schema_introspection import TableSchemaInfo, get_schema_fingerprint, introspect_schema
from db.value_sampling import attach_sample_values

logger = logging.getLogger(__name__)


def _hash_filename(db_name: str) -> str:
    """Per-database schema-fingerprint cache filename -- see `build_index`."""
    return f".schema_hash__{db_name}"


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


def _collection_name(settings: Settings, db_name: str) -> str:
    """Per-database Chroma collection name -- see `get_collection`'s docstring."""
    return f"{settings.chroma_collection_name}__{db_name}"


def get_collection(client: chromadb.ClientAPI, settings: Settings, db_name: str) -> Collection:
    """Gets or creates the schema-DDL collection for one configured database.

    Each database gets its **own** Chroma collection (name derived from
    `db_name` -- see `_collection_name`), never a shared one: this is what
    keeps `embeddings/retriever.py`'s FK-bridge and keyword-match expansion
    correct once more than one database is configured -- bridging across
    two physically unrelated databases' foreign-key graphs would be
    meaningless, and a shared collection would also risk table-name-as-ID
    collisions between two databases that happen to have a same-named
    table. `db_name` must be one of `Settings.databases`' `.name` values
    (`"default"` for a plain single-database setup -- see `Settings.
    databases`' docstring).

    Explicitly configured for cosine distance (`hnsw:space`) rather than
    accepting Chroma's default (squared L2). `embeddings/retriever.py`
    computes `similarity_score = 1 - distance`, which is only a correct,
    boundedly-interpretable similarity value if `distance` actually is
    cosine distance -- under the default L2 metric that expression silently
    produced a number that *looked* like a similarity score but wasn't one
    (e.g. deeply negative for every candidate), which is harmless for
    relative top-k ordering with unit-normalized embeddings (both metrics
    are monotonic in that case) but breaks any threshold/margin logic that
    needs the score's absolute value to mean something -- exactly what
    `retrieve_relevant_schema`'s adaptive candidate selection now needs.
    Note: `hnsw:space` is fixed at collection-creation time, so this only
    takes effect for a freshly created collection (a schema/config change
    already forces `build_index` to delete + recreate the collection; an
    existing collection built before this change needs one
    `python scripts/build_embeddings.py --force` to pick it up).
    """
    return client.get_or_create_collection(
        name=_collection_name(settings, db_name),
        embedding_function=_get_embedding_function(settings),
        metadata={"hnsw:space": "cosine"},
    )


def build_index(
    tables: list[TableSchemaInfo],
    db_name: str,
    settings: Settings | None = None,
    force: bool = False,
    fingerprint_tables: list[TableSchemaInfo] | None = None,
) -> int:
    """Builds (or refreshes) one database's Chroma collection from already-introspected tables.

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
        db_name: Which configured database (`Settings.databases[i].name`)
            these tables belong to -- determines the target Chroma
            collection (see `get_collection`) and the cache-hash filename
            (see `_hash_filename`), so different databases' indexes and
            invalidation state never collide.
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
            f"No tables to index for database {db_name!r}. Check its DB_*_SCHEMA (if set) "
            f"and that the connected database user can see the expected tables."
        )

    settings = settings or get_settings()
    current_hash = get_schema_fingerprint(
        fingerprint_tables if fingerprint_tables is not None else tables
    )
    hash_path = settings.chroma_persist_dir / _hash_filename(db_name)

    if (
        not force
        and hash_path.exists()
        and hash_path.read_text(encoding="utf-8").strip() == current_hash
    ):
        logger.info(
            "Schema unchanged since last index build for database %r (%d tables); "
            "skipping re-embedding.",
            db_name,
            len(tables),
        )
        return len(tables)

    client = get_chroma_client(settings)
    collection_name = _collection_name(settings, db_name)
    # Collection may not exist yet on a first run; exact error type varies by
    # chromadb version, so suppress broadly rather than chasing it.
    with contextlib.suppress(Exception):
        client.delete_collection(collection_name)
    collection = get_collection(client, settings, db_name)

    collection.add(
        ids=[table.table_name for table in tables],
        documents=[table.ddl for table in tables],
        metadatas=[
            {
                "table_name": table.table_name,
                "db_name": db_name,
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
        "Indexed %d table(s) into Chroma collection '%s' for database %r.",
        len(tables),
        collection_name,
        db_name,
    )
    return len(tables)


def refresh_schema_index(
    engine: Engine, db_name: str, settings: Settings | None = None, force: bool = False
) -> list[TableSchemaInfo]:
    """Introspects, samples, and (re)indexes one configured database's schema.

    Single source of truth for the introspect -> sample -> embed pipeline --
    previously duplicated between `scripts/build_embeddings.py` and
    `ui/app.py`'s schema initialization. The fingerprint used for cache
    invalidation is computed from the pre-value-sampling tables (see
    `build_index`'s `fingerprint_tables` parameter), so incidental data
    changes in a sampled column don't force a re-embed on their own.

    Args:
        engine: A read-only SQLAlchemy engine for this specific database
            (typically `db.connection.get_read_only_engine(config)` for the
            matching `db.connection.get_connection(settings, db_name)`).
        db_name: The configured connection's name (`Settings.databases[i].
            name`) -- also used to resolve that connection's own
            `db_schema` restriction (each database can restrict
            introspection to a different schema).
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
    db_schema = get_connection(settings, db_name).db_schema
    tables = introspect_schema(engine, schema=db_schema)
    sampled_tables = attach_sample_values(engine, tables)
    build_index(sampled_tables, db_name, settings=settings, force=force, fingerprint_tables=tables)
    return sampled_tables


def refresh_all_schema_indexes(
    settings: Settings | None = None, force: bool = False
) -> dict[str, list[TableSchemaInfo]]:
    """Introspects, samples, and (re)indexes every configured database's schema.

    The single shared orchestration point for "refresh everything" --
    `scripts/build_embeddings.py`, `ui/app.py`'s schema initialization, and
    `scripts/integration_test.py` all call this rather than looping over
    `Settings.databases` themselves. A failure introspecting/indexing one
    database does not stop the others -- it's logged and that database is
    simply omitted from the returned dict, so a single misconfigured or
    temporarily-unreachable connection can't block every other configured
    database from getting a working schema index.

    Returns:
        `{db_name: sample-value-enriched tables}` for every database that
        was successfully indexed.
    """
    settings = settings or get_settings()
    results: dict[str, list[TableSchemaInfo]] = {}
    for config in settings.databases:
        try:
            engine = get_read_only_engine(config)
            results[config.name] = refresh_schema_index(
                engine, config.name, settings=settings, force=force
            )
        except Exception as exc:  # noqa: BLE001 - one bad connection must not block the rest
            logger.warning("Skipping schema index refresh for database %r: %s", config.name, exc)
    return results
