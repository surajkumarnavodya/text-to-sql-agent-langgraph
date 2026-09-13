"""Unit tests for eval/history.py: conversation-history bookkeeping for
programmatically driving the agent (eval/runner.py, scripts/run_eval.py).

All functions here are pure (list in, list out) by design so they're
testable without a live agent run -- `AgentState` inputs are built as plain
dicts, matching the pattern used in tests/test_agent_nodes.py.
"""

from __future__ import annotations

from agent.state import AgentState
from eval.history import (
    MAX_FOLLOWUP_EXCHANGES,
    build_conversation_history,
    new_history_entry,
)


def _succeeded_state(sql: str = "SELECT 1", tables: list[str] | None = None) -> AgentState:
    return {
        "status": "succeeded",
        "sql": sql,
        "retry_count": 0,
        "row_count": 3,
        "schema_tables": [
            {"table_name": t, "ddl": "", "similarity_score": 1.0} for t in (tables or ["orders"])
        ],
    }


class TestNewHistoryEntry:
    def test_captures_question_sql_status_and_tables(self):
        entry = new_history_entry("total sales by year", _succeeded_state())

        assert entry.question == "total sales by year"
        assert entry.sql == "SELECT 1"
        assert entry.agent_status == "succeeded"
        assert entry.tables == ["orders"]

    def test_entries_get_distinct_ids(self):
        first = new_history_entry("q1", _succeeded_state())
        second = new_history_entry("q2", _succeeded_state())
        assert first.entry_id != second.entry_id

    def test_failed_state_defaults_row_count_and_sql_to_none(self):
        state: AgentState = {"status": "failed", "retry_count": 2}
        entry = new_history_entry("a bad question", state)
        assert entry.agent_status == "failed"
        assert entry.sql is None
        assert entry.row_count is None
        assert entry.tables == []


class TestBuildConversationHistory:
    def test_only_succeeded_entries_are_included(self):
        succeeded = new_history_entry("good question", _succeeded_state())
        failed = new_history_entry("bad question", {"status": "failed", "retry_count": 3})
        clarify = new_history_entry("vague", {"status": "needs_clarification", "retry_count": 0})
        history = [succeeded, failed, clarify]

        exchanges = build_conversation_history(history)

        assert len(exchanges) == 1
        assert exchanges[0]["question"] == "good question"

    def test_capped_to_max_exchanges_keeping_most_recent(self):
        history = [
            new_history_entry(f"question {i}", _succeeded_state(sql=f"SELECT {i}"))
            for i in range(5)
        ]

        exchanges = build_conversation_history(history, max_exchanges=3)

        assert [e["question"] for e in exchanges] == [
            "question 2",
            "question 3",
            "question 4",
        ]

    def test_default_cap_matches_module_constant(self):
        history = [
            new_history_entry(f"question {i}", _succeeded_state())
            for i in range(MAX_FOLLOWUP_EXCHANGES + 2)
        ]
        assert len(build_conversation_history(history)) == MAX_FOLLOWUP_EXCHANGES

    def test_exchange_carries_tables_and_sql_for_prompt_reference(self):
        entry = new_history_entry(
            "sales by year", _succeeded_state(sql="SELECT year", tables=["Fact", "Dim"])
        )
        exchanges = build_conversation_history([entry])
        assert exchanges[0]["sql"] == "SELECT year"
        assert exchanges[0]["tables"] == ["Fact", "Dim"]

    def test_empty_history_yields_empty_context(self):
        assert build_conversation_history([]) == []
