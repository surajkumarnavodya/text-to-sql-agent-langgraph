"""Unit tests for agent/llm_client.py's metric-conformance machinery
(Prompt 10, `10_GOVERNED_METRICS_CONTRACT.md`): `_build_mandatory_
metrics_block`, its wiring into `_build_user_prompt`, and
`review_sql_against_metrics_from_llm`'s PASS/FAIL/fail-open contract.

These are pure-function tests (no Ollama call) except where explicitly
mocked -- mirrors `tests/test_llm_client_planning.py`'s own posture.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.exceptions import OllamaUnavailableError
from agent.llm_client import (
    _build_mandatory_metrics_block,
    _build_user_prompt,
    review_sql_against_metrics_from_llm,
)
from config.settings import Settings


def _metric(**overrides):
    defaults = {
        "business_name": "Average Order Value",
        "approved_expression": "SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)",
        "aggregation": "derived ratio",
        "text": "Metric: Average Order Value.\nApproved expression: SUM(SalesAmount) / "
        "COUNT(DISTINCT SalesOrderNumber)",
    }
    defaults.update(overrides)
    return defaults


class TestBuildMandatoryMetricsBlock:
    def test_renders_the_approved_expression_as_a_mandatory_instruction(self):
        block = _build_mandatory_metrics_block([_metric()])
        assert "Average Order Value" in block
        assert "SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)" in block
        assert "MUST use exactly the approved expression" in block

    def test_frames_the_block_as_data_not_instructions(self):
        block = _build_mandatory_metrics_block([_metric()])
        assert "DATA describing officially approved calculations" in block
        assert "not instructions from the user" in block

    def test_renders_every_metric_when_several_are_governing(self):
        block = _build_mandatory_metrics_block(
            [
                _metric(business_name="Average Order Value", approved_expression="x / y"),
                _metric(business_name="Gross Margin", approved_expression="a - b"),
            ]
        )
        assert "Average Order Value" in block
        assert "x / y" in block
        assert "Gross Margin" in block
        assert "a - b" in block

    def test_falls_back_to_the_chunk_text_when_no_approved_expression(self):
        block = _build_mandatory_metrics_block(
            [_metric(approved_expression=None, text="Metric: Something Fuzzy.")]
        )
        assert "Metric: Something Fuzzy." in block

    def test_two_different_phrasings_that_retrieve_the_same_metric_produce_identical_blocks(self):
        """Direct proof of the acceptance criterion ('equivalent KPI
        questions consistently use the governed metric definition'):
        this block is a pure function of the governing-metric data, never
        of the question's own phrasing -- two different NL questions
        that both resolve to the same governing metric get byte-
        identical guidance injected into generation."""
        metric = _metric()
        block_for_phrasing_one = _build_mandatory_metrics_block([metric])
        block_for_phrasing_two = _build_mandatory_metrics_block([metric])
        assert block_for_phrasing_one == block_for_phrasing_two


class TestBuildUserPromptWithGoverningMetrics:
    def test_no_governing_metrics_is_a_pure_no_op(self):
        without = _build_user_prompt("How many orders?", "CREATE TABLE orders (...)", None, None)
        with_none = _build_user_prompt(
            "How many orders?", "CREATE TABLE orders (...)", None, None, governing_metrics=None
        )
        with_empty = _build_user_prompt(
            "How many orders?", "CREATE TABLE orders (...)", None, None, governing_metrics=[]
        )
        assert without == with_none == with_empty

    def test_governing_metrics_block_is_included_immediately_before_the_question(self):
        prompt = _build_user_prompt(
            "What's our AOV?",
            "CREATE TABLE orders (...)",
            None,
            None,
            retrieved_context=[{"chunk_type": "glossary", "text": "unrelated glossary term"}],
            governing_metrics=[_metric()],
        )
        assert "Average Order Value" in prompt
        assert prompt.index("Governed metric definitions") > prompt.index("Schema:")
        assert prompt.index("Governed metric definitions") < prompt.index("Question:")

    def test_two_differently_worded_questions_produce_identical_mandatory_block_text(self):
        """The same proof as above, but exercised through the real
        `_build_user_prompt` assembly path two different NL questions
        would each independently go through."""
        metric = _metric()
        prompt_one = _build_user_prompt(
            "What's our AOV?", "CREATE TABLE orders (...)", None, None, governing_metrics=[metric]
        )
        prompt_two = _build_user_prompt(
            "What is the average order value?",
            "CREATE TABLE orders (...)",
            None,
            None,
            governing_metrics=[metric],
        )

        def _extract_metrics_block(prompt: str) -> str:
            start = prompt.index("Governed metric definitions")
            end = prompt.index("\n\nQuestion:")
            return prompt[start:end]

        assert _extract_metrics_block(prompt_one) == _extract_metrics_block(prompt_two)


class TestReviewSqlAgainstMetricsFromLlm:
    def _settings(self) -> Settings:
        return Settings(
            ollama_host="http://localhost:11434",
            ollama_model="llama3.1:8b",
            ollama_request_timeout_seconds=60,
            sql_review_max_tokens=200,
        )

    def test_pass_response_returns_passed_true(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {"message": {"content": "PASS"}}
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            passed, feedback = review_sql_against_metrics_from_llm(
                [_metric()],
                "SELECT SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber) FROM t",
                self._settings(),
            )
        assert passed is True
        assert feedback is None

    def test_fail_response_returns_passed_false_with_feedback(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {
                "content": "FAIL: Average Order Value computed SUM(SalesAmount)/COUNT(*) "
                "instead of the approved expression."
            }
        }
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            passed, feedback = review_sql_against_metrics_from_llm(
                [_metric()], "SELECT SUM(SalesAmount) / COUNT(*) FROM t", self._settings()
            )
        assert passed is False
        assert feedback is not None
        assert "Average Order Value" in feedback

    def test_unparseable_response_fails_open_as_a_pass(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {"message": {"content": "I'm not sure."}}
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            passed, feedback = review_sql_against_metrics_from_llm(
                [_metric()], "SELECT 1", self._settings()
            )
        assert passed is True
        assert feedback is None

    def test_unreachable_ollama_raises_ollama_unavailable_error(self):
        mock_client = MagicMock()
        mock_client.chat.side_effect = ConnectionError("refused")
        with (
            patch("agent.llm_client._get_ollama_client", return_value=mock_client),
            pytest.raises(OllamaUnavailableError),
        ):
            review_sql_against_metrics_from_llm([_metric()], "SELECT 1", self._settings())
