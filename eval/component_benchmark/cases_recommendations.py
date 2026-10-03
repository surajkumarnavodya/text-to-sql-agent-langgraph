"""Recommendation cases: the evidence-first recommendation engine
(`recommendation.engine.generate_recommendations`).

Each case constructs a realistic `RecommendationInputs` from a real,
already-computed upstream artifact (most feed it a real
`analytics.engine.compute_analytics_result` output, the same composition
the live graph node performs) and checks the literal, named invariants
this engine's own module docstring and CLAUDE.md's "Evidence-first
recommendation engine" section promise: no recommendation without
evidence, and a restricted-column-touching candidate is suppressed for a
viewer lacking `Permission.VIEW_RESTRICTED_COLUMNS` -- re-verified here as
a tracked regression set, not a re-run of `recommendation/engine.py`'s own
much larger pytest unit suite.
"""

from __future__ import annotations

from unittest.mock import patch

from analytics.engine import compute_analytics_result
from recommendation.engine import RecommendationInputs, generate_recommendations
from recommendation.models import RecommendationCategory

from eval.component_benchmark.schema import ComponentCase


def _declining_revenue_result():
    return compute_analytics_result(
        ["year", "revenue"],
        [("2021", 150_000.0), ("2022", 120_000.0), ("2023", 90_000.0)],
    )


def _case_declining_revenue_produces_a_revenue_recommendation_with_evidence() -> tuple[bool, str]:
    inputs = RecommendationInputs(analytics_result=_declining_revenue_result())
    recommendations = generate_recommendations(inputs)
    revenue_recs = [r for r in recommendations if r.category == RecommendationCategory.REVENUE]
    passed = len(revenue_recs) >= 1 and all(len(r.evidence) >= 1 for r in revenue_recs)
    return passed, f"revenue_recs={len(revenue_recs)} total_recs={len(recommendations)}"


def _case_empty_inputs_produce_no_recommendations() -> tuple[bool, str]:
    """No evidence source supplied at all -- must produce an empty tuple,
    never an error and never a recommendation conjured with nothing to
    ground it."""
    recommendations = generate_recommendations(RecommendationInputs())
    passed = recommendations == ()
    return passed, f"recommendations={recommendations!r}"


def _case_restricted_column_hit_suppressed_for_unauthorized_viewer() -> tuple[bool, str]:
    with patch(
        "recommendation.engine.load_sensitive_columns",
        return_value={("customers", "ssn"): "restricted"},
    ):
        inputs = RecommendationInputs(
            restricted_column_hits=(("customers", "ssn"),),
            caller_roles=(),  # no VIEW_RESTRICTED_COLUMNS
        )
        recommendations = generate_recommendations(inputs)
    security_recs = [r for r in recommendations if r.category == RecommendationCategory.SECURITY]
    passed = security_recs == []
    return passed, f"security_recs={len(security_recs)} (expected 0, viewer unauthorized)"


def _case_restricted_column_hit_shown_for_authorized_viewer() -> tuple[bool, str]:
    """The identical setup as the case above, except the viewer's role
    (analyst) does carry VIEW_RESTRICTED_COLUMNS -- confirms the
    suppression above is genuinely permission-specific, not a blanket
    drop of every SECURITY candidate."""
    with patch(
        "recommendation.engine.load_sensitive_columns",
        return_value={("customers", "ssn"): "restricted"},
    ):
        inputs = RecommendationInputs(
            restricted_column_hits=(("customers", "ssn"),),
            caller_roles=("analyst",),
        )
        recommendations = generate_recommendations(inputs)
    security_recs = [r for r in recommendations if r.category == RecommendationCategory.SECURITY]
    passed = len(security_recs) == 1 and len(security_recs[0].evidence) >= 1
    return passed, f"security_recs={len(security_recs)}"


def _case_every_recommendation_from_a_rich_input_carries_evidence() -> tuple[bool, str]:
    """A realistic, multi-signal input (declining revenue + a restricted-
    column hit for an authorized viewer) -- not one recommendation in the
    whole set may have empty evidence, the engine's own central
    guarantee."""
    with patch(
        "recommendation.engine.load_sensitive_columns",
        return_value={("customers", "ssn"): "restricted"},
    ):
        inputs = RecommendationInputs(
            analytics_result=_declining_revenue_result(),
            restricted_column_hits=(("customers", "ssn"),),
            caller_roles=("analyst",),
        )
        recommendations = generate_recommendations(inputs)
    missing_evidence = [r for r in recommendations if not r.evidence]
    passed = len(recommendations) >= 2 and not missing_evidence
    return passed, f"total_recs={len(recommendations)} missing_evidence={len(missing_evidence)}"


CASES: list[ComponentCase] = [
    ComponentCase(
        "declining_revenue_produces_a_revenue_recommendation_with_evidence",
        "A declining-revenue analytics finding produces at least one REVENUE recommendation, always with evidence.",
        _case_declining_revenue_produces_a_revenue_recommendation_with_evidence,
    ),
    ComponentCase(
        "empty_inputs_produce_no_recommendations",
        "No evidence source supplied produces an empty tuple, never an error.",
        _case_empty_inputs_produce_no_recommendations,
    ),
    ComponentCase(
        "restricted_column_hit_suppressed_for_unauthorized_viewer",
        "A SECURITY candidate touching a restricted column is suppressed for a viewer lacking VIEW_RESTRICTED_COLUMNS.",
        _case_restricted_column_hit_suppressed_for_unauthorized_viewer,
    ),
    ComponentCase(
        "restricted_column_hit_shown_for_authorized_viewer",
        "The identical SECURITY candidate is shown for a viewer who does hold VIEW_RESTRICTED_COLUMNS.",
        _case_restricted_column_hit_shown_for_authorized_viewer,
    ),
    ComponentCase(
        "every_recommendation_from_a_rich_input_carries_evidence",
        "No recommendation in a realistic multi-signal run has empty evidence.",
        _case_every_recommendation_from_a_rich_input_carries_evidence,
    ),
]
