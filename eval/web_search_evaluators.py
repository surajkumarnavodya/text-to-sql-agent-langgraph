"""Evaluators for the web-search source (`search/web_search.py`,
`agent.orchestrator.nodes.web_search_node`) -- the web-search-specific
counterpart to `eval/evaluators.py` (SQL) and `eval/rag_evaluators.py`
(document/policy RAG). Closes the same disclosed gap
`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md` flagged for RAG: zero
evaluation coverage existed for this source before this module.

**A real, non-obvious finding this module is built to catch**: reading
`web_search_node`'s own code shows its structured `citations` list is
*every* search result, unconditionally -- not the subset the LLM's
generated answer actually cited inline. The system prompt instructs the
model to "cite the specific result that supports each... claim... as a
markdown link... Never invent a URL or cite a source not in the results
below," but nothing anywhere verifies that instruction was followed. The
structured `citations` list being complete-by-construction means a
recall/precision metric against it would trivially and uselessly always
score 100% -- it can't catch the actual risk (the model inventing a URL
in its prose that isn't a real search result). `evaluate_citation_fabrication`
below is what actually checks that, by parsing the model's own markdown
links out of the answer text and cross-referencing them against the real
results -- the same "never fabricate citation metadata" principle
`eval/rag_evaluators.py` enforces structurally, applied here to free-text
generation instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag.graph import Citation

# Matches a markdown link: [display text](url) -- deliberately simple
# (no full CommonMark parser) since `web_search_node`'s own system prompt
# only ever asks the model to produce this one link shape.
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\((https?://[^\s)]+)\)")


def extract_cited_urls(answer_text: str) -> frozenset[str]:
    """Pulls every markdown-link URL out of a web-search answer's text."""
    return frozenset(match.group(2) for match in _MARKDOWN_LINK_PATTERN.finditer(answer_text))


@dataclass(frozen=True)
class CitationFabricationResult:
    """Whether every URL the model inline-cited is a URL that was actually
    returned by the search provider -- see this module's own docstring for
    why this, not a recall/precision check against the (complete-by-
    construction) structured `citations` list, is the metric that actually
    catches a fabricated source."""

    cited_url_count: int
    fabricated_urls: frozenset[str]

    @property
    def has_fabrication(self) -> bool:
        return bool(self.fabricated_urls)

    @property
    def fabrication_rate(self) -> float:
        if self.cited_url_count == 0:
            return 0.0
        return round(len(self.fabricated_urls) / self.cited_url_count, 4)


def evaluate_citation_fabrication(
    answer_text: str, citations: list[Citation]
) -> CitationFabricationResult:
    """`citations` here is `web_search_node`'s own structured list (one
    entry per real search result, `filename` holding that result's URL --
    see that node's own code) -- the ground truth of "URLs that genuinely
    came back from the search provider" to check the model's inline
    markdown links against.
    """
    cited_urls = extract_cited_urls(answer_text)
    real_urls = {citation["filename"] for citation in citations}
    fabricated = cited_urls - real_urls
    return CitationFabricationResult(cited_url_count=len(cited_urls), fabricated_urls=fabricated)


@dataclass(frozen=True)
class SourceRelevanceResult:
    """Fraction of the actual search results that match at least one
    expected domain/keyword substring -- a coarse but honest proxy for
    "did the search return results from where a human would expect,"
    without requiring a live, network-dependent relevance judgment."""

    result_count: int
    relevant_count: int
    relevance_rate: float


def evaluate_source_relevance(
    result_urls: list[str], expected_domain_substrings: frozenset[str]
) -> SourceRelevanceResult:
    """An empty `expected_domain_substrings` (no specific source expected
    for this question -- a genuinely open-ended query) yields
    `relevance_rate=1.0` by convention, the same vacuous-pass convention
    `eval/rag_evaluators.py::evaluate_retrieval` and
    `eval/evaluators.py::evaluate_retrieval` both already use for the
    identical "nothing specific was expected" case.
    """
    if not expected_domain_substrings:
        return SourceRelevanceResult(
            result_count=len(result_urls), relevant_count=len(result_urls), relevance_rate=1.0
        )
    if not result_urls:
        return SourceRelevanceResult(result_count=0, relevant_count=0, relevance_rate=0.0)

    relevant_count = sum(
        1 for url in result_urls if any(domain in url for domain in expected_domain_substrings)
    )
    return SourceRelevanceResult(
        result_count=len(result_urls),
        relevant_count=relevant_count,
        relevance_rate=round(relevant_count / len(result_urls), 4),
    )
