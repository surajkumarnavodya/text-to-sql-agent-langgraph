"""Structured analytical-intent classification -- Prompt 11
(`11_ANALYTICAL_INTENT_CONTRACT.md`): what *kind* of analytical question
is this, before any SQL is generated.

`02_TARGET_ARCHITECTURE.md` §2 named this exact gap back in Prompt 02:
`agent.followup.classify_followup` only classifies a question's
*conversational* shape (standalone/followup/ambiguous), and
`agent.complexity`'s regex trip-wires only answer "is this complex
enough to widen the retry budget" -- neither is a general classifier of
what analytical operation the question actually asks for. This module
is that classifier's typed contract only (no LLM call lives here --
`agent.llm_client.generate_analytical_intent_from_llm` makes the call,
`agent.nodes.classify_analytical_intent_node` wires it into the graph),
mirroring `agent/followup.py`/`agent/provenance.py`/`semantic/catalog.py`'s
identical "typed contract module, separate from the LLM-calling module"
split.

**Deliberately additive to, never a replacement for, `agent.complexity`.**
That module's 4 regex signals still independently drive the retry-budget
bonus and still independently gate `plan_query_node`'s own LLM call --
this module's classification only ever *widens* that gate (via
`_INTENT_TYPES_IMPLYING_PLANNING` below), never narrows or replaces it.
See `agent.nodes.plan_query_node`'s own docstring for the exact gate.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from agent.provenance import DataTruthLevel


class AnalyticalIntentType(str, Enum):
    """What kind of analytical operation a question is asking for --
    exactly the 13 categories this prompt specifies. A closed set, like
    `retrieval.models.ChunkType`/`semantic.catalog.CatalogConceptType` --
    an unrecognized value from the LLM makes the whole response
    unparseable (see `agent.llm_client._parse_intent_response`), never
    silently coerced to a nearby category.
    """

    LOOKUP = "lookup"
    AGGREGATION = "aggregation"
    TREND = "trend"
    COMPARISON = "comparison"
    RANKING = "ranking"
    DISTRIBUTION = "distribution"
    SEGMENTATION = "segmentation"
    FUNNEL = "funnel"
    COHORT = "cohort"
    FORECAST = "forecast"
    ANOMALY = "anomaly"
    ROOT_CAUSE = "root_cause"
    RECOMMENDATION = "recommendation"


class ExpectedResultShape(str, Enum):
    """The shape of result a question's answer should take -- an
    independent judgment from `AnalyticalIntentType` (not programmatically
    derived from it): e.g. a FUNNEL question can reasonably expect either
    a `TABLE` or a `DISTRIBUTION` depending on phrasing, so the model
    picks both fields separately.
    """

    SINGLE_VALUE = "single_value"
    TIME_SERIES = "time_series"
    RANKED_LIST = "ranked_list"
    TABLE = "table"
    DISTRIBUTION = "distribution"
    COMPARISON_TABLE = "comparison_table"


#: Every intent except the two simplest, most direct categories --
#: `plan_query_node`'s own gate reads this directly (never re-derives
#: its own copy) to decide whether a classified intent alone (with zero
#: `agent.complexity` signals) should still trigger an up-front plan.
#: `LOOKUP`/`AGGREGATION` are excluded deliberately: a single filtered
#: row or a single aggregate rarely benefits from a multi-step plan the
#: way a trend/comparison/ranking/segmentation/... question does.
_INTENT_TYPES_IMPLYING_PLANNING: frozenset[AnalyticalIntentType] = frozenset(
    AnalyticalIntentType
) - frozenset({AnalyticalIntentType.LOOKUP, AnalyticalIntentType.AGGREGATION})


class AnalyticalIntentClassification(BaseModel):
    """One question's structured analytical-intent classification --
    always `AI_INFERENCE` (see `truth_level`), never promoted, and never
    itself a security or authorization control (see `agent.nodes
    .classify_analytical_intent_node`'s own docstring for why this is
    true by construction, not by convention).

    `intent`/`confidence` are the only two **required** fields --
    deliberately no default for `confidence`: a response that omits it
    is treated as malformed (see `agent.llm_client._parse_intent_response`)
    rather than silently assigned a fabricated mid-confidence value, per
    master-contract rules 9-10's "never silently..." spirit. Every other
    field defaults to empty/`None` ("not mentioned in the question"),
    since not every question has a time requirement, a comparison, or an
    ambiguity.

    Attributes:
        intent: The classified `AnalyticalIntentType`.
        confidence: The model's own confidence in `intent`, `0.0`-`1.0`.
        metric_candidates: Raw metric-shaped phrases pulled from the
            question's own text (e.g. `"revenue"`, `"churn rate"`) --
            **not** the same thing as `AgentState["governing_metrics"]`
            (a `CONFIRMED_BUSINESS_TRUTH` set already resolved against
            the published semantic catalog). A candidate here may or may
            not turn out to match a governed metric; the two are never
            conflated into one field.
        dimensions: Candidate breakdown dimensions mentioned or implied
            (e.g. `"region"`, `"product category"`).
        time_requirement: Free-text description of a time constraint the
            question implies (e.g. `"last 6 months"`, `"year-over-year"`),
            or `None` if the question has none.
        comparison: Free-text description of what's being compared, for
            a `COMPARISON`-shaped question (e.g. `"this quarter vs last
            quarter"`), or `None` otherwise.
        filters: Candidate filter conditions mentioned (e.g. `"region =
            West"`).
        expected_result_shape: See `ExpectedResultShape`; `None` if the
            model can't confidently judge one.
        ambiguity_flags: Reasons this question's *analytical*
            interpretation is unclear (e.g. `"'best selling' could mean
            highest revenue or highest unit count"`) -- deliberately a
            different axis from `agent.followup`'s own conversational
            ambiguity (a dangling reference with no prior turn to resolve
            against); this field never short-circuits the graph the way
            `classify_followup_node`'s own "ambiguous" classification
            does -- it's advisory metadata only, fed into the planning
            prompt and the widened-gate check above.
        truth_level: Always `AI_INFERENCE` -- see `agent.provenance
            .DataTruthLevel`'s own docstring.
    """

    model_config = ConfigDict(frozen=True)

    intent: AnalyticalIntentType
    confidence: float = Field(ge=0.0, le=1.0)
    metric_candidates: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    time_requirement: str | None = None
    comparison: str | None = None
    filters: tuple[str, ...] = ()
    expected_result_shape: ExpectedResultShape | None = None
    ambiguity_flags: tuple[str, ...] = ()
    truth_level: DataTruthLevel = DataTruthLevel.AI_INFERENCE


def intent_implies_planning(classification: dict) -> bool:
    """Whether this classification alone (independent of `agent
    .complexity`'s own regex signals) should widen `plan_query_node`'s
    gate to call the planner -- true when the classified `intent` is in
    `_INTENT_TYPES_IMPLYING_PLANNING`, or when `ambiguity_flags` is
    non-empty (an ambiguous question benefits from an explicit plan to
    work through the ambiguity, regardless of which intent was guessed).

    Args:
        classification: The plain dict `AgentState["analytical_intent"]`
            holds (`AnalyticalIntentClassification.model_dump()`'s
            output) -- never the pydantic model itself, matching
            `AgentState`'s own established "plain dicts, not model
            instances" convention for `retrieved_context`/`query_plan`-
            shaped fields. `classification["intent"]` is re-parsed via
            `AnalyticalIntentType(...)` explicitly rather than relying on
            dict-membership-via-hash-equality against a plain string,
            so this is correct regardless of whether the caller's dict
            happens to still hold the enum member or a plain string
            (e.g. after a JSON round-trip).
    """
    intent = AnalyticalIntentType(classification["intent"])
    return intent in _INTENT_TYPES_IMPLYING_PLANNING or bool(classification.get("ambiguity_flags"))
