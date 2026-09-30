"""Unit tests for db/row_count_estimate.py (Prompt 06).

All four per-dialect strategies query a mocked `Engine.connect()` context
manager -- never a real database -- confirming: the right catalog-only SQL
shape is used per dialect, a missing/NULL catalog value resolves to
`None`, an unsupported `db_type` resolves to `None`, and any
`SQLAlchemyError` fails open to `None` rather than raising.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from db.row_count_estimate import estimate_row_count, supports_row_count_estimate


def _mock_engine(fetchone_result):
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = fetchone_result

    @contextmanager
    def _connect(*args, **kwargs):
        yield connection

    engine = MagicMock()
    engine.connect.side_effect = _connect
    return engine, connection


class TestSupportsRowCountEstimate:
    @pytest.mark.parametrize("db_type", ["postgresql", "mysql", "mssql", "oracle"])
    def test_true_for_every_supported_db_type(self, db_type):
        assert supports_row_count_estimate(db_type) is True

    def test_false_for_unsupported_db_type(self):
        assert supports_row_count_estimate("duckdb") is False


class TestEstimateRowCountPerDialect:
    @pytest.mark.parametrize("db_type", ["postgresql", "mysql", "mssql", "oracle"])
    def test_returns_the_catalog_value_when_present(self, db_type):
        engine, _ = _mock_engine((12345,))
        assert estimate_row_count(engine, "orders", "dbo", db_type) == 12345

    @pytest.mark.parametrize("db_type", ["postgresql", "mysql", "mssql", "oracle"])
    def test_returns_none_when_no_row_matches(self, db_type):
        engine, _ = _mock_engine(None)
        assert estimate_row_count(engine, "orders", "dbo", db_type) is None

    @pytest.mark.parametrize("db_type", ["postgresql", "mysql", "mssql", "oracle"])
    def test_returns_none_when_the_catalog_value_itself_is_null(self, db_type):
        """Oracle's NUM_ROWS before DBMS_STATS has run, or a partition-stats
        row that happens to sum to NULL, must resolve to None, not raise or
        return 0 (0 would falsely claim 'known to be empty')."""
        engine, _ = _mock_engine((None,))
        assert estimate_row_count(engine, "orders", "dbo", db_type) is None

    def test_oracle_uppercases_table_and_schema(self):
        engine, connection = _mock_engine((1,))
        estimate_row_count(engine, "orders", "hr", "oracle")
        _, params = connection.execute.call_args[0]
        assert params["table"] == "ORDERS"
        assert params["schema"] == "HR"

    def test_none_schema_is_passed_through_not_defaulted(self):
        engine, connection = _mock_engine((1,))
        estimate_row_count(engine, "orders", None, "postgresql")
        _, params = connection.execute.call_args[0]
        assert params["schema"] is None


class TestEstimateRowCountFailsOpen:
    def test_unsupported_db_type_returns_none_without_querying(self):
        engine = MagicMock()
        assert estimate_row_count(engine, "orders", "dbo", "duckdb") is None
        engine.connect.assert_not_called()

    def test_sqlalchemy_error_returns_none_not_raise(self):
        engine = MagicMock()
        engine.connect.side_effect = SQLAlchemyError("connection refused")
        assert estimate_row_count(engine, "orders", "dbo", "mssql") is None
