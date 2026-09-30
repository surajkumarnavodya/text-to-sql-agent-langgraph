"""Unit tests for agent/provenance.py's typed DATABASE_FACT/AI_INFERENCE/
CONFIRMED_BUSINESS_TRUTH vocabulary. Pure contract tests -- no LLM/DB.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.provenance import DataTruthLevel, ProvenancedClaim


class TestDataTruthLevel:
    def test_exactly_three_values(self):
        assert {level.value for level in DataTruthLevel} == {
            "database_fact",
            "ai_inference",
            "confirmed_business_truth",
        }


class TestProvenancedClaim:
    def test_database_fact_claim_needs_no_grounding(self):
        claim = ProvenancedClaim(value="1,234 rows.", level=DataTruthLevel.DATABASE_FACT)
        assert claim.grounded_in == ()

    def test_ai_inference_claim_may_carry_grounding(self):
        claim = ProvenancedClaim(
            value="Sales rose 12% year over year.",
            level=DataTruthLevel.AI_INFERENCE,
            grounded_in=("row_count=1234", "top_share_percent=60.4"),
        )
        assert claim.grounded_in == ("row_count=1234", "top_share_percent=60.4")

    def test_ai_inference_claim_may_omit_grounding(self):
        # A real, valid state -- e.g. a recommendation with no specific
        # numeric grounding (see agent/provenance.py's own docstring).
        claim = ProvenancedClaim(
            value="Consider reviewing this category.", level=DataTruthLevel.AI_INFERENCE
        )
        assert claim.grounded_in == ()

    def test_confirmed_business_truth_rejects_grounded_in(self):
        with pytest.raises(ValidationError):
            ProvenancedClaim(
                value="This is the approved gross margin formula.",
                level=DataTruthLevel.CONFIRMED_BUSINESS_TRUTH,
                grounded_in=("some fact",),
            )

    def test_confirmed_business_truth_without_grounded_in_is_valid(self):
        claim = ProvenancedClaim(
            value="This is the approved gross margin formula.",
            level=DataTruthLevel.CONFIRMED_BUSINESS_TRUTH,
        )
        assert claim.grounded_in == ()

    def test_frozen(self):
        claim = ProvenancedClaim(value="x", level=DataTruthLevel.DATABASE_FACT)
        with pytest.raises(ValidationError):
            claim.value = "y"
