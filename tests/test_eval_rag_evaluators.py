"""Tests for eval/rag_evaluators.py -- pure grading logic, no live Chroma/
LLM/database needed (mirrors eval/test_eval_evaluators.py's own posture
for the SQL evaluators)."""

from __future__ import annotations

from eval.rag_evaluators import (
    evaluate_citation_correctness,
    evaluate_citation_coverage,
    evaluate_retrieval,
)
from rag.graph import Citation
from rag.store import ChunkResult


def _chunk(document_id: str, filename: str, chunk_index: int = 0) -> ChunkResult:
    return ChunkResult(
        chunk_text="some text",
        similarity=0.9,
        document_id=document_id,
        filename=filename,
        chunk_index=chunk_index,
        page_number=1,
        sensitivity_category=None,
        has_pdf_bytes=False,
    )


def _citation(
    document_id: str, filename: str, chunk_index: int = 0, page_number: int | None = None
) -> Citation:
    return {
        "filename": filename,
        "chunk_index": chunk_index,
        "page_number": page_number,
        "document_id": document_id,
        "has_pdf_bytes": False,
    }


class TestEvaluateRetrieval:
    def test_full_recall_and_precision_when_only_expected_documents_are_retrieved(self):
        chunks = [_chunk("d1", "policy.pdf"), _chunk("d1", "policy.pdf", chunk_index=1)]

        result = evaluate_retrieval(chunks, frozenset({"policy.pdf"}))

        assert result.recall == 1.0
        assert result.precision == 1.0
        assert result.missing_documents == frozenset()

    def test_partial_recall_when_an_expected_document_is_never_retrieved(self):
        chunks = [_chunk("d1", "policy.pdf")]

        result = evaluate_retrieval(chunks, frozenset({"policy.pdf", "handbook.pdf"}))

        assert result.recall == 0.5
        assert result.missing_documents == frozenset({"handbook.pdf"})

    def test_precision_drops_when_irrelevant_documents_are_also_retrieved(self):
        chunks = [_chunk("d1", "policy.pdf"), _chunk("d2", "unrelated.pdf")]

        result = evaluate_retrieval(chunks, frozenset({"policy.pdf"}))

        assert result.recall == 1.0
        assert result.precision == 0.5

    def test_no_expected_documents_is_a_vacuous_full_recall(self):
        chunks = [_chunk("d1", "whatever.pdf")]

        result = evaluate_retrieval(chunks, frozenset())

        assert result.recall == 1.0
        assert result.expected_count == 0

    def test_no_chunks_retrieved_at_all_gives_zero_recall_for_a_real_expectation(self):
        result = evaluate_retrieval([], frozenset({"policy.pdf"}))

        assert result.recall == 0.0
        assert result.missing_documents == frozenset({"policy.pdf"})


class TestEvaluateCitationCorrectness:
    def test_all_correct_when_every_citation_matches_a_retrieved_chunk(self):
        chunks = [_chunk("d1", "policy.pdf", chunk_index=0)]
        citations = [_citation("d1", "policy.pdf", chunk_index=0, page_number=1)]

        result = evaluate_citation_correctness(citations, chunks)

        assert result.all_correct
        assert result.correctness_rate == 1.0
        assert result.fabricated == ()

    def test_flags_a_citation_whose_document_id_was_never_retrieved(self):
        chunks = [_chunk("d1", "policy.pdf", chunk_index=0)]
        fabricated_citation = _citation("d-does-not-exist", "invented.pdf")

        result = evaluate_citation_correctness([fabricated_citation], chunks)

        assert not result.all_correct
        assert result.fabricated == (fabricated_citation,)
        assert result.correctness_rate == 0.0

    def test_flags_a_citation_with_the_right_document_but_wrong_chunk_index(self):
        """A real document that WAS retrieved, but at a different chunk
        index than the citation claims -- still a fabrication, since the
        specific cited passage was never actually seen."""
        chunks = [_chunk("d1", "policy.pdf", chunk_index=0)]
        wrong_chunk_citation = _citation("d1", "policy.pdf", chunk_index=7)

        result = evaluate_citation_correctness([wrong_chunk_citation], chunks)

        assert not result.all_correct

    def test_no_citations_at_all_is_vacuously_correct(self):
        result = evaluate_citation_correctness([], [_chunk("d1", "policy.pdf")])

        assert result.all_correct
        assert result.correctness_rate == 1.0


class TestEvaluateCitationCoverage:
    def test_a_succeeded_answer_with_no_citations_fails_coverage(self):
        assert evaluate_citation_coverage([], "succeeded") is False

    def test_a_succeeded_answer_with_citations_passes(self):
        citation = _citation("d1", "policy.pdf")
        assert evaluate_citation_coverage([citation], "succeeded") is True

    def test_an_insufficient_information_answer_needs_no_citations(self):
        assert evaluate_citation_coverage([], "insufficient_information") is True

    def test_a_restricted_answer_needs_no_citations(self):
        assert evaluate_citation_coverage([], "restricted") is True
