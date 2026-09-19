"""Tests for eval/web_search_evaluators.py -- pure grading logic, no real
Tavily call or LLM needed."""

from __future__ import annotations

from eval.web_search_evaluators import (
    evaluate_citation_fabrication,
    evaluate_source_relevance,
    extract_cited_urls,
)
from rag.graph import Citation


def _citation(url: str) -> Citation:
    return {
        "filename": url,
        "chunk_index": 0,
        "page_number": None,
        "document_id": "",
        "has_pdf_bytes": False,
    }


class TestExtractCitedUrls:
    def test_extracts_a_single_markdown_link(self):
        text = "Revenue grew 12% in 2025 ([Reuters](https://reuters.com/article))."

        assert extract_cited_urls(text) == frozenset({"https://reuters.com/article"})

    def test_extracts_multiple_distinct_links(self):
        text = (
            "According to a live web search:\n\n"
            "## Overview\n"
            "Rates rose ([Fed](https://fed.gov/a)) while inflation eased "
            "([BLS](https://bls.gov/b))."
        )

        assert extract_cited_urls(text) == frozenset({"https://fed.gov/a", "https://bls.gov/b"})

    def test_a_repeated_link_is_deduplicated(self):
        text = "See [source](https://example.com/x) and again [source](https://example.com/x)."

        assert extract_cited_urls(text) == frozenset({"https://example.com/x"})

    def test_no_markdown_links_returns_an_empty_set(self):
        assert extract_cited_urls("Plain prose with no citations at all.") == frozenset()

    def test_ignores_a_non_http_link(self):
        """Only real http(s) URLs count -- a stray `[text](#anchor)` or
        `[text](mailto:a@b.com)` isn't a citation."""
        text = "See [note](#appendix) for detail."

        assert extract_cited_urls(text) == frozenset()


class TestEvaluateCitationFabrication:
    def test_no_fabrication_when_every_cited_url_was_a_real_result(self):
        citations = [_citation("https://reuters.com/a"), _citation("https://bls.gov/b")]
        answer = "Rates rose ([Reuters](https://reuters.com/a))."

        result = evaluate_citation_fabrication(answer, citations)

        assert not result.has_fabrication
        assert result.fabrication_rate == 0.0

    def test_flags_a_url_the_model_cited_that_was_never_a_real_search_result(self):
        """The exact scenario this module exists to catch: the model
        invents a plausible-looking URL that was never actually returned
        by Tavily."""
        citations = [_citation("https://real-result.com/a")]
        answer = "Some claim ([Made Up Source](https://not-a-real-result.com/fake))."

        result = evaluate_citation_fabrication(answer, citations)

        assert result.has_fabrication
        assert result.fabricated_urls == frozenset({"https://not-a-real-result.com/fake"})
        assert result.fabrication_rate == 1.0

    def test_partial_fabrication_rate_with_a_mix_of_real_and_fabricated_citations(self):
        citations = [_citation("https://real.com/a")]
        answer = "Claim one ([Real](https://real.com/a)). Claim two ([Fake](https://fake.com/b))."

        result = evaluate_citation_fabrication(answer, citations)

        assert result.cited_url_count == 2
        assert result.fabrication_rate == 0.5

    def test_no_inline_citations_at_all_is_not_treated_as_fabrication(self):
        """An answer with zero markdown links has nothing to be wrong
        about -- a `fabrication_rate` of 0.0, not a divide-by-zero or a
        false positive."""
        citations = [_citation("https://real.com/a")]

        result = evaluate_citation_fabrication("A plain-prose answer with no links.", citations)

        assert not result.has_fabrication
        assert result.fabrication_rate == 0.0
        assert result.cited_url_count == 0

    def test_empty_search_results_with_a_cited_url_is_full_fabrication(self):
        result = evaluate_citation_fabrication("See [source](https://x.com/y).", [])

        assert result.has_fabrication
        assert result.fabrication_rate == 1.0


class TestEvaluateSourceRelevance:
    def test_all_results_relevant_when_every_url_matches_an_expected_domain(self):
        result = evaluate_source_relevance(
            ["https://reuters.com/a", "https://reuters.com/b"], frozenset({"reuters.com"})
        )

        assert result.relevance_rate == 1.0
        assert result.relevant_count == 2

    def test_partial_relevance_when_some_results_are_off_domain(self):
        result = evaluate_source_relevance(
            ["https://reuters.com/a", "https://randomblog.example/b"], frozenset({"reuters.com"})
        )

        assert result.relevance_rate == 0.5

    def test_no_expected_domains_is_a_vacuous_full_relevance(self):
        result = evaluate_source_relevance(["https://anything.example/a"], frozenset())

        assert result.relevance_rate == 1.0

    def test_no_results_at_all_with_a_real_expectation_is_zero_relevance(self):
        result = evaluate_source_relevance([], frozenset({"reuters.com"}))

        assert result.relevance_rate == 0.0
        assert result.result_count == 0

    def test_matches_against_any_of_several_expected_domains(self):
        result = evaluate_source_relevance(
            ["https://bls.gov/a"], frozenset({"reuters.com", "bls.gov", "fed.gov"})
        )

        assert result.relevance_rate == 1.0
