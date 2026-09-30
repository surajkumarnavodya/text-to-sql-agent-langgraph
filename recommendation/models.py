"""Typed shape for one recommended action or insight.

No provider in this codebase produces one of these yet -- see this
package's own `__init__.py` docstring.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

from agent.provenance import DataTruthLevel, ProvenancedClaim


class RecommendationKind(str, Enum):
    """What a `Recommendation` is suggesting.

    A closed set, mirroring `analytics.models.AnalyticsFindingKind`'s own
    "deliberately closed, not a free-form string" rationale. Deliberately
    small today -- a future prompt that designs a real recommendation
    engine should extend this set to match whatever it actually produces,
    rather than this module guessing at categories ahead of any real
    implementation.
    """

    NEXT_QUESTION = "next_question"
    ACTION = "action"


class Recommendation(BaseModel):
    """One recommended next question or action, always an `AI_INFERENCE`
    claim (per `agent.provenance.DataTruthLevel`) -- a recommendation is,
    by definition, this platform's own suggestion, never a fact read
    straight from the database.

    Attributes:
        kind: What this recommendation is suggesting -- see
            `RecommendationKind`.
        claim: The recommendation's own text, tagged
            `DataTruthLevel.AI_INFERENCE`. Per rule 10
            (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), a caller must never
            silently render this as if it were confirmed fact -- the UI
            contract for a future consumer of this type is to visually
            distinguish a recommendation from a `DATABASE_FACT`/
            `CONFIRMED_BUSINESS_TRUTH` claim, not this module's job to
            enforce by itself.
        rationale: Optional, short, human-readable explanation of why this
            was recommended (e.g. which `analytics.models.AnalyticsFinding`
            prompted it) -- distinct from `claim.grounded_in`, which names
            specific fact sources rather than explaining the reasoning.
    """

    model_config = ConfigDict(frozen=True)

    kind: RecommendationKind
    claim: ProvenancedClaim
    rationale: str | None = None

    @model_validator(mode="after")
    def _claim_must_be_ai_inference(self) -> Recommendation:
        """A type-level enforcement of rule 10's spirit, not just a prose
        rule: a `Recommendation`'s own claim can never be constructed as
        anything but `AI_INFERENCE` -- there is no such thing as a
        `DATABASE_FACT` or `CONFIRMED_BUSINESS_TRUTH` recommendation, since
        a recommendation is definitionally this platform's own suggestion.
        """
        if self.claim.level != DataTruthLevel.AI_INFERENCE:
            raise ValueError(
                f"Recommendation.claim.level must be AI_INFERENCE, got {self.claim.level!r}."
            )
        return self
