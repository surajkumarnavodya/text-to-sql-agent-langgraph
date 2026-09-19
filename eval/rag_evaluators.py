"""Evaluators for the document/policy RAG subgraph (`rag/graph.py`) --
the RAG-specific counterpart to `eval/evaluators.py`'s Text-to-SQL
evaluators. Closes a real, previously-disclosed gap:
`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md` confirmed
`eval/evaluators.py` has zero RAG-specific coverage today.

Every function here is pure (no network/DB/LLM calls) given real
`rag.store.ChunkResult`/`rag.graph.Citation` values, mirroring
`eval/evaluators.py`'s own "grading is pure, `eval/runner.py`/a future
`eval/rag_runner.py` owns talking to the live agent" separation.

Two metrics, deliberately not more:

- **Retrieval recall/precision** — did the retriever surface the
  documents a human would expect for this question, and how much noise
  came with them. The direct RAG counterpart of
  `eval/evaluators.py::evaluate_retrieval`'s schema-table recall for SQL.
- **Citation correctness** — every `Citation` the RAG subgraph produced
  must trace back to a chunk that was *actually retrieved*, never a
  fabricated `(document_id, chunk_index)` pair. `rag/graph.py`'s
  `_generate_node` already builds `citations` deterministically from the
  real retrieved `ChunkResult` list (confirmed by reading that code, not
  assumed) — this evaluator turns that architectural guarantee into a
  regression check, the same way `eval/evaluators.py::evaluate_sql_exact_match`
  locks in a property that "should always hold" so a future change that
  quietly breaks it gets caught.

Deliberately NOT attempted here: an LLM-judged "faithfulness" score (does
every sentence in the answer trace to a chunk). That requires either a
second LLM call (a real, separate cost/complexity/reliability tradeoff) or
a much more elaborate NLI-style pipeline — out of scope for this pass;
citation correctness is the deterministic, honest substitute this
codebase's own "never fabricate" principle actually requires.
"""

from __future__ import annotations

from dataclasses import dataclass

from rag.graph import Citation
from rag.store import ChunkResult


@dataclass(frozen=True)
class RetrievalResult:
    """Recall/precision of retrieval against a human-labeled expected set.

    Mirrors `eval/evaluators.py::evaluate_retrieval`'s shape (recall
    fraction over an expected set) but scoped to RAG document filenames
    instead of SQL schema table names.
    """

    expected_count: int
    retrieved_relevant_count: int
    retrieved_total_count: int
    recall: float
    precision: float
    missing_documents: frozenset[str]


def evaluate_retrieval(
    retrieved_chunks: list[ChunkResult], expected_document_filenames: frozenset[str]
) -> RetrievalResult:
    """Recall = fraction of `expected_document_filenames` present among the
    retrieved chunks' own filenames; precision = fraction of retrieved
    chunks whose filename is in the expected set.

    An empty `expected_document_filenames` (a question with no single
    "correct" source document -- e.g. a broad summarization request)
    yields `recall=1.0` by convention (nothing was missed, vacuously),
    matching `eval/evaluators.py::evaluate_retrieval`'s own documented
    convention for the identical edge case.
    """
    retrieved_filenames = {chunk.filename for chunk in retrieved_chunks}
    if not expected_document_filenames:
        recall = 1.0
        retrieved_relevant_count = 0
        missing_documents: frozenset[str] = frozenset()
    else:
        found = expected_document_filenames & retrieved_filenames
        retrieved_relevant_count = len(found)
        recall = len(found) / len(expected_document_filenames)
        missing_documents = expected_document_filenames - retrieved_filenames

    if not retrieved_chunks:
        precision = 1.0 if not expected_document_filenames else 0.0
    else:
        relevant_chunk_count = sum(
            1 for chunk in retrieved_chunks if chunk.filename in expected_document_filenames
        )
        precision = relevant_chunk_count / len(retrieved_chunks)

    return RetrievalResult(
        expected_count=len(expected_document_filenames),
        retrieved_relevant_count=retrieved_relevant_count,
        retrieved_total_count=len(retrieved_chunks),
        recall=round(recall, 4),
        precision=round(precision, 4),
        missing_documents=missing_documents,
    )


@dataclass(frozen=True)
class CitationCorrectnessResult:
    """Whether every citation traces back to an actually-retrieved chunk."""

    citation_count: int
    correct_count: int
    fabricated: tuple[Citation, ...]

    @property
    def all_correct(self) -> bool:
        return not self.fabricated

    @property
    def correctness_rate(self) -> float:
        if self.citation_count == 0:
            # No citations to be wrong about -- vacuously correct, distinct
            # from "verified 100% correct with real citations present."
            # Callers checking citation *coverage* (were there citations at
            # all) should use `evaluate_citation_coverage` below instead.
            return 1.0
        return round(self.correct_count / self.citation_count, 4)


def evaluate_citation_correctness(
    citations: list[Citation], retrieved_chunks: list[ChunkResult]
) -> CitationCorrectnessResult:
    """Every citation's `(document_id, chunk_index)` pair must match a real
    retrieved chunk -- never a fabricated reference. This is a real
    correctness property, not just a style check: a citation the UI can't
    actually trace back to retrieved evidence is exactly the "invented
    citation metadata" the master prompt's Part 14 forbids.
    """
    retrieved_keys = {(chunk.document_id, chunk.chunk_index) for chunk in retrieved_chunks}
    fabricated = tuple(
        citation
        for citation in citations
        if (citation["document_id"], citation["chunk_index"]) not in retrieved_keys
    )
    return CitationCorrectnessResult(
        citation_count=len(citations),
        correct_count=len(citations) - len(fabricated),
        fabricated=fabricated,
    )


def evaluate_citation_coverage(citations: list[Citation], status: str) -> bool:
    """A `succeeded` answer (not `insufficient_information`/`restricted`)
    should carry at least one citation -- an uncited "succeeded" answer is
    a real quality signal worth tracking (the model answered from
    retrieved context but the citation-building step produced nothing,
    which per `rag/graph.py`'s own design should only happen if `chunks`
    itself was empty, which shouldn't coincide with `status="succeeded"`).
    """
    if status != "succeeded":
        return True
    return len(citations) > 0
