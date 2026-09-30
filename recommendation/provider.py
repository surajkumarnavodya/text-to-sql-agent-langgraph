"""The `RecommendationProvider` interface -- shape only, no implementation.

Unlike `analytics.provider.AnalyticsProvider`, this Protocol has no
default implementation in this increment: no existing code anywhere in
this repository computes a recommendation, so there is nothing to adapt
without inventing new logic, which a target-architecture prompt should
not do. A future prompt that designs the actual recommendation engine
implements this Protocol; nothing needs to change here when it does.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.insight import ResultSummary
from recommendation.models import Recommendation


@runtime_checkable
class RecommendationProvider(Protocol):
    """A source of recommendations for one query result.

    Takes the same `ResultSummary` shape `analytics.provider
    .AnalyticsProvider.analyze` does, deliberately -- a real
    recommendation engine will likely want to consider a result's
    `analytics.models.AnalyticsResult` findings too, but the exact
    signature (does it take an `AnalyticsResult` as well? the original
    question text? retrieved business context?) is a design decision for
    whichever future prompt builds the first real implementation, not one
    this target-architecture prompt should make on its behalf. This
    Protocol is intentionally the minimal shape that's still real enough
    to type-check a future implementation against.
    """

    def recommend(self, summary: ResultSummary) -> tuple[Recommendation, ...]:
        """Returns every recommendation derivable from `summary`, if any."""
        ...
