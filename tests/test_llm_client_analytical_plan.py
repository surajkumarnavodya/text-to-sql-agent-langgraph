"""Unit tests for agent/llm_client.py's structured-analytical-planning
machinery (Prompt 12, `12_ANALYTICAL_PLANNING_CONTRACT.md`):
`_parse_analytical_plan_response`'s fail-open contract,
`_build_analytical_plan_user_prompt`'s DATA-framing/context-reuse,
`_build_analytical_plan_block`'s rendering, and
`generate_analytical_plan_from_llm`'s mocked-Ollama call shape.

Mirrors `tests/test_llm_client_intent.py`'s own posture: pure-function
tests except where Ollama is explicitly mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.exceptions import OllamaUnavailableError
from agent.llm_client import (
    _build_analytical_plan_block,
    _build_analytical_plan_user_prompt,
    _parse_analytical_plan_response,
    generate_analytical_plan_from_llm,
)
from config.settings import Settings


class TestParseAnalyticalPlanResponse:
    def test_valid_json_object_parses(self):
        result = _parse_analytical_plan_response(
            '{"metrics": [{"name": "revenue", "table": "Sales", "column": "Amount", '
            '"aggregation": "sum"}]}'
        )
        assert result is not None
        assert result["metrics"][0]["name"] == "revenue"
        assert result["metrics"][0]["aggregation"] == "sum"

    def test_fenced_json_parses(self):
        result = _parse_analytical_plan_response('```json\n{"metrics": [], "limit": 10}\n```')
        assert result is not None
        assert result["limit"] == 10

    def test_defaults_are_filled_in_for_omitted_optional_fields(self):
        result = _parse_analytical_plan_response('{"metrics": []}')
        assert result is not None
        assert result["dimensions"] == []
        assert result["filters"] == []
        assert result["time_range"] is None
        assert result["truth_level"] == "ai_inference"

    def test_malformed_json_returns_none(self):
        assert _parse_analytical_plan_response("not json at all") is None

    def test_json_array_instead_of_object_returns_none(self):
        assert _parse_analytical_plan_response("[]") is None

    def test_unrecognized_aggregation_value_returns_none(self):
        assert (
            _parse_analytical_plan_response(
                '{"metrics": [{"name": "x", "table": "T", "column": "C", '
                '"aggregation": "median"}]}'
            )
            is None
        )

    def test_metric_with_no_table_or_column_still_parses_shape_only(self):
        """Shape-only parsing -- `AnalyticalPlan` doesn't itself enforce
        "a metric needs a table/column or a governed_metric_key"; that's
        `agent.plan_validator.validate_plan`'s own job, not this
        function's."""
        result = _parse_analytical_plan_response('{"metrics": [{"name": "mystery"}]}')
        assert result is not None
        assert result["metrics"][0]["table"] is None


class TestBuildAnalyticalPlanUserPrompt:
    def test_includes_schema_and_question(self):
        prompt = _build_analytical_plan_user_prompt("How much revenue?", "CREATE TABLE Sales (...)")
        assert "CREATE TABLE Sales" in prompt
        assert "Question: How much revenue?" in prompt

    def test_includes_relationship_context_reusing_the_shared_helper(self):
        retrieved_context = [
            {"chunk_type": "relationship", "text": "Sales.CustomerKey -> Customer.CustomerKey"}
        ]
        prompt = _build_analytical_plan_user_prompt(
            "revenue by region", "CREATE TABLE Sales (...)", retrieved_context=retrieved_context
        )
        assert "Sales.CustomerKey -> Customer.CustomerKey" in prompt

    def test_includes_governing_metric_names_only(self):
        prompt = _build_analytical_plan_user_prompt(
            "What's our revenue?",
            "CREATE TABLE Sales (...)",
            governing_metrics=[{"business_name": "Revenue", "approved_expression": "SUM(x)"}],
        )
        assert "Revenue" in prompt
        assert "SUM(x)" not in prompt

    def test_no_optional_context_is_a_pure_no_op_beyond_schema_and_question(self):
        prompt = _build_analytical_plan_user_prompt("x?", "CREATE TABLE T (...)")
        assert prompt == "Schema:\nCREATE TABLE T (...)\n\nQuestion: x?"


class TestBuildAnalyticalPlanBlock:
    def test_renders_validated_framing_and_rendered_steps(self):
        plan = {
            "metrics": [
                {"name": "revenue", "table": "Sales", "column": "Amount", "aggregation": "sum"}
            ],
        }
        block = _build_analytical_plan_block(plan)
        assert "Validated analytical plan" in block
        assert "deterministically checked" in block
        assert "SUM(Sales.Amount)" in block


class TestGenerateAnalyticalPlanFromLlm:
    def _settings(self) -> Settings:
        return Settings(
            ollama_host="http://localhost:11434",
            ollama_model="llama3.1:8b",
            ollama_request_timeout_seconds=60,
            analytical_plan_max_tokens=400,
        )

    def test_valid_response_returns_a_plain_dict(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {
                "content": '{"metrics": [{"name": "revenue", "table": "Sales", '
                '"column": "Amount", "aggregation": "sum"}]}'
            }
        }
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            result = generate_analytical_plan_from_llm(
                "What's total revenue?", "CREATE TABLE Sales (...)", self._settings()
            )
        assert isinstance(result, dict)
        assert result["metrics"][0]["name"] == "revenue"

    def test_unparseable_response_returns_none(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {"message": {"content": "I'm not sure."}}
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            result = generate_analytical_plan_from_llm(
                "some question", "CREATE TABLE T (...)", self._settings()
            )
        assert result is None

    def test_unreachable_ollama_raises_ollama_unavailable_error(self):
        mock_client = MagicMock()
        mock_client.chat.side_effect = ConnectionError("refused")
        with (
            patch("agent.llm_client._get_ollama_client", return_value=mock_client),
            pytest.raises(OllamaUnavailableError),
        ):
            generate_analytical_plan_from_llm(
                "some question", "CREATE TABLE T (...)", self._settings()
            )

    def test_uses_the_configured_max_tokens_setting(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {"message": {"content": '{"metrics": []}'}}
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            generate_analytical_plan_from_llm(
                "some question", "CREATE TABLE T (...)", self._settings()
            )
        _, kwargs = mock_client.chat.call_args
        assert kwargs["options"]["num_predict"] == 400
        assert kwargs["options"]["temperature"] == 0.0
