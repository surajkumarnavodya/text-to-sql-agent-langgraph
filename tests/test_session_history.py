"""Unit tests for session query history state management (ui/session_history.py).

All functions here are pure (list in, list out) by design specifically so
they're testable without a running Streamlit app -- see the module
docstring. `AgentState` inputs are built as plain dicts, matching the
pattern used in tests/test_agent_nodes.py.
"""

from __future__ import annotations

from agent.state import AgentState
from ui.session_history import (
    MAX_FOLLOWUP_EXCHANGES,
    append_entry,
    build_conversation_history,
    clear_history,
    new_history_entry,
    replace_entry,
    status_label,
    with_confirmed_error,
    with_confirmed_result,
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
        assert entry.confirmed_columns is None

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


class TestAddAndClear:
    def test_append_entry_does_not_mutate_original_list(self):
        history = []
        entry = new_history_entry("q1", _succeeded_state())
        new_history = append_entry(history, entry)

        assert history == []
        assert new_history == [entry]

    def test_append_preserves_order_oldest_first(self):
        e1 = new_history_entry("first", _succeeded_state())
        e2 = new_history_entry("second", _succeeded_state())
        history = append_entry(append_entry([], e1), e2)
        assert [e.question for e in history] == ["first", "second"]

    def test_clear_history_returns_empty_list(self):
        history = append_entry([], new_history_entry("q1", _succeeded_state()))
        assert clear_history() == []
        assert history != []  # clear_history() does not mutate its caller's list


class TestReplaceEntry:
    def test_replaces_matching_entry_only(self):
        e1 = new_history_entry("q1", _succeeded_state())
        e2 = new_history_entry("q2", _succeeded_state())
        history = [e1, e2]

        updated_e1 = with_confirmed_result(e1, ["col"], [(1,)])
        result = replace_entry(history, e1.entry_id, updated_e1)

        assert result[0].confirmed_columns == ["col"]
        assert result[1] is e2


class TestConfirmedResultTracking:
    def test_with_confirmed_result_sets_columns_and_rows_clears_error(self):
        entry = new_history_entry("q1", _succeeded_state())
        updated = with_confirmed_result(entry, ["a", "b"], [(1, 2)])
        assert updated.confirmed_columns == ["a", "b"]
        assert updated.confirmed_rows == [(1, 2)]
        assert updated.confirmed_error is None

    def test_with_confirmed_error_clears_columns_and_rows(self):
        entry = new_history_entry("q1", _succeeded_state())
        entry = with_confirmed_result(entry, ["a"], [(1,)])
        updated = with_confirmed_error(entry, "timeout")
        assert updated.confirmed_error == "timeout"
        assert updated.confirmed_columns is None
        assert updated.confirmed_rows is None

    def test_confirmed_update_returns_new_object(self):
        entry = new_history_entry("q1", _succeeded_state())
        updated = with_confirmed_result(entry, ["a"], [(1,)])
        assert updated is not entry
        assert entry.confirmed_columns is None  # original untouched


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


class TestStatusLabel:
    def test_succeeded_with_no_retries(self):
        entry = new_history_entry("q", _succeeded_state())
        icon, label = status_label(entry)
        assert label == "succeeded"

    def test_succeeded_with_retries_labeled_retried(self):
        state = _succeeded_state()
        state["retry_count"] = 2
        entry = new_history_entry("q", state)
        _icon, label = status_label(entry)
        assert label == "retried"

    def test_failed(self):
        entry = new_history_entry("q", {"status": "failed", "retry_count": 1})
        _icon, label = status_label(entry)
        assert label == "failed"

    def test_needs_clarification(self):
        entry = new_history_entry("q", {"status": "needs_clarification", "retry_count": 0})
        _icon, label = status_label(entry)
        assert label == "needs clarification"
