"""Vector-store interface + its ChromaDB implementation.

`VectorStore` is the abstraction every caller in this package (`ingestion.py`,
`retriever.py`) programs against -- never `chromadb` directly outside this
module. `ChromaVectorStore` is the only implementation (see
`docs/vector-retrieval-design.md`'s "Selected vector database" section for
why this project standardizes on Chroma rather than adding a second vector
database), but the interface exists so a future backend swap, or a
test double, doesn't require touching `ingestion.py`/`retriever.py` at all.

Reuses `embeddings.schema_indexer.get_chroma_client` -- the same
process-lifetime-cached `PersistentClient` singleton every other
Chroma-backed module in this codebase (schema DDL, golden examples, media
search) already shares. One collection per configured database
(`f"{Settings.retrieval_collection_name}__{database_id}"`), the same
per-database-collection convention `embeddings/schema_indexer.py` and
`embeddings/golden_examples.py` already establish, for the same reason:
retrieval must never mix business-context chunks across two physically
unrelated databases.

Embeddings are always supplied explicitly by the caller (via `embeddings.py`)
rather than delegated to a Chroma-managed `embedding_function` callback --
unlike `embeddings/schema_indexer.py`'s collections, which let Chroma call
the embedding function internally. This is deliberate: it's what makes
`embeddings.py`'s own retry/timeout/dimension-validation logic the single
authority over how a vector was produced, and keeps this module able to
validate a vector's dimensionality *before* ever handing it to Chroma.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from chromadb.api.models.Collection import Collection

from config.settings import Settings
from retrieval.models import Chunk, ChunkType, ScoredChunk, Sensitivity

logger = logging.getLogger(__name__)

_METRIC_TO_HNSW_SPACE = {"cosine": "cosine", "l2": "l2", "ip": "ip"}


class VectorStoreError(Exception):
    """Raised when a vector-store operation fails after all configured retries.

    Structured and specific (never a bare re-raise) -- `retriever.py` and
    `ingestion.py` both catch this exact type to trigger their own fail-open
    behavior (see each module's docstring), the same "log a structured
    warning, degrade, never hide the failure" contract
    `docs/vector-retrieval-design.md`'s fallback-behavior section requires.
    """


@dataclass(frozen=True)
class HealthCheckResult:
    ok: bool
    detail: str
    chunk_count: int | None = None


@dataclass(frozen=True)
class CollectionInfo:
    name: str
    database_id: str
    count: int
    similarity_metric: str
    embedding_dimensions: int | None = None
    counts_by_type: dict[str, int] = field(default_factory=dict)


class VectorStore(ABC):
    """Interface every caller in this package programs against."""

    @abstractmethod
    def create_collection_if_missing(self, database_id: str) -> None: ...

    @abstractmethod
    def health_check(self, database_id: str) -> HealthCheckResult: ...

    @abstractmethod
    def upsert_documents(
        self, database_id: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None: ...

    @abstractmethod
    def delete_by_source(self, database_id: str, source_ids: Iterable[str]) -> int: ...

    @abstractmethod
    def delete_by_ids(self, database_id: str, chunk_ids: Iterable[str]) -> int: ...

    @abstractmethod
    def similarity_search(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        chunk_types: list[ChunkType] | None = None,
    ) -> list[ScoredChunk]: ...

    @abstractmethod
    def similarity_search_with_filters(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        filters: dict[str, Any],
    ) -> list[ScoredChunk]: ...

    @abstractmethod
    def count(self, database_id: str) -> int: ...

    @abstractmethod
    def collection_info(self, database_id: str) -> CollectionInfo: ...

    @abstractmethod
    def list_chunk_hashes(
        self, database_id: str, chunk_types: list[ChunkType] | None = None
    ) -> dict[str, str]:
        """Returns `{chunk_id: content_hash}` for every currently-stored chunk
        (optionally scoped to `chunk_types`) -- the diff base
        `ingestion.py` compares freshly-built chunks against to decide
        insert/update/skip/delete, without re-embedding anything first."""

    @abstractmethod
    def drop_collection(self, database_id: str) -> None:
        """Deletes the *entire* collection for `database_id`, including its
        schema/index configuration -- reserved for `ingestion.rebuild_collection`
        (an explicit, operator-initiated rebuild), never called during normal
        ingestion (see `ingestion.py`'s module docstring)."""

    @abstractmethod
    def close(self) -> None: ...


def _retry(func_name: str, call: Callable[[], Any], retry_count: int, timeout_seconds: int) -> Any:
    """Bounded exponential backoff around one Chroma call -- same policy
    shape as `retrieval.embeddings._with_retry`, kept as a separate copy
    (not a shared helper) since the two operate on different call signatures
    and this module must not import `retrieval.embeddings` (no reason for a
    dependency between them)."""
    attempt = 0
    start = time.monotonic()
    last_exc: Exception | None = None
    while attempt <= retry_count:
        if attempt > 0:
            elapsed = time.monotonic() - start
            if elapsed >= timeout_seconds:
                break
            backoff = min(0.5 * (2 ** (attempt - 1)), timeout_seconds - elapsed)
            time.sleep(max(backoff, 0))
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - chromadb/onnxruntime error types vary
            last_exc = exc
            logger.warning(
                "[vector_store] %s attempt %d/%d failed: %s",
                func_name,
                attempt + 1,
                retry_count + 1,
                exc,
            )
            attempt += 1
    raise VectorStoreError(
        f"{func_name} failed after {attempt} attempt(s): {last_exc}"
    ) from last_exc


def _chunk_to_metadata(chunk: Chunk) -> dict[str, Any]:
    """Flattens a `Chunk` into Chroma-storable metadata (str/int/float/bool
    values only -- Chroma metadata cannot hold nested dicts/lists directly,
    so `tags`/`allowed_roles` are comma-joined and `extra` is JSON-encoded)."""
    return {
        "chunk_type": chunk.chunk_type.value,
        "database_id": chunk.database_id,
        "schema_name": chunk.schema_name or "",
        "table_name": chunk.table_name or "",
        "column_name": chunk.column_name or "",
        "source_id": chunk.source_id,
        "content_hash": chunk.content_hash,
        "version": chunk.version,
        "sensitivity": chunk.sensitivity.value,
        "tags": ",".join(chunk.tags),
        "allowed_roles": ",".join(chunk.allowed_roles),
        "embedding_model": chunk.embedding_model,
        "embedding_dimensions": chunk.embedding_dimensions,
        "source_updated_at": chunk.source_updated_at or "",
        "extra_json": json.dumps(chunk.extra, default=str),
    }


def _metadata_to_chunk(chunk_id: str, text: str, metadata: dict[str, Any]) -> Chunk:
    """Inverse of `_chunk_to_metadata` -- reconstructs a `Chunk` from a
    Chroma query/get result row."""
    tags = tuple(t for t in (metadata.get("tags") or "").split(",") if t)
    allowed_roles = tuple(r for r in (metadata.get("allowed_roles") or "").split(",") if r)
    try:
        extra = json.loads(metadata.get("extra_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        extra = {}
    return Chunk(
        chunk_id=chunk_id,
        chunk_type=ChunkType(metadata["chunk_type"]),
        text=text,
        database_id=metadata.get("database_id") or "",
        schema_name=metadata.get("schema_name") or None,
        table_name=metadata.get("table_name") or None,
        column_name=metadata.get("column_name") or None,
        source_id=metadata.get("source_id") or chunk_id,
        content_hash=metadata.get("content_hash") or "",
        version=int(metadata.get("version") or 1),
        sensitivity=Sensitivity(metadata.get("sensitivity") or "normal"),
        tags=tags,
        allowed_roles=allowed_roles,
        embedding_model=metadata.get("embedding_model") or "",
        embedding_dimensions=int(metadata.get("embedding_dimensions") or 0),
        source_updated_at=metadata.get("source_updated_at") or None,
        extra=extra,
    )


class ChromaVectorStore(VectorStore):
    """`VectorStore` backed by this project's shared ChromaDB `PersistentClient`."""

    def __init__(self, settings: Settings) -> None:
        # Imported lazily so this module's own import doesn't require
        # chromadb to already be configured/importable in a context that
        # only needs the interface/dataclasses above (e.g. a type-checking
        # pass, or a test that only exercises a fake VectorStore).
        from embeddings.schema_indexer import get_chroma_client

        self._settings = settings
        self._client = get_chroma_client(settings)
        self._retry_count = settings.retrieval_embedding_retry_count
        self._timeout_seconds = settings.retrieval_embedding_timeout_seconds

    def _collection_name(self, database_id: str) -> str:
        return f"{self._settings.retrieval_collection_name}__{database_id}"

    def _get_collection(self, database_id: str) -> Collection:
        space = _METRIC_TO_HNSW_SPACE.get(self._settings.retrieval_similarity_metric, "cosine")
        return self._client.get_or_create_collection(
            name=self._collection_name(database_id),
            metadata={"hnsw:space": space},
        )

    def create_collection_if_missing(self, database_id: str) -> None:
        self._get_collection(database_id)

    def health_check(self, database_id: str) -> HealthCheckResult:
        try:
            collection = self._get_collection(database_id)
            count = collection.count()
        except Exception as exc:  # noqa: BLE001 - chromadb error types vary
            return HealthCheckResult(ok=False, detail=f"Vector store unreachable: {exc}")
        return HealthCheckResult(ok=True, detail="Collection reachable.", chunk_count=count)

    def upsert_documents(
        self, database_id: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must be the same length")
        collection = self._get_collection(database_id)

        def _call() -> None:
            # chromadb's own stubs type `embeddings=` as a narrower union
            # (raw ndarrays / Sequence[float]) than the plain `list[list[float]]`
            # this module deliberately works with everywhere else (see this
            # module's docstring on why embeddings are always supplied
            # explicitly) -- a real, accepted runtime shape, just not one the
            # stub's Sequence[float] | Sequence[int] union expresses cleanly.
            collection.upsert(
                ids=[c.chunk_id for c in chunks],
                documents=[c.text for c in chunks],
                embeddings=cast(Any, vectors),
                metadatas=[_chunk_to_metadata(c) for c in chunks],
            )

        _retry("upsert_documents", _call, self._retry_count, self._timeout_seconds)

    def delete_by_source(self, database_id: str, source_ids: Iterable[str]) -> int:
        source_id_list = list(source_ids)
        if not source_id_list:
            return 0
        collection = self._get_collection(database_id)
        # cast: chromadb's `Where` stub type is a closed literal union that
        # doesn't cleanly express a plain nested dict built at runtime like
        # this one -- same accepted-but-narrowly-typed situation as
        # `embeddings=` above.
        existing = collection.get(
            where=cast(Any, {"source_id": {"$in": source_id_list}}), include=[]
        )
        ids = existing.get("ids") or []
        if not ids:
            return 0

        def _call() -> None:
            collection.delete(ids=ids)

        _retry("delete_by_source", _call, self._retry_count, self._timeout_seconds)
        return len(ids)

    def delete_by_ids(self, database_id: str, chunk_ids: Iterable[str]) -> int:
        id_list = list(chunk_ids)
        if not id_list:
            return 0
        collection = self._get_collection(database_id)

        def _call() -> None:
            collection.delete(ids=id_list)

        _retry("delete_by_ids", _call, self._retry_count, self._timeout_seconds)
        return len(id_list)

    def _query(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        where: dict[str, Any] | None,
    ) -> list[ScoredChunk]:
        collection = self._get_collection(database_id)
        count = collection.count()
        if count == 0:
            return []

        def _call() -> Any:
            return collection.query(
                query_embeddings=cast(Any, [query_vector]),
                n_results=min(top_k, count),
                where=cast(Any, where),
            )

        result = _retry("similarity_search", _call, self._retry_count, self._timeout_seconds)

        ids_row = (result.get("ids") or [[]])[0]
        documents_row = (result.get("documents") or [[]])[0]
        metadatas_row = (result.get("metadatas") or [[]])[0]
        distances_row = (result.get("distances") or [[]])[0]

        scored: list[ScoredChunk] = []
        for chunk_id, document, metadata, distance in zip(
            ids_row, documents_row, metadatas_row, distances_row, strict=True
        ):
            similarity = round(1.0 - distance, 4)
            chunk = _metadata_to_chunk(chunk_id, document, metadata or {})
            scored.append(ScoredChunk(chunk=chunk, vector_similarity=similarity))
        return scored

    def similarity_search(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        chunk_types: list[ChunkType] | None = None,
    ) -> list[ScoredChunk]:
        where = None
        if chunk_types:
            where = {"chunk_type": {"$in": [t.value for t in chunk_types]}}
        return self._query(database_id, query_vector, top_k, where)

    def similarity_search_with_filters(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        filters: dict[str, Any],
    ) -> list[ScoredChunk]:
        """`filters` is a plain `{metadata_field: value_or_list}` mapping,
        built server-side (see `retriever.py` -- never from a client-supplied
        payload, per this package's security posture). A list value becomes
        a Chroma `$in` filter; a scalar becomes an equality filter.
        """
        if not filters:
            return self._query(database_id, query_vector, top_k, None)
        clauses: list[dict[str, Any]] = []
        for field_name, value in filters.items():
            if isinstance(value, list):
                clauses.append({field_name: {"$in": value}})
            else:
                clauses.append({field_name: {"$eq": value}})
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}
        return self._query(database_id, query_vector, top_k, where)

    def count(self, database_id: str) -> int:
        return self._get_collection(database_id).count()

    def collection_info(self, database_id: str) -> CollectionInfo:
        collection = self._get_collection(database_id)
        total = collection.count()
        counts_by_type: dict[str, int] = {}
        dimensions: int | None = None
        if total > 0:
            # Untyped as `Any` -- Chroma's `Metadata` value type is a broad
            # union at the stub level (str/int/float/bool/...), matching
            # `embeddings/retriever.py`'s own identical, established
            # treatment of this same class of Chroma metadata read-back.
            all_metadata: list[Any] = collection.get(include=["metadatas"]).get("metadatas") or []
            for metadata in all_metadata:
                chunk_type = str((metadata or {}).get("chunk_type", "unknown"))
                counts_by_type[chunk_type] = counts_by_type.get(chunk_type, 0) + 1
                if dimensions is None and metadata.get("embedding_dimensions"):
                    dimensions = int(metadata["embedding_dimensions"])
        return CollectionInfo(
            name=self._collection_name(database_id),
            database_id=database_id,
            count=total,
            similarity_metric=self._settings.retrieval_similarity_metric,
            embedding_dimensions=dimensions,
            counts_by_type=counts_by_type,
        )

    def list_chunk_hashes(
        self, database_id: str, chunk_types: list[ChunkType] | None = None
    ) -> dict[str, str]:
        collection = self._get_collection(database_id)
        if collection.count() == 0:
            return {}
        where = {"chunk_type": {"$in": [t.value for t in chunk_types]}} if chunk_types else None
        result = collection.get(where=cast(Any, where), include=["metadatas"])
        ids = result.get("ids") or []
        metadatas: list[Any] = result.get("metadatas") or []
        return {
            chunk_id: str((metadata or {}).get("content_hash", ""))
            for chunk_id, metadata in zip(ids, metadatas, strict=True)
        }

    def drop_collection(self, database_id: str) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._client.delete_collection(self._collection_name(database_id))

    def close(self) -> None:
        # The underlying PersistentClient is a process-lifetime singleton
        # shared with every other Chroma-backed module (see
        # `embeddings.schema_indexer._cached_chroma_client`'s docstring for
        # why it must never be closed/recreated mid-process) -- this is a
        # deliberate no-op, present only to satisfy the `VectorStore`
        # interface for a future backend that does own a real connection.
        return None


def get_vector_store(settings: Settings) -> VectorStore:
    """Factory -- returns the configured `VectorStore` implementation.

    Chroma is the only implementation today (see this module's own
    docstring for why); this indirection exists so `ingestion.py`/
    `retriever.py` never construct `ChromaVectorStore` directly, keeping a
    future second backend a one-function change.
    """
    return ChromaVectorStore(settings)
