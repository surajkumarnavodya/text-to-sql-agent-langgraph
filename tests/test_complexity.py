"""Unit tests for the adaptive-retry-budget complexity heuristic (agent/complexity.py)."""

from __future__ import annotations

from agent.complexity import compute_max_retries, detect_complexity_signals


class TestDetectComplexitySignals:
    def test_plain_question_matches_nothing(self):
        assert detect_complexity_signals("How many customers are there?") == []

    def test_top_n_ordered_by_metric_is_not_per_group(self):
        """ "Top N by <metric>" is ordinary top-N (ORDER BY + LIMIT), not
        top-N-*per-group* -- must not false-positive on the single most
        common top-N phrasing."""
        assert detect_complexity_signals("Show me the top 10 customers by revenue.") == []

    def test_top_n_per_group_is_detected(self):
        signals = detect_complexity_signals("What is the top 5 products per region this year?")
        assert "top_n_per_group" in signals

    def test_top_n_for_each_group_is_detected(self):
        signals = detect_complexity_signals(
            "For each year and sales territory, show the top 3 product subcategories."
        )
        assert "top_n_per_group" in signals

    def test_growth_comparison_is_detected(self):
        assert "growth_comparison" in detect_complexity_signals(
            "What was the year-over-year growth in sales?"
        )

    def test_ranking_window_is_detected(self):
        assert "ranking_window" in detect_complexity_signals(
            "Show a running total of sales by month."
        )

    def test_multi_metric_needs_at_least_three_hits(self):
        assert detect_complexity_signals("Show revenue and profit.") == []  # only 2 keyword hits
        signals = detect_complexity_signals(
            "Show total revenue, profit, and distinct customer count."
        )
        assert "multi_metric" in signals  # "total", "revenue", "profit", "distinct" -> 4 hits

    def test_real_failing_question_matches_multiple_signals(self):
        """The exact question that exhausted the retry budget in production
        (nested-aggregate YoY growth + wrong-shape TOP N + GROUP BY) --
        should get every signal it plausibly can."""
        question = (
            "For each year and sales territory, show the top 3 product subcategories "
            "by Internet sales. Include revenue, profit, distinct customers, and "
            "year-over-year growth."
        )
        signals = detect_complexity_signals(question)
        assert set(signals) == {"top_n_per_group", "growth_comparison", "multi_metric"}


class TestComputeMaxRetries:
    def test_no_signals_returns_base_unchanged(self):
        max_retries, signals = compute_max_retries(
            "How many customers are there?", base_max_retries=3, max_bonus=2
        )
        assert max_retries == 3
        assert signals == []

    def test_bonus_is_capped_at_max_bonus(self):
        question = (
            "For each year and sales territory, show the top 3 product subcategories "
            "by Internet sales. Include revenue, profit, distinct customers, and "
            "year-over-year growth."
        )
        max_retries, signals = compute_max_retries(question, base_max_retries=3, max_bonus=2)
        assert len(signals) == 3  # three signals matched...
        assert max_retries == 5  # ...but the bonus is capped at 2, not 3

    def test_zero_bonus_disables_the_adaptive_budget(self):
        question = "What is the top 5 products per region this year?"
        max_retries, signals = compute_max_retries(question, base_max_retries=3, max_bonus=0)
        assert signals  # a signal did match...
        assert max_retries == 3  # ...but max_bonus=0 means no extra retries granted
