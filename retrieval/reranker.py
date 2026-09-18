"""Deterministic post-retrieval scoring: no external reranker model or API.

This project has no reranking-model dependency (no cross-encoder, no hosted
reranking API), and adding one purely for this feature would be a real,
new, unrelated dependency for a small-schema, single-user-oriented app (see
`docs/vector-retrieval-design.md`'s vector-database-selection section for
the same "don't add infrastructure this project's own stated scale doesn't
justify" reasoning). Instead, `rerank()` combines four cheap, explainable
signals into one final score:

    final_score = (vector_similarity * rerank_weight)
                + (chunk_type_priority * (1 - rerank_weight))
                + exact_term_match_bonus
                - diversity_penalty

1. **Vector similarity** -- the raw cosine score from `vector_store.py`.
2. **Chunk-type priority** -- a fixed weight per `ChunkType` (see
   `_TYPE_PRIORITY`), reflecting that a `metric`/`relationship` chunk
   answering "how do I compute/join this" is usually more directly useful
   to SQL generation than a `documentation` chunk is, all else equal.
3. **Exact term match** -- a flat bonus when a distinctive question keyword
   (same cheap extraction `embeddings/retriever.py`'s own lexical-bonus
   fallback uses) appears verbatim in the chunk's text -- a literal
   business-term match is strong evidence even when the embedding
   similarity alone is middling.
4. **Diversity penalty** -- applied *after* the first three are computed,
   via a greedy re-selection (`_diversify`, an MMR-style loop): each time a
   chunk is selected, every remaining candidate sharing that chunk's
   `(chunk_type, table_name)` pair is discounted by `diversity_weight`
   before the next pick -- this is what keeps eight near-duplicate `column`
   chunks from the same table crowding out a `metric`/`glossary` chunk that
   would otherwise never make the cut.

`rerank_weight` and `diversity_weight` are both configurable
(`Settings.retrieval_rerank_weight`/`retrieval_diversity_weight`) so this
formula can be tuned without a code change -- see
`docs/retrieval-evaluation.md` for how to judge whether a change helped.
"""

from __future__ import annotations

import re

from retrieval.models import ChunkType, ScoredChunk

# Reflects which chunk type most directly answers "what do I write in the
# SQL" versus "background reading" -- tunable, not a claim of universal
# correctness. A metric/relationship chunk usually resolves a concrete
# generation question (a formula, a join); documentation is the most
# open-ended/background type, so it's weighted lowest.
_TYPE_PRIORITY: dict[ChunkType, float] = {
    ChunkType.METRIC: 1.0,
    ChunkType.RELATIONSHIP: 0.95,
    ChunkType.GLOSSARY: 0.9,
    ChunkType.SQL_EXAMPLE: 0.85,
    ChunkType.TABLE: 0.8,
    ChunkType.COLUMN: 0.7,
    ChunkType.DOCUMENTATION: 0.6,
}

_EXACT_MATCH_BONUS = 0.12
_MIN_KEYWORD_LENGTH = 4
_WORD_RE = re.compile(r"[a-zA-Z]+")
_STOPWORDS = frozenset(
    {
        "show",
        "list",
        "find",
        "give",
        "many",
        "much",
        "what",
        "which",
        "when",
        "where",
        "were",
        "have",
        "does",
        "with",
        "that",
        "this",
        "from",
        "total",
        "table",
        "tables",
        "data",
        "value",
        "values",
        "count",
        "number",
        "amount",
    }
)


def extract_keywords(question: str) -> set[str]:
    """Distinctive lowercased words from `question` -- same shape as
    `embeddings/retriever.py::_extract_keywords`, kept as an independent
    copy (not imported from there) since this package must not depend on
    `embeddings/` and the two lists are allowed to diverge over time."""
    words = _WORD_RE.findall(question.lower())
    return {w for w in words if len(w) >= _MIN_KEYWORD_LENGTH and w not in _STOPWORDS}


def _score_candidate(
    candidate: ScoredChunk, keywords: set[str], rerank_weight: float
) -> ScoredChunk:
    type_priority = _TYPE_PRIORITY.get(candidate.chunk.chunk_type, 0.5)
    text_lower = candidate.chunk.text.lower()
    exact_match = any(keyword in text_lower for keyword in keywords)
    bonus = _EXACT_MATCH_BONUS if exact_match else 0.0
    final_score = round(
        candidate.vector_similarity * rerank_weight + type_priority * (1 - rerank_weight) + bonus,
        4,
    )
    return candidate.model_copy(update={"final_score": final_score})


def _diversify(candidates: list[ScoredChunk], diversity_weight: float) -> list[ScoredChunk]:
    """Greedy MMR-style re-selection -- see this module's docstring."""
    remaining = list(candidates)
    selected: list[ScoredChunk] = []
    seen_counts: dict[tuple[str, str], int] = {}

    while remaining:
        best_index = -1
        best_effective_score = float("-inf")
        for index, item in enumerate(remaining):
            key = (item.chunk.chunk_type.value, item.chunk.table_name or "")
            penalty = diversity_weight * seen_counts.get(key, 0)
            effective_score = (item.final_score or 0.0) - penalty
            if effective_score > best_effective_score:
                best_effective_score = effective_score
                best_index = index
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        key = (chosen.chunk.chunk_type.value, chosen.chunk.table_name or "")
        seen_counts[key] = seen_counts.get(key, 0) + 1

    return selected


def rerank(
    candidates: list[ScoredChunk],
    question: str,
    rerank_weight: float,
    diversity_weight: float,
) -> list[ScoredChunk]:
    """Scores and diversity-reorders `candidates` -- see this module's docstring.

    Args:
        candidates: Raw vector-search hits (any order), possibly already
            deduplicated by `retriever.py` (deduplication happens before
            this call, not here -- reranking a duplicate wastes the
            diversity budget on an entry that will just be filtered anyway).
        question: The user's natural-language question, for exact-term matching.
        rerank_weight: 0..1 -- how much of `final_score` comes from raw
            vector similarity vs. chunk-type priority (see this module's docstring).
        diversity_weight: 0..1 -- how strongly a repeated `(chunk_type,
            table_name)` pair is discounted on subsequent picks.

    Returns:
        `candidates`, reordered by `final_score` descending after diversity
        adjustment -- every entry's `final_score` is set (never None).
    """
    if not candidates:
        return []
    keywords = extract_keywords(question)
    scored = [_score_candidate(c, keywords, rerank_weight) for c in candidates]
    return _diversify(scored, diversity_weight)
