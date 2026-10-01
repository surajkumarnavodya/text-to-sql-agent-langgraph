"""Contract tests for recommendation/models.py and recommendation/provider.py.

`recommendation/engine.py` (Prompt 17, `17_RECOMMENDATION_ENGINE_CONTRACT.md`)
has its own dedicated `tests/test_recommendation_engine.py`; this file
stays focused on the typed `Recommendation` model itself (including its
Prompt-17 additions) and the still-unimplemented `RecommendationProvider`
Protocol shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
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

    def test_pre_prompt_17_construction_still_works_unmodified(self):
        """The exact call shape `analytics.forecasting
        .recommendations_for_forecast`'s six pre-existing call sites use --
        every Prompt 17 field must default so this keeps validating."""
        rec = Recommendation(
            kind=RecommendationKind.NEXT_QUESTION,
            claim=ProvenancedClaim(value="Ask about X.", level=DataTruthLevel.AI_INFERENCE),
            rationale="because Y.",
        )
        assert rec.category is None
        assert rec.evidence == ()
        assert rec.affected_entity is None
        assert rec.action is None
        assert rec.measurable_impact is None
        assert rec.confidence is None
        assert rec.rule_or_model is None
        assert rec.limitations == ()
        assert rec.generated_at
        assert rec.engine_version


class TestRecommendationEvidence:
    def test_database_fact_evidence_is_valid(self):
        rec = Recommendation(
            kind=RecommendationKind.ACTION,
            claim=ProvenancedClaim(value="Do X.", level=DataTruthLevel.AI_INFERENCE),
            category=RecommendationCategory.DATA_QUALITY,
            evidence=(
                ProvenancedClaim(value="42% null.", level=DataTruthLevel.DATABASE_FACT, source="x"),
            ),
            confidence=0.8,
            rule_or_model="test.Rule",
        )
        assert rec.evidence[0].level == DataTruthLevel.DATABASE_FACT

    def test_confirmed_business_truth_evidence_is_valid(self):
        rec = Recommendation(
            kind=RecommendationKind.ACTION,
            claim=ProvenancedClaim(value="Do X.", level=DataTruthLevel.AI_INFERENCE),
            category=RecommendationCategory.SECURITY,
            evidence=(
                ProvenancedClaim(
                    value="Classified restricted.",
                    level=DataTruthLevel.CONFIRMED_BUSINESS_TRUTH,
                    source="config.sensitive_columns",
                ),
            ),
        )
        assert rec.evidence[0].level == DataTruthLevel.CONFIRMED_BUSINESS_TRUTH

    def test_ai_inference_evidence_is_rejected(self):
        with pytest.raises(ValidationError):
            Recommendation(
                kind=RecommendationKind.ACTION,
                claim=ProvenancedClaim(value="Do X.", level=DataTruthLevel.AI_INFERENCE),
                evidence=(
                    ProvenancedClaim(value="Another inference.", level=DataTruthLevel.AI_INFERENCE),
                ),
            )

    def test_confidence_out_of_range_is_rejected(self):
        with pytest.raises(ValidationError):
            Recommendation(
                kind=RecommendationKind.ACTION,
                claim=ProvenancedClaim(value="Do X.", level=DataTruthLevel.AI_INFERENCE),
                confidence=1.5,
            )


class TestRecommendationCategory:
    def test_every_required_category_exists(self):
        expected = {
            "performance",
            "anomaly",
            "revenue",
            "customer",
            "product",
            "operations",
            "data_quality",
            "security",
            "database_performance",
        }
        assert {c.value for c in RecommendationCategory} == expected


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
