"""Typed shape for one recommended action or insight.

Until Prompt 17 (`17_RECOMMENDATION_ENGINE_CONTRACT.md`), no provider in
this codebase produced one of these except `analytics.forecasting
.recommendations_for_forecast` (a standalone function, not a
`recommendation.provider.RecommendationProvider` implementation -- see
that function's own docstring for why). `recommendation.engine` (new,
Prompt 17) is the first real, general-purpose producer: a deterministic
Data -> Finding -> Evidence -> Rule/Model -> Candidate -> Evidence
Validation -> Confidence -> Recommendation pipeline across nine
categories (performance, anomaly, revenue, customer, product, operations,
data quality, security, database performance).

Every field Prompt 17 adds below is **additive, with a default** --
`analytics.forecasting.recommendations_for_forecast`'s existing six call
sites (`Recommendation(kind=..., claim=..., rationale=...)`) keep
constructing and validating exactly as they did before this prompt,
unmodified and untested-for-regression-free by re-running
`tests/test_analytics_forecasting.py` unchanged. The new, richer fields
are populated only by `recommendation.engine`'s own candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.provenance import DataTruthLevel, ProvenancedClaim

#: The typed-contract schema version for everything `recommendation.engine`
#: produces -- master-contract rule 11's "record ... version metadata"
#: requirement, the identical precedent `analytics.models
#: .ANALYTICS_ENGINE_VERSION`/`FORECASTING_ENGINE_VERSION` already
#: establish. Bump when a rule's formula/confidence calculation or a
#: category's evidence shape changes in a way a consumer should be able
#: to detect.
RECOMMENDATION_ENGINE_VERSION = "1.0.0"


class RecommendationKind(str, Enum):
    """What a `Recommendation` is suggesting.

    A closed set, mirroring `analytics.models.AnalyticsFindingKind`'s own
    "deliberately closed, not a free-form string" rationale.
    """

    NEXT_QUESTION = "next_question"
    ACTION = "action"


class RecommendationCategory(str, Enum):
    """Which modular domain a `recommendation.engine`-produced
    `Recommendation` belongs to -- Prompt 17's own required taxonomy
    (`17_RECOMMENDATION_ENGINE_CONTRACT.md`). A closed set, like every
    other enum in this codebase's typed-contract modules
    (`analytics.models.AnalyticsFindingKind`, `retrieval.models
    .ChunkType`, ...).

    `None` (the field's default on `Recommendation`) means "produced
    before this category taxonomy existed" -- every pre-Prompt-17
    `Recommendation` (i.e. every one `analytics.forecasting
    .recommendations_for_forecast` already produces) stays uncategorized
    rather than being silently reclassified.
    """

    PERFORMANCE = "performance"
    ANOMALY = "anomaly"
    REVENUE = "revenue"
    CUSTOMER = "customer"
    PRODUCT = "product"
    OPERATIONS = "operations"
    DATA_QUALITY = "data_quality"
    SECURITY = "security"
    DATABASE_PERFORMANCE = "database_performance"


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
        category: Which `RecommendationCategory` this belongs to --
            `None` for a pre-Prompt-17 recommendation (see that enum's own
            docstring).
        evidence: The `DATABASE_FACT`/`CONFIRMED_BUSINESS_TRUTH` claim(s)
            this recommendation is traceable to -- Prompt 17's own "no
            recommendation without evidence" requirement. Empty by
            default (a pre-Prompt-17 `Recommendation` never set this);
            `recommendation.engine`'s own pipeline never constructs one of
            these with an empty tuple here (enforced in
            `recommendation.engine._validate_evidence`, a pipeline-level
            invariant, not a type-level one -- see that function's own
            docstring for why it isn't a `model_validator` on this class).
            The one invariant this class *does* enforce at the type level:
            every element, if any are present, must be `DATABASE_FACT` or
            `CONFIRMED_BUSINESS_TRUTH`, never `AI_INFERENCE` -- an
            inference can never ground another inference without
            defeating the entire "evidence-first" premise (master rule 10).
        affected_entity: A short, human-readable pointer to what this
            recommendation is about (e.g. a column name, a table name, a
            pipeline stage name, a dimension-value label) -- `None` when
            there's no single entity to name (e.g. a whole-result
            recommendation).
        action: The concrete, human-readable next step being recommended
            (e.g. "Add an index on OrderDate" / "Review access to this
            restricted column") -- distinct from `claim.value`, which is
            the full recommendation sentence a caller renders; `action` is
            the imperative core of it, useful for a UI that wants to
            render a short call-to-action button/label separately from
            the full explanation.
        measurable_impact: A short, human-readable estimate of impact,
            populated **only** when the evidence actually supports a
            number (e.g. "resolves 3 flagged anomalies" / "affects 12.4%
            of rows") -- `None`, never a fabricated or vague placeholder,
            when no measurable impact is computable from the evidence in
            hand (Prompt 17's own "if supported" qualifier).
        confidence: `0.0`-`1.0`, this candidate's confidence as computed
            by whichever rule produced it -- see each rule's own formula
            in `recommendation.engine`. `None` for a pre-Prompt-17
            recommendation.
        rule_or_model: The fully-qualified name of the rule/model that
            produced this recommendation (e.g.
            `"recommendation.engine.AnomalyRule"`) -- master rule 11's
            "record ... the rule/model" requirement, the literal
            traceability this prompt's acceptance criterion needs.
        limitations: Disclosed caveats about this specific recommendation
            (e.g. "based on a single period's sample, not a full
            history") -- the same "always disclose, never silently omit"
            convention `analytics.models.ForecastResult.limitations`
            already establishes elsewhere in this codebase.
        generated_at: ISO-8601 UTC timestamp of when this recommendation
            was constructed -- master rule 11's "record ... timestamp"
            requirement. Unlike `analytics.models.ForecastModelMetadata`
            (which deliberately omits a timestamp for pure-function
            reproducibility, see that class's own docstring), a
            `Recommendation` is a point-in-time judgment about live data,
            not a reproducible closed-form calculation, so recording when
            it was made is the more useful property here.
        engine_version: `RECOMMENDATION_ENGINE_VERSION` at construction
            time.
    """

    model_config = ConfigDict(frozen=True)

    kind: RecommendationKind
    claim: ProvenancedClaim
    rationale: str | None = None
    category: RecommendationCategory | None = None
    evidence: tuple[ProvenancedClaim, ...] = ()
    affected_entity: str | None = None
    action: str | None = None
    measurable_impact: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rule_or_model: str | None = None
    limitations: tuple[str, ...] = ()
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    engine_version: str = RECOMMENDATION_ENGINE_VERSION

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

    @model_validator(mode="after")
    def _evidence_must_not_be_ai_inference(self) -> Recommendation:
        """`evidence` exists to *ground* a recommendation in something more
        solid than the LLM's/rule's own say-so -- an `AI_INFERENCE` claim
        (e.g. another recommendation, a narrative insight sentence) cannot
        serve that purpose without defeating the whole "evidence-first"
        premise (master rule 10: never let an inference be laundered into
        looking like confirmed grounding). `DATABASE_FACT` and
        `CONFIRMED_BUSINESS_TRUTH` are both acceptable -- a hand-reviewed
        classification (`config.sensitive_columns`) is exactly as valid a
        piece of evidence as a computed statistic, just a different kind
        of ground truth (see `agent.provenance.DataTruthLevel`'s own
        docstring for why both are named as pre-existing precedents).
        """
        for item in self.evidence:
            if item.level == DataTruthLevel.AI_INFERENCE:
                raise ValueError(
                    "Recommendation.evidence items must be DATABASE_FACT or "
                    "CONFIRMED_BUSINESS_TRUTH, never AI_INFERENCE -- an inference "
                    "cannot ground another inference."
                )
        return self
