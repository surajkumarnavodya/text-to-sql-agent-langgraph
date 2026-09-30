"""Contract tests for recommendation/ -- no default provider exists yet
(see recommendation/__init__.py's own docstring), so this only tests the
typed models and the Protocol shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from recommendation.models import Recommendation, RecommendationKind
from recommendation.provider import RecommendationProvider

from agent.insight import ResultSummary
from agent.provenance import DataTruthLevel, ProvenancedClaim


class TestRecommendation:
    def test_ai_inference_claim_is_valid(self):
        rec = Recommendation(
            kind=RecommendationKind.NEXT_QUESTION,
            claim=ProvenancedClaim(
                value="Would you like to see this broken down by territory?",
                level=DataTruthLevel.AI_INFERENCE,
            ),
        )
        assert rec.claim.level == DataTruthLevel.AI_INFERENCE

    @pytest.mark.parametrize(
        "level", [DataTruthLevel.DATABASE_FACT, DataTruthLevel.CONFIRMED_BUSINESS_TRUTH]
    )
    def test_non_ai_inference_claim_is_rejected(self, level):
        with pytest.raises(ValidationError):
            Recommendation(
                kind=RecommendationKind.ACTION,
                claim=ProvenancedClaim(value="Do X.", level=level),
            )

    def test_rationale_is_optional(self):
        rec = Recommendation(
            kind=RecommendationKind.ACTION,
            claim=ProvenancedClaim(value="Do X.", level=DataTruthLevel.AI_INFERENCE),
        )
        assert rec.rationale is None


class TestRecommendationProviderProtocol:
    def test_a_conforming_implementation_satisfies_the_protocol(self):
        class _FakeProvider:
            def recommend(self, summary: ResultSummary) -> tuple[Recommendation, ...]:
                return ()

        assert isinstance(_FakeProvider(), RecommendationProvider)

    def test_a_non_conforming_object_does_not_satisfy_the_protocol(self):
        class _NotAProvider:
            pass

        assert not isinstance(_NotAProvider(), RecommendationProvider)
