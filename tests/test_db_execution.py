"""Unit tests for db/execution.py: read-only SQL execution mechanics.

Fully mocked -- no real database or thread ever actually talks to a server.
Exercises the two behaviors that used to have no direct unit test (only
indirect coverage through `execute_sql_node`'s error-handling branches, which
mock `execute_readonly_sql` away entirely): the row-cap fetch on success, and
the background-thread timeout/abort path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from db.execution import execute_readonly_sql


def _mock_engine(dialect_name: str = "postgresql", execute_side_effect=None) -> MagicMock:
    engine = MagicMock()
    engine.dialect.name = dialect_name
    connection = MagicMock()
    if execute_side_effect is not None:
        connection.execute.side_effect = execute_side_effect
    else:
        cursor_result = MagicMock()
        cursor_result.keys.return_value = ["id", "name"]
        cursor_result.fetchmany.return_value = [(1, "a"), (2, "b")]
        connection.execute.return_value = cursor_result
    engine.connect.return_value.__enter__.return_value = connection
    engine.connect.return_value.__exit__.return_value = False
    return engine


class TestExecuteReadonlySql:
    def test_returns_columns_and_row_capped_rows(self, monkeypatch):
        engine = _mock_engine()
        monkeypatch.setattr("db.execution.get_read_only_engine", lambda: engine)

        columns, rows = execute_readonly_sql("SELECT id, name FROM t", 5, max_result_rows=10)

        assert columns == ["id", "name"]
        assert rows == [(1, "a"), (2, "b")]
        # fetchmany, not fetchall -- the row cap is enforced at the cursor
        # level independent of any LIMIT already in the SQL text.
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchmany.assert_called_once_with(10)

    def test_defaults_max_result_rows_from_settings(self, monkeypatch):
        engine = _mock_engine()
        monkeypatch.setattr("db.execution.get_read_only_engine", lambda: engine)
        fake_settings = MagicMock(max_result_rows=42)
        monkeypatch.setattr("db.execution.get_settings", lambda: fake_settings)

        execute_readonly_sql("SELECT 1", 5, max_result_rows=None)

        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchmany.assert_called_once_with(42)

    def test_applies_driver_level_statement_timeout_for_known_dialect(self, monkeypatch):
        engine = _mock_engine(dialect_name="postgresql")
        monkeypatch.setattr("db.execution.get_read_only_engine", lambda: engine)

        execute_readonly_sql("SELECT 1", 5, max_result_rows=10)

        connection = engine.connect.return_value.__enter__.return_value
        executed_sql_texts = [str(call.args[0]) for call in connection.execute.call_args_list]
        assert any("statement_timeout" in text for text in executed_sql_texts)

    def test_skips_statement_timeout_for_unmapped_dialect(self, monkeypatch):
        engine = _mock_engine(dialect_name="oracle")
        monkeypatch.setattr("db.execution.get_read_only_engine", lambda: engine)

        columns, rows = execute_readonly_sql("SELECT 1", 5, max_result_rows=10)

        # No SET-statement dialect mapping for oracle -- only the real query
        # itself should have been executed.
        connection = engine.connect.return_value.__enter__.return_value
        assert connection.execute.call_count == 1
        assert columns == ["id", "name"]

    def test_propagates_execution_error(self, monkeypatch):
        from sqlalchemy.exc import SQLAlchemyError

        def _raise(*args, **kwargs):
            raise SQLAlchemyError("column does not exist")

        engine = _mock_engine(execute_side_effect=_raise)
        monkeypatch.setattr("db.execution.get_read_only_engine", lambda: engine)

        with pytest.raises(SQLAlchemyError, match="column does not exist"):
            execute_readonly_sql("SELECT bad_col FROM t", 5, max_result_rows=10)

    def test_raises_timeout_error_when_query_exceeds_deadline(self, monkeypatch):
        import time

        def _slow_execute(*args, **kwargs):
            time.sleep(0.3)
            return MagicMock()

        engine = _mock_engine(execute_side_effect=_slow_execute)
        monkeypatch.setattr("db.execution.get_read_only_engine", lambda: engine)

        # 0s timeout (an int, matching the real signature) -- worker.join(0)
        # returns immediately, and the query is still guaranteed to be
        # "alive" (it sleeps 0.3s), so this deterministically exercises the
        # abort path without the test itself waiting on a real timeout.
        with pytest.raises(TimeoutError, match="exceeded the 0s timeout"):
            execute_readonly_sql("SELECT slow_query()", 0, max_result_rows=10)
