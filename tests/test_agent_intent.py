"""Unit tests for agent/intent.py (Prompt 11,
`11_ANALYTICAL_INTENT_CONTRACT.md`) -- the analytical-intent typed
contract. Fully offline: plain pydantic model construction/validation,
no DB, no HTTP, no LLM call (that's `tests/test_llm_client_intent.py`).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.intent import (
    _INTENT_TYPES_IMPLYING_PLANNING,
    AnalyticalIntentClassification,
    AnalyticalIntentType,
    ExpectedResultShape,
    intent_implies_planning,
)
from agent.provenance import DataTruthLevel

ALL_INTENT_TYPES = list(AnalyticalIntentType)


class TestAnalyticalIntentTypeGoldenParse:
    """One golden-parse case per all 13 intent types -- confirms every
    enum value round-trips through the model."""

    @pytest.mark.parametrize("intent_type", ALL_INTENT_TYPES)
    def test_every_intent_type_round_trips(self, intent_type):
        classification = AnalyticalIntentClassification(intent=intent_type, confidence=0.75)
        assert classification.intent == intent_type
        assert classification.intent.value == intent_type.value

    def test_thirteen_intent_types_exist(self):
        """A closed set, per this prompt's own 13 named categories --
        catches an accidental addition/removal."""
        assert len(ALL_INTENT_TYPES) == 13

    def test_unrecognized_intent_value_is_rejected(self):
        """An unrecognized value makes the whole response unparseable --
        never silently coerced to a nearby category (see the enum's own
        docstring)."""
        with pytest.raises(ValidationError):
            AnalyticalIntentClassification(
                intent="not_a_real_intent",  # type: ignore[arg-type]
                confidence=0.5,
            )


class TestExpectedResultShape:
    def test_six_shapes_exist(self):
        assert len(list(ExpectedResultShape)) == 6

    def test_unrecognized_shape_value_is_rejected(self):
        with pytest.raises(ValidationError):
            AnalyticalIntentClassification(
                intent=AnalyticalIntentType.TREND,
                confidence=0.5,
                expected_result_shape="not_a_real_shape",  # type: ignore[arg-type]
            )


class TestRequiredFieldEnforcement:
    def test_missing_intent_raises(self):
        with pytest.raises(ValidationError):
            AnalyticalIntentClassification(confidence=0.5)  # type: ignore[call-arg]

    def test_missing_confidence_raises(self):
        """Deliberately no default for `confidence` -- a response that
        omits it is malformed, not silently assigned a fabricated
        mid-confidence value (master rule 9/10's "never silently..."
        spirit)."""
        with pytest.raises(ValidationError):
            AnalyticalIntentClassification(intent=AnalyticalIntentType.LOOKUP)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad_confidence", [-0.1, 1.1, 2.0, -5.0])
    def test_confidence_out_of_range_raises(self, bad_confidence):
        with pytest.raises(ValidationError):
            AnalyticalIntentClassification(
                intent=AnalyticalIntentType.LOOKUP, confidence=bad_confidence
            )

    @pytest.mark.parametrize("ok_confidence", [0.0, 0.5, 1.0])
    def test_confidence_boundary_values_are_accepted(self, ok_confidence):
        classification = AnalyticalIntentClassification(
            intent=AnalyticalIntentType.LOOKUP, confidence=ok_confidence
        )
        assert classification.confidence == ok_confidence


class TestOptionalFieldDefaults:
    def test_every_optional_field_defaults_to_empty_or_none(self):
        classification = AnalyticalIntentClassification(
            intent=AnalyticalIntentType.AGGREGATION, confidence=0.9
        )
        assert classification.metric_candidates == ()
        assert classification.dimensions == ()
        assert classification.time_requirement is None
        assert classification.comparison is None
        assert classification.filters == ()
        assert classification.expected_result_shape is None
        assert classification.ambiguity_flags == ()

    def test_truth_level_always_defaults_to_ai_inference(self):
        """Always AI_INFERENCE -- never promoted, never confirmed (see
        the model's own docstring and agent.provenance.DataTruthLevel)."""
        classification = AnalyticalIntentClassification(
            intent=AnalyticalIntentType.FORECAST, confidence=0.6
        )
        assert classification.truth_level == DataTruthLevel.AI_INFERENCE

    def test_model_is_frozen(self):
        classification = AnalyticalIntentClassification(
            intent=AnalyticalIntentType.LOOKUP, confidence=0.5
        )
        with pytest.raises(ValidationError):
            classification.confidence = 0.9


class TestIntentTypesImplyingPlanning:
    """The exact membership `plan_query_node`'s widened gate reads
    directly (never re-derives its own copy)."""

    def test_lookup_is_excluded(self):
        assert AnalyticalIntentType.LOOKUP not in _INTENT_TYPES_IMPLYING_PLANNING

    def test_aggregation_is_excluded(self):
        assert AnalyticalIntentType.AGGREGATION not in _INTENT_TYPES_IMPLYING_PLANNING

    @pytest.mark.parametrize(
        "intent_type",
        [
            t
            for t in ALL_INTENT_TYPES
            if t not in (AnalyticalIntentType.LOOKUP, AnalyticalIntentType.AGGREGATION)
        ],
    )
    def test_every_other_intent_type_is_included(self, intent_type):
        assert intent_type in _INTENT_TYPES_IMPLYING_PLANNING

    def test_exactly_eleven_of_thirteen_imply_planning(self):
        assert len(_INTENT_TYPES_IMPLYING_PLANNING) == 11


class TestIntentImpliesPlanning:
    """`intent_implies_planning` -- takes a plain dict (`AgentState`'s
    established "plain dicts, not model instances" convention), not the
    pydantic model itself."""

    def _dict_for(self, intent: AnalyticalIntentType, ambiguity_flags: tuple = ()) -> dict:
        return AnalyticalIntentClassification(
            intent=intent, confidence=0.8, ambiguity_flags=ambiguity_flags
        ).model_dump(mode="json")

    def test_lookup_with_no_ambiguity_does_not_imply_planning(self):
        assert intent_implies_planning(self._dict_for(AnalyticalIntentType.LOOKUP)) is False

    def test_aggregation_with_no_ambiguity_does_not_imply_planning(self):
        assert intent_implies_planning(self._dict_for(AnalyticalIntentType.AGGREGATION)) is False

    def test_trend_implies_planning(self):
        assert intent_implies_planning(self._dict_for(AnalyticalIntentType.TREND)) is True

    def test_comparison_implies_planning(self):
        assert intent_implies_planning(self._dict_for(AnalyticalIntentType.COMPARISON)) is True

    def test_lookup_with_ambiguity_flags_implies_planning_anyway(self):
        """Non-empty `ambiguity_flags` alone widens the gate, regardless
        of which intent was guessed."""
        classification = self._dict_for(
            AnalyticalIntentType.LOOKUP, ambiguity_flags=("unclear phrasing",)
        )
        assert intent_implies_planning(classification) is True

    def test_works_with_a_plain_string_intent_value_too(self):
        """Correct regardless of whether the caller's dict happens to
        still hold the enum member or a plain string (e.g. after a JSON
        round-trip) -- `classification["intent"]` is re-parsed via
        `AnalyticalIntentType(...)` explicitly."""
        assert intent_implies_planning({"intent": "trend", "ambiguity_flags": []}) is True
        assert intent_implies_planning({"intent": "lookup", "ambiguity_flags": []}) is False
