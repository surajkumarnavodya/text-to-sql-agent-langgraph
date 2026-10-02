"""The governed recommendation lifecycle's typed vocabulary -- Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).

**Deliberately ORM-independent and pure**, mirroring `semantic.catalog`'s
own "no identity/SQLAlchemy import" split exactly, for the identical
reason: this vocabulary (and the transition table below) needs to be
unit-testable with plain enum values, with no database in the loop, and
reusable by both the repository layer (`identity/repositories
/recommendation_governance.py`, defense in depth) and the API layer
(`api/recommendation_governance.py`, the user-facing 409 before a
repository call is even attempted) -- the exact same two-layer
enforcement `semantic.catalog.VALID_STATUS_TRANSITIONS` already
establishes for the semantic catalog.

**This is a feedback/quality lifecycle, not a content-review workflow**
-- unlike `semantic.catalog.CatalogStatus` (draft -> reviewed ->
published -> superseded, which gates whether content is *live*), nothing
here ever gates whether a `recommendation.models.Recommendation` is
shown to anyone: Prompt 17's engine already decided that (evidence
validation, confidence floor, authorization) before this prompt's own
concerns begin. This lifecycle exists purely to **measure and record**
how good a recommendation turned out to be, after the fact -- the literal
"recommendation quality can be measured and improved through controlled
feedback" acceptance criterion.
"""

from __future__ import annotations

from enum import Enum


class RecommendationStatus(str, Enum):
    """One persisted recommendation's own lifecycle state -- the exact
    eight values Prompt 18 specifies.

    Attributes:
        GENERATED: The initial state every persisted
            `identity.models.RecommendationRecord` starts in -- set the
            moment `recommendation.engine.generate_recommendations`'s
            output is written to governance storage (see
            `identity/repositories/recommendation_governance.py
            ::create_record`). No human has looked at it yet.
        REVIEWED: A reviewer has looked at it but hasn't yet rendered a
            quality verdict -- an optional intermediate state (a caller
            may go straight from `GENERATED` to a verdict), mirroring
            `semantic.catalog.CatalogStatus.REVIEWED`'s own "exists as
            its own state so it can be scheduled/batched separately"
            rationale, applied here to triage rather than publish-gating.
        ACCEPTED: A reviewer/user judged the recommendation accurate and
            useful.
        REJECTED: A reviewer/user judged the recommendation not worth
            acting on (a legitimate, non-error disagreement with the
            suggestion) -- terminal.
        PARTIALLY_USEFUL: Judged correct in part, or useful but
            incomplete/imprecise -- distinct from `ACCEPTED` (fully
            useful) and from `INCORRECT` (actually wrong), so aggregate
            quality metrics can distinguish "good," "mixed," and "wrong"
            rather than collapsing them into one binary.
        INCORRECT: A reviewer/user judged the recommendation's own
            finding/evidence/claim to be factually wrong -- distinct from
            `REJECTED` ("I don't want to act on this, even though it may
            be true") -- terminal.
        RESOLVED: The underlying issue the recommendation flagged has
            since been addressed in the real world (e.g. the anomalous
            period was investigated and explained, the flagged query was
            optimized, the restricted-column access was confirmed
            authorized) -- only reachable from `ACCEPTED`/
            `PARTIALLY_USEFUL` (a recommendation can't be "resolved" if
            it was never judged worth acting on in the first place) --
            terminal.
        EXPIRED: No longer relevant -- stale, superseded by a fresher
            engine run over the same evidence, or administratively
            retired. Reachable from any non-terminal state (see
            `VALID_STATUS_TRANSITIONS`) -- terminal.
    """

    GENERATED = "generated"
    REVIEWED = "reviewed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIALLY_USEFUL = "partially_useful"
    INCORRECT = "incorrect"
    RESOLVED = "resolved"
    EXPIRED = "expired"


#: Transitions `identity/repositories/recommendation_governance.py` enforces
#: -- the single source of truth for "which status can move to which,"
#: checked by both the repository layer (defense in depth) and
#: `api/recommendation_governance.py` (the user-facing 409 before a
#: repository call is even attempted). Mirrors `semantic.catalog
#: .VALID_STATUS_TRANSITIONS`'s own dict shape exactly.
VALID_STATUS_TRANSITIONS: dict[RecommendationStatus, frozenset[RecommendationStatus]] = {
    RecommendationStatus.GENERATED: frozenset(
        {
            RecommendationStatus.REVIEWED,
            RecommendationStatus.ACCEPTED,
            RecommendationStatus.REJECTED,
            RecommendationStatus.PARTIALLY_USEFUL,
            RecommendationStatus.INCORRECT,
            RecommendationStatus.EXPIRED,
        }
    ),
    RecommendationStatus.REVIEWED: frozenset(
        {
            RecommendationStatus.ACCEPTED,
            RecommendationStatus.REJECTED,
            RecommendationStatus.PARTIALLY_USEFUL,
            RecommendationStatus.INCORRECT,
            RecommendationStatus.EXPIRED,
        }
    ),
    RecommendationStatus.ACCEPTED: frozenset(
        {RecommendationStatus.RESOLVED, RecommendationStatus.EXPIRED}
    ),
    RecommendationStatus.PARTIALLY_USEFUL: frozenset(
        {RecommendationStatus.RESOLVED, RecommendationStatus.EXPIRED}
    ),
    RecommendationStatus.REJECTED: frozenset(),
    RecommendationStatus.INCORRECT: frozenset(),
    RecommendationStatus.RESOLVED: frozenset(),
    RecommendationStatus.EXPIRED: frozenset(),
}

#: The verdicts `POST /recommendations/{id}/feedback` accepts as its own
#: target status -- `RESOLVED` and `EXPIRED` each have their own, more
#: narrowly-permissioned/validated route (`.../resolve`, `.../expire`)
#: instead, so a caller can't reach either one through the general
#: feedback endpoint (see `18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).
FEEDBACK_VERDICT_STATUSES: frozenset[RecommendationStatus] = frozenset(
    {
        RecommendationStatus.REVIEWED,
        RecommendationStatus.ACCEPTED,
        RecommendationStatus.REJECTED,
        RecommendationStatus.PARTIALLY_USEFUL,
        RecommendationStatus.INCORRECT,
    }
)

#: Every status with no outgoing transition at all -- a record in one of
#: these states is permanently done, for whichever reason its own value
#: names.
TERMINAL_STATUSES: frozenset[RecommendationStatus] = frozenset(
    status for status, targets in VALID_STATUS_TRANSITIONS.items() if not targets
)


def is_terminal_status(status: RecommendationStatus) -> bool:
    """True if `status` has no legal outgoing transition at all."""
    return status in TERMINAL_STATUSES
