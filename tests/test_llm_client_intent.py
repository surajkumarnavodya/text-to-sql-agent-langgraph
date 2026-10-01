"""Unit tests for agent/llm_client.py's analytical-intent-classification
machinery (Prompt 11, `11_ANALYTICAL_INTENT_CONTRACT.md`):
`_parse_intent_response`'s fail-open contract, `_build_intent_user_prompt`/
`_build_intent_governing_metrics_block`'s DATA-framing and
governing-metric-grounding inclusion, `_build_plan_intent_block`'s
rendering, and `generate_analytical_intent_from_llm`'s mocked-Ollama call
shape.

These are pure-function tests (no Ollama call) except where explicitly
mocked -- mirrors `tests/test_llm_client_metric_conformance.py`'s own
posture.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.exceptions import OllamaUnavailableError
from agent.llm_client import (
    _build_intent_governing_metrics_block,
    _build_intent_user_prompt,
    _build_plan_intent_block,
    _parse_intent_response,
    generate_analytical_intent_from_llm,
)
from config.settings import Settings


class TestParseIntentResponse:
    def test_valid_json_object_parses(self):
        result = _parse_intent_response('{"intent": "trend", "confidence": 0.8}')
        assert result is not None
        assert result["intent"] == "trend"
        assert result["confidence"] == 0.8

    def test_fenced_json_parses(self):
        result = _parse_intent_response(
            '```json\n{"intent": "ranking", "confidence": 0.6, ' '"ambiguity_flags": ["x"]}\n```'
        )
        assert result is not None
        assert result["intent"] == "ranking"
        assert result["ambiguity_flags"] == ["x"]

    def test_defaults_are_filled_in_for_omitted_optional_fields(self):
        result = _parse_intent_response('{"intent": "lookup", "confidence": 0.5}')
        assert result is not None
        assert result["metric_candidates"] == []
        assert result["dimensions"] == []
        assert result["time_requirement"] is None
        assert result["truth_level"] == "ai_inference"

    def test_malformed_json_returns_none(self):
        assert _parse_intent_response("not json at all") is None

    def test_json_array_instead_of_object_returns_none(self):
        assert _parse_intent_response('["lookup", 0.5]') is None

    def test_unrecognized_intent_value_returns_none(self):
        assert _parse_intent_response('{"intent": "not_a_real_intent", "confidence": 0.5}') is None

    def test_unrecognized_expected_result_shape_returns_none(self):
        assert (
            _parse_intent_response(
                '{"intent": "trend", "confidence": 0.5, '
                '"expected_result_shape": "not_a_real_shape"}'
            )
            is None
        )

    def test_missing_required_intent_returns_none(self):
        assert _parse_intent_response('{"confidence": 0.5}') is None

    def test_missing_required_confidence_returns_none(self):
        assert _parse_intent_response('{"intent": "trend"}') is None

    def test_confidence_out_of_range_returns_none(self):
        assert _parse_intent_response('{"intent": "trend", "confidence": 1.5}') is None


class TestBuildIntentGoverningMetricsBlock:
    def test_no_governing_metrics_is_a_pure_no_op(self):
        assert _build_intent_governing_metrics_block([]) is None

    def test_renders_metric_names_only_not_the_approved_expression(self):
        block = _build_intent_governing_metrics_block(
            [
                {
                    "business_name": "Average Order Value",
                    "approved_expression": "SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)",
                }
            ]
        )
        assert block is not None
        assert "Average Order Value" in block
        assert "SUM(SalesAmount)" not in block

    def test_frames_the_block_as_data_not_instructions(self):
        block = _build_intent_governing_metrics_block([{"business_name": "AOV"}])
        assert block is not None
        assert "DATA" in block
        assert "never instructions" in block

    def test_renders_every_metric_when_several_are_governing(self):
        block = _build_intent_governing_metrics_block(
            [{"business_name": "Average Order Value"}, {"business_name": "Gross Margin"}]
        )
        assert block is not None
        assert "Average Order Value" in block
        assert "Gross Margin" in block


class TestBuildIntentUserPrompt:
    def test_question_always_included(self):
        prompt = _build_intent_user_prompt("What is our revenue trend?")
        assert "Question: What is our revenue trend?" in prompt

    def test_no_governing_metrics_omits_that_section(self):
        prompt = _build_intent_user_prompt("How many orders?")
        assert "Governed metrics" not in prompt

    def test_governing_metrics_grounding_is_included_before_the_question(self):
        prompt = _build_intent_user_prompt(
            "What's our AOV?", governing_metrics=[{"business_name": "Average Order Value"}]
        )
        assert "Average Order Value" in prompt
        assert prompt.index("Governed metrics") < prompt.index("Question:")


class TestBuildPlanIntentBlock:
    def test_none_classification_is_a_pure_no_op(self):
        assert _build_plan_intent_block(None) is None

    def test_empty_dict_is_a_pure_no_op(self):
        assert _build_plan_intent_block({}) is None

    def test_renders_the_classified_intent(self):
        block = _build_plan_intent_block({"intent": "trend"})
        assert block is not None
        assert "Classified intent: trend" in block

    def test_frames_the_block_as_an_ai_inference_never_confirmed(self):
        block = _build_plan_intent_block({"intent": "trend"})
        assert block is not None
        assert "AI inference" in block
        assert "never confirmed" in block

    def test_renders_time_requirement_and_comparison_when_present(self):
        block = _build_plan_intent_block(
            {
                "intent": "comparison",
                "time_requirement": "last 6 months",
                "comparison": "this quarter vs last quarter",
            }
        )
        assert block is not None
        assert "last 6 months" in block
        assert "this quarter vs last quarter" in block

    def test_renders_ambiguity_flags_when_present(self):
        block = _build_plan_intent_block(
            {"intent": "lookup", "ambiguity_flags": ["'best selling' is ambiguous"]}
        )
        assert block is not None
        assert "'best selling' is ambiguous" in block

    def test_omits_optional_lines_when_absent(self):
        block = _build_plan_intent_block({"intent": "aggregation"})
        assert block is not None
        assert "Time requirement" not in block
        assert "Comparison" not in block
        assert "Candidate dimensions" not in block
        assert "Ambiguity to resolve" not in block


class TestGenerateAnalyticalIntentFromLlm:
    def _settings(self) -> Settings:
        return Settings(
            ollama_host="http://localhost:11434",
            ollama_model="llama3.1:8b",
            ollama_request_timeout_seconds=60,
            intent_classification_max_tokens=250,
        )

    def test_valid_response_returns_a_plain_dict(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {"content": '{"intent": "trend", "confidence": 0.85}'}
        }
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            result = generate_analytical_intent_from_llm("revenue trend?", self._settings())
        assert isinstance(result, dict)
        assert result["intent"] == "trend"
        assert result["confidence"] == 0.85

    def test_unparseable_response_returns_none(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {"message": {"content": "I'm not sure."}}
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            result = generate_analytical_intent_from_llm("some question", self._settings())
        assert result is None

    def test_unreachable_ollama_raises_ollama_unavailable_error(self):
        mock_client = MagicMock()
        mock_client.chat.side_effect = ConnectionError("refused")
        with (
            patch("agent.llm_client._get_ollama_client", return_value=mock_client),
            pytest.raises(OllamaUnavailableError),
        ):
            generate_analytical_intent_from_llm("some question", self._settings())

    def test_uses_the_configured_max_tokens_setting(self):
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {"content": '{"intent": "lookup", "confidence": 0.5}'}
        }
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            generate_analytical_intent_from_llm("some question", self._settings())
        _, kwargs = mock_client.chat.call_args
        assert kwargs["options"]["num_predict"] == 250
        assert kwargs["options"]["temperature"] == 0.0

    def test_governing_metrics_are_rendered_into_the_call_without_being_conflated(self):
        """Grounding context only -- never read back as this call's own
        `metric_candidates` output (the two stay separate truth-level
        concepts, see `agent/intent.py`'s module docstring)."""
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {"content": '{"intent": "aggregation", "confidence": 0.9}'}
        }
        with patch("agent.llm_client._get_ollama_client", return_value=mock_client):
            result = generate_analytical_intent_from_llm(
                "What's our AOV?",
                self._settings(),
                governing_metrics=[{"business_name": "Average Order Value"}],
            )
        _, kwargs = mock_client.chat.call_args
        user_message = kwargs["messages"][1]["content"]
        assert "Average Order Value" in user_message
        assert result is not None
        assert result["metric_candidates"] == []
