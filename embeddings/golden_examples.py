"""Stores and retrieves the "golden dataset" -- human-approved (question,
SQL) pairs a user explicitly saved via the dashboard's thumbs-up feedback
(after its "Confirm and Run" flow) -- and turns the best-matching ones
into few-shot examples injected into the SQL-generation prompt (see
`agent.nodes.retrieve_golden_examples_node` and
`agent.llm_client._build_golden_examples_block`).

Deliberately modeled on `embeddings/schema_indexer.py`'s ChromaDB pattern
rather than `rag/store.py`'s SQL Server native `VECTOR` one: the core
pipeline already depends unconditionally on a local Chroma
`PersistentClient` for schema retrieval (no server, no extra connection
string, no `ENABLE_*`-gated optional subsystem), and this reuses the exact
same embedding function/runtime already resident in the process. `rag/`'s
store, by contrast, is deliberately an *optional* subsystem (a dedicated
SQL Server 2025+ connection, gated behind `ENABLE_DOCUMENT_RAG`/
`ENABLE_POLICY_RAG`) -- making a core few-shot-example feature depend on
that would be new, unwanted overhead this app's core pipeline doesn't
otherwise carry.

One collection per configured database (`f"golden_examples__{db_name}"`),
mirroring `embeddings.schema_indexer._collection_name` exactly and for the
same reason: a golden example saved against one database's schema is
meaningless -- or actively misleading -- for a different database.

Every function here fails open: a missing/empty collection, a disabled
feature flag, or any Chroma error all degrade to "no examples" (an empty
list from `retrieve_golden_examples`, or a logged-and-swallowed no-op from
`save_golden_example`), never an exception that could interrupt a
question or a UI feedback click. This mirrors `agent.nodes.plan_query_node`/
`estimate_query_cost_node`'s "an accuracy aid must never be a reason a
question can't be answered" philosophy.
"""

from __future__ import annotations

import hashlib
import logging

import chromadb
from chromadb.api.models.Collection import Collection

from agent.state import GoldenExample
from config.settings import Settings, get_settings
from embeddings.schema_indexer import get_chroma_client, get_embedding_function
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)


def _collection_name(db_name: str) -> str:
    """Per-database golden-dataset Chroma collection name."""
    return f"golden_examples__{db_name}"


def get_golden_examples_collection(
    client: chromadb.ClientAPI, settings: Settings, db_name: str
) -> Collection:
    """Gets or creates the golden-dataset collection for one configured database.

    Same cosine-distance configuration as `embeddings.schema_indexer
    .get_collection`, for the same reason: `retrieve_golden_examples` below
    computes `similarity_score = 1 - distance`, which is only a correct,
    boundedly-interpretable value under cosine distance.
    """
    return client.get_or_create_collection(
        name=_collection_name(db_name),
        embedding_function=get_embedding_function(settings),
        metadata={"hnsw:space": "cosine"},
    )


def _example_id(db_name: str, question: str, sql: str) -> str:
    """Deterministic id for one (db_name, question, sql) triple.

    Re-saving the exact same pair (e.g. a user clicking thumbs-up twice)
    upserts the same document rather than accumulating duplicates -- see
    `save_golden_example`.
    """
    digest_input = f"{db_name}\x00{question}\x00{sql}".encode()
    return hashlib.sha256(digest_input).hexdigest()


def save_golden_example(
    question: str, sql: str, db_name: str, settings: Settings | None = None
) -> None:
    """Saves one human-approved (question, SQL) pair to the golden dataset.

    Never raises -- a storage failure here must not be the reason a UI
    feedback click appears to fail; it's logged and swallowed, matching
    every other fail-open contract in this module.

    Args:
        question: The natural-language question, exactly as asked.
        sql: The exact SQL that was actually run and confirmed correct --
            not necessarily the LLM's original draft if the user edited it
            before confirming (see `ui/session_history.py`'s
            `QueryHistoryEntry.confirmed_sql`).
        db_name: Which configured database (`Settings.databases[i].name`)
            this pair belongs to.
        settings: Optional `Settings` override (mainly for tests).
    """
    settings = settings or get_settings()
    try:
        client = get_chroma_client(settings)
        collection = get_golden_examples_collection(client, settings, db_name)
        collection.upsert(
            ids=[_example_id(db_name, question, sql)],
            documents=[question],
            metadatas=[{"sql": sql}],
        )
    except Exception as exc:  # noqa: BLE001 - saving feedback must never crash the UI
        safe_detail = redact_secrets(str(exc), settings)
        logger.warning("Failed to save golden example for database %r: %s", db_name, safe_detail)
        return
    logger.info("Saved a golden example for database %r.", db_name)


def retrieve_golden_examples(
    question: str, db_name: str, settings: Settings | None = None, top_k: int | None = None
) -> list[GoldenExample]:
    """Returns the best-matching saved golden examples for this question.

    Args:
        question: The user's natural-language question.
        db_name: Which configured database's golden-dataset collection to
            search (see `Settings.databases`' docstring; `"default"` for a
            plain single-database setup).
        settings: Optional `Settings` override (mainly for tests).
        top_k: Override for `Settings.golden_examples_top_k`.

    Returns:
        Up to `top_k` examples, most-similar first, each with
        `similarity_score >= Settings.golden_examples_min_similarity`.
        Empty list if the collection is missing/empty, nothing cleared the
        similarity threshold, or the query failed for any reason -- never
        raises (see this module's docstring).
    """
    settings = settings or get_settings()
    resolved_top_k = top_k or settings.golden_examples_top_k

    try:
        client = get_chroma_client(settings)
        collection = get_golden_examples_collection(client, settings, db_name)
        count = collection.count()
        if count == 0:
            return []
        result = collection.query(
            query_texts=[question],
            n_results=min(resolved_top_k, count),
        )
    except Exception as exc:  # noqa: BLE001 - Chroma/onnxruntime error types vary by backend
        safe_detail = redact_secrets(str(exc), settings)
        logger.warning("Failed to query golden examples for database %r: %s", db_name, safe_detail)
        return []

    documents = result.get("documents") or [[]]
    metadatas = result.get("metadatas") or [[]]
    distances = result.get("distances") or [[]]

    documents_row = documents[0] if documents else []
    metadatas_row = metadatas[0] if metadatas else []
    distances_row = distances[0] if distances else []

    examples: list[GoldenExample] = []
    for document, metadata, distance in zip(
        documents_row, metadatas_row, distances_row, strict=True
    ):
        similarity_score = round(1.0 - distance, 4)
        if similarity_score < settings.golden_examples_min_similarity:
            continue
        sql = metadata.get("sql") if metadata else None
        if not isinstance(sql, str) or not sql:
            continue
        examples.append(
            GoldenExample(question=document, sql=sql, similarity_score=similarity_score)
        )
    return examples
