"""Unit tests for recommendation/governance.py -- Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`)'s typed lifecycle vocabulary.
"""

from __future__ import annotations

import pytest
from recommendation.governance import (
    FEEDBACK_VERDICT_STATUSES,
    TERMINAL_STATUSES,
    VALID_STATUS_TRANSITIONS,
    RecommendationStatus,
    is_terminal_status,
)


class TestEightStatusesExist:
    def test_every_required_status_exists(self):
        expected = {
            "generated",
            "reviewed",
            "accepted",
            "rejected",
            "partially_useful",
            "incorrect",
            "resolved",
            "expired",
        }
        assert {s.value for s in RecommendationStatus} == expected


class TestValidTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            (RecommendationStatus.GENERATED, RecommendationStatus.REVIEWED),
            (RecommendationStatus.GENERATED, RecommendationStatus.ACCEPTED),
            (RecommendationStatus.GENERATED, RecommendationStatus.REJECTED),
            (RecommendationStatus.GENERATED, RecommendationStatus.PARTIALLY_USEFUL),
            (RecommendationStatus.GENERATED, RecommendationStatus.INCORRECT),
            (RecommendationStatus.GENERATED, RecommendationStatus.EXPIRED),
            (RecommendationStatus.REVIEWED, RecommendationStatus.ACCEPTED),
            (RecommendationStatus.REVIEWED, RecommendationStatus.REJECTED),
            (RecommendationStatus.REVIEWED, RecommendationStatus.PARTIALLY_USEFUL),
            (RecommendationStatus.REVIEWED, RecommendationStatus.INCORRECT),
            (RecommendationStatus.REVIEWED, RecommendationStatus.EXPIRED),
            (RecommendationStatus.ACCEPTED, RecommendationStatus.RESOLVED),
            (RecommendationStatus.ACCEPTED, RecommendationStatus.EXPIRED),
            (RecommendationStatus.PARTIALLY_USEFUL, RecommendationStatus.RESOLVED),
            (RecommendationStatus.PARTIALLY_USEFUL, RecommendationStatus.EXPIRED),
        ],
    )
    def test_allowed_transition(self, current, target):
        assert target in VALID_STATUS_TRANSITIONS[current]

    @pytest.mark.parametrize(
        "current,target",
        [
            (RecommendationStatus.REJECTED, RecommendationStatus.ACCEPTED),
            (RecommendationStatus.INCORRECT, RecommendationStatus.ACCEPTED),
            (RecommendationStatus.RESOLVED, RecommendationStatus.REJECTED),
            (RecommendationStatus.EXPIRED, RecommendationStatus.GENERATED),
            (RecommendationStatus.ACCEPTED, RecommendationStatus.GENERATED),
            (RecommendationStatus.GENERATED, RecommendationStatus.RESOLVED),
            (RecommendationStatus.REVIEWED, RecommendationStatus.GENERATED),
        ],
    )
    def test_disallowed_transition(self, current, target):
        assert target not in VALID_STATUS_TRANSITIONS[current]

    def test_every_status_has_an_entry(self):
        assert set(VALID_STATUS_TRANSITIONS.keys()) == set(RecommendationStatus)


class TestTerminalStatuses:
    def test_terminal_statuses_have_no_outgoing_transitions(self):
        expected = {
            RecommendationStatus.REJECTED,
            RecommendationStatus.INCORRECT,
            RecommendationStatus.RESOLVED,
            RecommendationStatus.EXPIRED,
        }
        assert expected == TERMINAL_STATUSES

    @pytest.mark.parametrize("status", list(TERMINAL_STATUSES))
    def test_is_terminal_status_true_for_terminal(self, status):
        assert is_terminal_status(status) is True

    @pytest.mark.parametrize(
        "status",
        [
            RecommendationStatus.GENERATED,
            RecommendationStatus.REVIEWED,
            RecommendationStatus.ACCEPTED,
            RecommendationStatus.PARTIALLY_USEFUL,
        ],
    )
    def test_is_terminal_status_false_for_non_terminal(self, status):
        assert is_terminal_status(status) is False


class TestFeedbackVerdictStatuses:
    def test_resolved_and_expired_are_excluded(self):
        assert RecommendationStatus.RESOLVED not in FEEDBACK_VERDICT_STATUSES
        assert RecommendationStatus.EXPIRED not in FEEDBACK_VERDICT_STATUSES

    def test_every_other_non_generated_status_is_included(self):
        expected = {
            RecommendationStatus.REVIEWED,
            RecommendationStatus.ACCEPTED,
            RecommendationStatus.REJECTED,
            RecommendationStatus.PARTIALLY_USEFUL,
            RecommendationStatus.INCORRECT,
        }
        assert expected == FEEDBACK_VERDICT_STATUSES
