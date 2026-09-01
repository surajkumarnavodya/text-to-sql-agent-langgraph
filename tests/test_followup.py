"""Unit tests for the follow-up classification heuristic (agent/followup.py).

Pure function, no mocking required -- these tests exercise the
referring_signal / has_subject decision table directly against representative
standalone, follow-up, and ambiguous questions (see the module docstring for
the rationale behind each signal).
"""

from __future__ import annotations

import pytest

from agent.followup import classify_followup


class TestStandaloneQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "Show total sales by year",
            "How many customers do we have in Germany?",
            "Which sales territory had the highest sales for the Bikes category?",
            "List the top 10 products by revenue",
        ],
    )
    def test_classified_standalone_regardless_of_history(self, question):
        assert classify_followup(question, has_history=False).classification == "standalone"
        assert classify_followup(question, has_history=True).classification == "standalone"


class TestFollowupQuestions:
    @pytest.mark.parametrize(
        "question",
        [
            "Now break that down by month",
            "Just show the top 3 of those",
            "What about for 2013 instead?",
            "And what about Canada?",
            "Same but for last year",
        ],
    )
    def test_classified_followup_when_history_exists(self, question):
        result = classify_followup(question, has_history=True)
        assert result.classification == "followup"
        assert result.referring_signal is True

    @pytest.mark.parametrize(
        "question",
        [
            "Now break that down by month",
            "Just show the top 3 of those",
            "What about for 2013 instead?",
        ],
    )
    def test_same_referring_question_is_ambiguous_with_no_history(self, question):
        result = classify_followup(question, has_history=False)
        assert result.classification == "ambiguous"
        assert result.referring_signal is True


class TestAmbiguousQuestions:
    @pytest.mark.parametrize("question", ["why", "more", "hmm"])
    def test_bare_fragment_is_ambiguous_regardless_of_history(self, question):
        assert classify_followup(question, has_history=False).classification == "ambiguous"
        assert classify_followup(question, has_history=True).classification == "ambiguous"

    def test_result_records_which_patterns_matched(self):
        result = classify_followup("Now break that down by month", has_history=True)
        assert "leading_discourse_marker" in result.matched_patterns
        assert "referring_pronoun" in result.matched_patterns

    def test_standalone_question_matches_no_patterns(self):
        result = classify_followup("Show total sales by year", has_history=True)
        assert result.matched_patterns == ()
        assert result.has_subject is True
