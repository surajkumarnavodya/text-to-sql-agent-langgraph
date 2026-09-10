"""Top-k retrieval + LLM relevance grading + query rewriting for the agentic
RAG subgraph (`rag/graph.py`).

Mirrors `embeddings/retriever.py`'s shape (embed question -> top-k similarity
search) for the schema-retrieval path, but scoped to one `rag.store`
collection instead of a Chroma collection, and adds the two steps that make
this "agentic" rather than a single fixed retrieval: grading whether what
came back actually addresses the question, and rewriting the query to try
again if not (see `rag/graph.py` for how these compose into the bounded
retry loop).
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine

from config.settings import Settings
from rag.embedding import embed_texts
from rag.llm import call_ollama
from rag.store import ChunkResult, Collection, similarity_search

logger = logging.getLogger(__name__)

_GRADE_SYSTEM_PROMPT = (
    "You judge whether retrieved document excerpts actually answer a "
    "question. Respond with exactly one word: YES if the excerpts contain "
    "enough information to answer the question, NO if they don't or are "
    "off-topic. No other text."
)

_REWRITE_SYSTEM_PROMPT = (
    "You rewrite a search query to improve document retrieval, based on why "
    "the previous search came back irrelevant. Respond with ONLY the "
    "rewritten query text -- no explanation, no quotes, no commentary."
)


def retrieve(
    engine: Engine, collection: Collection, question: str, top_k: int, settings: Settings
) -> list[ChunkResult]:
    """Embeds `question` and returns the top-k most similar chunks in `collection`."""
    query_embedding = embed_texts([question], settings)[0]
    return similarity_search(engine, collection, query_embedding, top_k)


def grade_relevance(question: str, chunks: list[ChunkResult], settings: Settings) -> bool:
    """Asks the LLM whether the retrieved chunks actually address the question.

    A cheap, single-word-response call (same pattern as `agent.followup`'s
    heuristic vs. this being an actual LLM judgment call -- unlike follow-up
    classification, "is this excerpt relevant" genuinely needs semantic
    judgment, not a regex). Empty `chunks` is graded false without a wasted
    LLM call -- there is nothing to judge.
    """
    if not chunks:
        return False
    excerpts = "\n\n".join(f"[{c.filename}] {c.chunk_text[:500]}" for c in chunks)
    user_prompt = f"Question: {question}\n\nRetrieved excerpts:\n{excerpts}\n\nDo these excerpts answer the question?"
    response = call_ollama(_GRADE_SYSTEM_PROMPT, user_prompt, settings, max_tokens=5)
    return response.strip().upper().startswith("Y")


def rewrite_query(question: str, prior_chunks: list[ChunkResult], settings: Settings) -> str:
    """Rewrites the question to try to retrieve more relevant chunks.

    Given the prior (unhelpful) retrieval's chunk titles as context, so the
    rewrite can deliberately steer away from whatever it already tried,
    rather than blindly rephrasing in a way that might retrieve the exact
    same chunks again.
    """
    prior_titles = ", ".join(sorted({c.filename for c in prior_chunks})) or "(nothing retrieved)"
    user_prompt = (
        f"Original question: {question}\n"
        f"Previous search retrieved excerpts from: {prior_titles}, which did not "
        "answer the question. Rewrite the search query to find more relevant content."
    )
    rewritten = call_ollama(_REWRITE_SYSTEM_PROMPT, user_prompt, settings, max_tokens=100)
    return rewritten or question
