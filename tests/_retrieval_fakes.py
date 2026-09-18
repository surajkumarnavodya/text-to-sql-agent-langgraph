"""Shared test doubles for `retrieval/` tests.

Not a test module itself (no `test_` prefix -- pytest won't collect it).
`InMemoryVectorStore` implements the full `VectorStore` interface entirely
in a Python dict, with brute-force cosine-similarity search -- no chromadb,
no disk I/O, no network. This is what lets `tests/test_ingestion.py`/
`tests/test_retrieval.py`/`tests/test_vector_fallback.py` exercise the real
`ingestion.py`/`retriever.py` orchestration logic without depending on a
real ChromaDB instance at all, matching this project's existing convention
that every test under `tests/` runs fully mocked/offline.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from retrieval.models import Chunk, ChunkType, ScoredChunk
from retrieval.vector_store import CollectionInfo, HealthCheckResult, VectorStore


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (norm_a * norm_b)


class InMemoryVectorStore(VectorStore):
    """A `VectorStore` backed by a plain dict -- see this module's docstring."""

    def __init__(self) -> None:
        # {database_id: {chunk_id: (Chunk, vector)}}
        self._data: dict[str, dict[str, tuple[Chunk, list[float]]]] = {}
        self.fail_next_upsert = False
        self.fail_health_check = False
        self.fail_query = False

    def create_collection_if_missing(self, database_id: str) -> None:
        self._data.setdefault(database_id, {})

    def health_check(self, database_id: str) -> HealthCheckResult:
        if self.fail_health_check:
            return HealthCheckResult(ok=False, detail="simulated failure")
        count = len(self._data.get(database_id, {}))
        return HealthCheckResult(ok=True, detail="ok", chunk_count=count)

    def upsert_documents(
        self, database_id: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        if self.fail_next_upsert:
            from retrieval.vector_store import VectorStoreError

            raise VectorStoreError("simulated upsert failure")
        bucket = self._data.setdefault(database_id, {})
        for chunk, vector in zip(chunks, vectors, strict=True):
            bucket[chunk.chunk_id] = (chunk, vector)

    def delete_by_source(self, database_id: str, source_ids: Iterable[str]) -> int:
        source_id_set = set(source_ids)
        bucket = self._data.get(database_id, {})
        to_delete = [cid for cid, (chunk, _) in bucket.items() if chunk.source_id in source_id_set]
        for cid in to_delete:
            del bucket[cid]
        return len(to_delete)

    def delete_by_ids(self, database_id: str, chunk_ids: Iterable[str]) -> int:
        bucket = self._data.get(database_id, {})
        count = 0
        for cid in list(chunk_ids):
            if cid in bucket:
                del bucket[cid]
                count += 1
        return count

    def similarity_search(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        chunk_types: list[ChunkType] | None = None,
    ) -> list[ScoredChunk]:
        if self.fail_query:
            from retrieval.vector_store import VectorStoreError

            raise VectorStoreError("simulated query failure")
        bucket = self._data.get(database_id, {})
        candidates = [
            (chunk, vector)
            for chunk, vector in bucket.values()
            if chunk_types is None or chunk.chunk_type in chunk_types
        ]
        scored = [
            ScoredChunk(
                chunk=chunk, vector_similarity=round(_cosine_similarity(query_vector, vector), 4)
            )
            for chunk, vector in candidates
        ]
        scored.sort(key=lambda s: s.vector_similarity, reverse=True)
        return scored[:top_k]

    def similarity_search_with_filters(
        self,
        database_id: str,
        query_vector: list[float],
        top_k: int,
        filters: dict,
    ) -> list[ScoredChunk]:
        bucket = self._data.get(database_id, {})

        def matches(chunk: Chunk) -> bool:
            for field_name, value in filters.items():
                actual = getattr(chunk, field_name, None)
                if isinstance(value, list):
                    if actual not in value:
                        return False
                elif actual != value:
                    return False
            return True

        candidates = [(chunk, vector) for chunk, vector in bucket.values() if matches(chunk)]
        scored = [
            ScoredChunk(
                chunk=chunk, vector_similarity=round(_cosine_similarity(query_vector, vector), 4)
            )
            for chunk, vector in candidates
        ]
        scored.sort(key=lambda s: s.vector_similarity, reverse=True)
        return scored[:top_k]

    def count(self, database_id: str) -> int:
        return len(self._data.get(database_id, {}))

    def collection_info(self, database_id: str) -> CollectionInfo:
        bucket = self._data.get(database_id, {})
        counts_by_type: dict[str, int] = {}
        for chunk, _ in bucket.values():
            counts_by_type[chunk.chunk_type.value] = (
                counts_by_type.get(chunk.chunk_type.value, 0) + 1
            )
        return CollectionInfo(
            name=f"fake__{database_id}",
            database_id=database_id,
            count=len(bucket),
            similarity_metric="cosine",
            counts_by_type=counts_by_type,
        )

    def list_chunk_hashes(
        self, database_id: str, chunk_types: list[ChunkType] | None = None
    ) -> dict[str, str]:
        bucket = self._data.get(database_id, {})
        return {
            cid: chunk.content_hash
            for cid, (chunk, _) in bucket.items()
            if chunk_types is None or chunk.chunk_type in chunk_types
        }

    def drop_collection(self, database_id: str) -> None:
        self._data.pop(database_id, None)

    def close(self) -> None:
        return None
