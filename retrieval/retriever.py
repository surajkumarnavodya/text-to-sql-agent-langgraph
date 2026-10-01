"""Retrieval orchestration -- the module `agent.nodes.retrieve_business_context_node`
calls, and the only entry point in this package a LangGraph node touches
directly.

Pipeline, per question:
    1. Skip entirely if `Settings.enable_business_context_retrieval` is off,
       or the target database's collection is missing/empty -- both are
       "nothing to add," not failures.
    2. Embed the question once (the same text is used as the query vector
       for every chunk type below -- see this module's docstring note on
       why no separate term/metric/table extraction step exists).
    3. Query each chunk type's own bounded top-k (`Settings
       .retrieval_top_k_<type>`) with a similarity-threshold floor and a
       small over-fetch buffer, so post-filtering (role visibility,
       threshold) has room to trim without starving a type entirely.
    4. Merge every type's candidates, deduplicate by content hash, rerank
       (`reranker.rerank` -- vector similarity + type priority + exact-term
       bonus + diversity), then trim to the character/token context budget.
    5. Return a `RetrievalResult` -- never raises. Any embedding or
       vector-store failure at any step degrades to an empty result plus a
       warning, exactly like `retrieve_golden_examples_node`'s existing
       fail-open contract for a *different* Chroma-backed accuracy aid.

**Why one embedded query instead of separate business-term/metric/table/
column/time-concept extraction steps**: the task this package was built to
satisfy calls for identifying each of those categories explicitly before
searching. This implementation instead relies on per-type semantic search
(the glossary collection is asked the same question a business-term
extractor would have been asked to isolate terms from, etc.) plus
`reranker.py`'s lexical exact-match bonus for precision -- the same "avoid a
second, separate extraction pass duplicating what vector similarity already
does reasonably well" reasoning `embeddings/retriever.py`'s own docstring
gives for *not* re-widening its primary candidate pool. A dedicated
NL-understanding step (regex/NER-based term-boundary detection, explicit
date-range parsing) is a materially larger, separate feature -- named as a
known limitation in `docs/vector-retrieval-design.md`, not silently skipped.

**Security**: `caller_roles` always comes from `AgentState["caller_roles"]`
(set once by the authenticated request handler, `api/auth.py` ->
`agent.graph.run_agent`) -- this module never accepts a client-supplied
role/tenant filter. A chunk whose `allowed_roles` doesn't clear
`caller_roles` is dropped before it ever reaches the prompt, the same
"enforce authorized filters from server-side identity only" rule
`agent/authz.py`'s own docstring states for the rest of this codebase.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings
from retrieval.embeddings import EmbeddingError, EmbeddingProvider, get_embedding_provider
from retrieval.models import ChunkType, ScoredChunk
from retrieval.reranker import rerank
from retrieval.vector_store import VectorStore, VectorStoreError, get_vector_store
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)

# Over-fetch multiplier for each type's primary vector query, so role/
# threshold filtering has room to trim without starving a type down to
# fewer than its configured top-k just because a couple of candidates got
# filtered out.
_OVER_FETCH_MULTIPLIER = 2
_MAX_OVER_FETCH = 20


@dataclass(frozen=True)
class RetrievalResult:
    """Everything `agent.nodes.retrieve_business_context_node` needs to
    populate `AgentState`'s retrieval fields (see `agent/state.py`)."""

    items: list[ScoredChunk] = field(default_factory=list)
    query: str = ""
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.items


def extract_governing_metrics(items: list[ScoredChunk]) -> list[dict[str, Any]]:
    """Filters already-retrieved chunks down to **governing metrics**
    -- Prompt 10 (`10_GOVERNED_METRICS_CONTRACT.md`)'s "confirmed
    definitions take precedence" mechanism.

    Deliberately not a new vector query or a second matching pass:
    whatever `retrieve_business_context`'s own similarity search +
    rerank already judged relevant enough to return for this question
    *is* the governing-metric set -- this function only picks the
    `METRIC`-typed `business_concept` chunks back out of that already-
    computed result. Every such chunk is already guaranteed `PUBLISHED`
    (see `retrieval.chunking.business_concept_chunk_from_catalog_entry`'s
    own docstring: only a published entry is ever rendered into a chunk
    at all), so every dict this returns is `CONFIRMED_BUSINESS_TRUTH` by
    construction, never an unreviewed draft.

    Returns:
        One plain dict per governing metric (`business_name`,
        `approved_expression`, `aggregation`, `text` -- the chunk's own
        self-contained rendering, reused directly rather than re-derived)
        -- consumed by `agent.llm_client._build_mandatory_metrics_block`
        and `review_sql_against_metrics_from_llm`. Empty for a question
        with no governing metric, the common case.
    """
    governing: list[dict[str, Any]] = []
    for item in items:
        chunk = item.chunk
        if chunk.chunk_type != ChunkType.BUSINESS_CONCEPT:
            continue
        if chunk.extra.get("concept_type") != "metric":
            continue
        governing.append(
            {
                "business_name": chunk.extra.get("business_name") or chunk.source_id,
                "approved_expression": chunk.extra.get("approved_expression"),
                "aggregation": chunk.extra.get("aggregation"),
                "text": chunk.text,
            }
        )
    return governing


def _per_type_top_k(settings: Settings) -> dict[ChunkType, int]:
    return {
        ChunkType.TABLE: settings.retrieval_top_k_tables,
        ChunkType.COLUMN: settings.retrieval_top_k_columns,
        ChunkType.RELATIONSHIP: settings.retrieval_top_k_relationships,
        ChunkType.GLOSSARY: settings.retrieval_top_k_glossary,
        ChunkType.METRIC: settings.retrieval_top_k_metrics,
        ChunkType.SQL_EXAMPLE: settings.retrieval_top_k_sql_examples,
        ChunkType.DOCUMENTATION: settings.retrieval_top_k_documentation,
        ChunkType.BUSINESS_CONCEPT: settings.retrieval_top_k_business_concepts,
    }


def _filter_candidates(
    candidates: list[ScoredChunk],
    caller_roles: tuple[str, ...],
    similarity_threshold: float,
    top_k: int,
) -> list[ScoredChunk]:
    filtered = [
        c
        for c in candidates
        if c.vector_similarity >= similarity_threshold and c.chunk.is_visible_to(caller_roles)
    ]
    filtered.sort(key=lambda c: c.vector_similarity, reverse=True)
    return filtered[:top_k]


def _deduplicate(candidates: list[ScoredChunk]) -> list[ScoredChunk]:
    """Drops a later candidate whose chunk carries the same `content_hash`
    as one already kept -- two different chunk identities can still end up
    with identical rendered text (e.g. a table chunk and a documentation
    chunk both happening to describe the same thing near-verbatim); keeping
    both wastes context budget on redundant text."""
    seen_hashes: set[str] = set()
    seen_ids: set[str] = set()
    deduped: list[ScoredChunk] = []
    for candidate in candidates:
        chunk = candidate.chunk
        if chunk.chunk_id in seen_ids or chunk.content_hash in seen_hashes:
            continue
        seen_ids.add(chunk.chunk_id)
        seen_hashes.add(chunk.content_hash)
        deduped.append(candidate)
    return deduped


def _apply_context_budget(
    candidates: list[ScoredChunk], max_chars: int, max_tokens: int
) -> list[ScoredChunk]:
    """Trims an already-ranked list to a character/token budget.

    Tokens are approximated as `chars / 4` (this codebase has no tokenizer
    dependency anywhere -- see `retrieval.chunking._split_with_overlap`'s
    identical approximation note); the *tighter* of the two budgets wins.
    Always keeps at least the first candidate, even if its text alone
    exceeds the budget -- an empty result because the single best match was
    "too long" would be a worse outcome than one slightly-over-budget item.
    """
    effective_char_budget = min(max_chars, max_tokens * 4)
    kept: list[ScoredChunk] = []
    running_total = 0
    for candidate in candidates:
        text_len = len(candidate.chunk.text)
        if kept and running_total + text_len > effective_char_budget:
            break
        kept.append(candidate)
        running_total += text_len
    return kept


def retrieve_business_context(
    question: str,
    database_id: str,
    caller_roles: tuple[str, ...],
    settings: Settings,
    vector_store: VectorStore | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> RetrievalResult:
    """Retrieves and ranks business-context chunks for one question.

    Args:
        question: The user's natural-language question (already sanitized
            by `agent.input_guard.check_input`, same as every other node
            downstream of `sanitize_input`).
        database_id: Which configured database's collection to search --
            `AgentState["selected_database"]`, already resolved by
            `retrieve_schema_node` before this node runs.
        caller_roles: The authenticated caller's roles (`AgentState
            ["caller_roles"]`) -- the only source of role-based filtering
            this function ever applies (see this module's docstring).
        settings: Application settings.
        vector_store: Optional override (mainly for tests) -- defaults to
            `retrieval.vector_store.get_vector_store(settings)`.
        embedding_provider: Optional override (mainly for tests) -- defaults
            to `retrieval.embeddings.get_embedding_provider(settings)`.

    Returns:
        A `RetrievalResult` -- always, never raises. `warnings` is non-empty
        whenever retrieval degraded for any reason (disabled, empty index,
        embedding failure, vector-store failure); `items` is `[]` in every
        one of those cases, and the caller must proceed without this
        context, not treat it as a reason the question can't be answered.
    """
    start = time.perf_counter()
    if not settings.enable_business_context_retrieval:
        return RetrievalResult(query=question, metadata={"enabled": False})

    try:
        store = vector_store or get_vector_store(settings)
        provider = embedding_provider or get_embedding_provider(settings)

        health = store.health_check(database_id)
        if not health.ok:
            logger.warning(
                "[retrieve_business_context] vector store health check failed for "
                "database %r: %s",
                database_id,
                health.detail,
            )
            return RetrievalResult(
                query=question,
                warnings=[f"Business-context index unavailable: {health.detail}"],
                metadata={"database_id": database_id, "health_ok": False},
            )
        if not health.chunk_count:
            logger.info(
                "[retrieve_business_context] no chunks indexed yet for database %r "
                "(run `python -m scripts.ingest_schema --database-id %s`)",
                database_id,
                database_id,
            )
            return RetrievalResult(
                query=question,
                warnings=["Business-context index is empty for this database."],
                metadata={"database_id": database_id, "chunk_count": 0},
            )

        query_vector = provider.embed_text(question)

        per_type_top_k = _per_type_top_k(settings)
        all_candidates: list[ScoredChunk] = []
        counts_by_type: dict[str, int] = {}
        for chunk_type, top_k in per_type_top_k.items():
            if top_k <= 0:
                continue
            over_fetch = min(top_k * _OVER_FETCH_MULTIPLIER, _MAX_OVER_FETCH)
            raw = store.similarity_search(
                database_id, query_vector, top_k=over_fetch, chunk_types=[chunk_type]
            )
            selected = _filter_candidates(
                raw, caller_roles, settings.retrieval_similarity_threshold, top_k
            )
            counts_by_type[chunk_type.value] = len(selected)
            all_candidates.extend(selected)

        deduped = _deduplicate(all_candidates)
        ranked = rerank(
            deduped,
            question,
            rerank_weight=settings.retrieval_rerank_weight,
            diversity_weight=settings.retrieval_diversity_weight,
        )
        final_items = _apply_context_budget(
            ranked, settings.retrieval_max_context_chars, settings.retrieval_max_context_tokens
        )

        duration_ms = (time.perf_counter() - start) * 1000
        warnings: list[str] = []
        if not final_items:
            warnings.append("No business-context chunks cleared the similarity threshold.")

        logger.info(
            "[retrieve_business_context] database=%r candidates=%d final=%d "
            "duration_ms=%.1f counts_by_type=%s",
            database_id,
            len(all_candidates),
            len(final_items),
            duration_ms,
            counts_by_type,
        )
        return RetrievalResult(
            items=final_items,
            query=question,
            sources=[item.chunk.chunk_id for item in final_items],
            warnings=warnings,
            metadata={
                "database_id": database_id,
                "candidate_count": len(all_candidates),
                "final_count": len(final_items),
                "counts_by_type": counts_by_type,
                "duration_ms": round(duration_ms, 1),
            },
        )
    except (EmbeddingError, VectorStoreError) as exc:
        safe_detail = redact_secrets(str(exc), settings)
        logger.warning(
            "[retrieve_business_context] retrieval failed for database %r, "
            "proceeding without business context: %s",
            database_id,
            safe_detail,
        )
        return RetrievalResult(
            query=question,
            warnings=[f"Business-context retrieval unavailable: {safe_detail}"],
            metadata={"database_id": database_id, "error": True},
        )
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        safe_detail = redact_secrets(str(exc), settings)
        logger.warning(
            "[retrieve_business_context] unexpected failure for database %r, "
            "proceeding without business context: %s",
            database_id,
            safe_detail,
        )
        return RetrievalResult(
            query=question,
            warnings=[f"Business-context retrieval failed unexpectedly: {safe_detail}"],
            metadata={"database_id": database_id, "error": True},
        )
