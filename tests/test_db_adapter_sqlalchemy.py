"""Wiring tests for db/adapter.py's SqlAlchemyDatabaseAdapter -- proves every
method is a pure, zero-logic forward to the existing, already-tested
db.connection/db.execution/db.query_cost/db.schema_introspection functions
(mirroring tests/test_tools_definitions.py's monkeypatch-and-assert style),
plus parametrized compatibility tests asserting DatabaseCapabilities never
diverges from the real per-module dicts.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

import db.adapter as adapter_module
from config.settings import Settings
from db.adapter import DatabaseCapabilities, SqlAlchemyDatabaseAdapter
from db.connection import SUPPORTED_DB_TYPES, ConnectionTestResult, WritePrivilegeCheckResult
from db.execution import _STATEMENT_TIMEOUT_SQL
from db.query_cost import _STRATEGIES, CostEstimate


def _settings(**overrides) -> Settings:
    base = {
        "db_type": "postgresql",
        "db_host": "localhost",
        "db_name": "testdb",
    }
    base.update(overrides)
    return Settings(**base)


_SENTINEL_ENGINE = object()


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(adapter_module, "get_read_only_engine", lambda conn: _SENTINEL_ENGINE)
    return SqlAlchemyDatabaseAdapter(_settings())


class TestCapabilitiesResolvedOnConstruction:
    def test_capabilities_reflect_the_connection_db_type(self, monkeypatch):
        monkeypatch.setattr(adapter_module, "get_read_only_engine", lambda conn: _SENTINEL_ENGINE)
        instance = SqlAlchemyDatabaseAdapter(_settings(db_type="mssql"))
        assert instance.capabilities.db_type == "mssql"
        assert instance.capabilities == DatabaseCapabilities.for_db_type("mssql")


class TestTestConnectionForwarding:
    def test_forwards_to_the_real_function_with_the_connection(self, adapter, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_test_connection(connection):
            captured["connection"] = connection
            return ConnectionTestResult(success=True, message="ok")

        monkeypatch.setattr(adapter_module, "test_connection", _fake_test_connection)
        result = adapter.test_connection()
        assert result.success is True
        assert captured["connection"] is adapter._connection


class TestIntrospectSchemaForwarding:
    def test_forwards_engine_and_schema(self, adapter, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_introspect_schema(engine, schema):
            captured["engine"] = engine
            captured["schema"] = schema
            return []

        monkeypatch.setattr(adapter_module, "introspect_schema", _fake_introspect_schema)
        result = adapter.introspect_schema("dbo")
        assert result == []
        assert captured["engine"] is _SENTINEL_ENGINE
        assert captured["schema"] == "dbo"

    def test_schema_defaults_to_none(self, adapter, monkeypatch):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            adapter_module,
            "introspect_schema",
            lambda engine, schema: captured.setdefault("schema", schema) or [],
        )
        adapter.introspect_schema()
        assert captured["schema"] is None


class TestGetSchemaFingerprintForwarding:
    def test_forwards_tables_unmodified(self, adapter, monkeypatch):
        sentinel_tables = ["not-really-a-table"]
        captured: dict[str, object] = {}

        def _fake_fingerprint(tables):
            captured["tables"] = tables
            return "fingerprint-value"

        monkeypatch.setattr(adapter_module, "get_schema_fingerprint", _fake_fingerprint)
        assert adapter.get_schema_fingerprint(sentinel_tables) == "fingerprint-value"
        assert captured["tables"] is sentinel_tables


class TestExecuteReadonlyForwarding:
    def test_forwards_all_arguments_including_engine(self, adapter, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_execute_readonly_sql(sql, timeout, max_rows, engine=None, params=None):
            captured.update(
                sql=sql, timeout=timeout, max_rows=max_rows, engine=engine, params=params
            )
            return (["col"], [(1,)])

        monkeypatch.setattr(adapter_module, "execute_readonly_sql", _fake_execute_readonly_sql)
        columns, rows = adapter.execute_readonly("SELECT 1", 30, max_result_rows=100)
        assert columns == ["col"]
        assert rows == [(1,)]
        assert captured["sql"] == "SELECT 1"
        assert captured["timeout"] == 30
        assert captured["max_rows"] == 100
        assert captured["engine"] is _SENTINEL_ENGINE
        assert captured["params"] is None

    def test_omitting_params_is_a_no_op_matching_the_original_call_shape(
        self, adapter, monkeypatch
    ):
        """Regression test for the new `params` parameter added alongside
        this adapter: a caller that never passes params must produce the
        exact same None value at the real execute_readonly_sql call site."""
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            adapter_module,
            "execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None, params=None: captured.setdefault(
                "params", params
            )
            or (["c"], []),
        )
        adapter.execute_readonly("SELECT 1", 30)
        assert captured.get("params") is None

    def test_params_are_threaded_through_when_provided(self, adapter, monkeypatch):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            adapter_module,
            "execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None, params=None: captured.setdefault(
                "params", params
            )
            or (["c"], []),
        )
        adapter.execute_readonly("SELECT :id", 30, params={"id": 5})
        assert captured["params"] == {"id": 5}

    def test_timeout_error_propagates_not_swallowed(self, adapter, monkeypatch):
        def _raise_timeout(sql, timeout, max_rows, engine=None, params=None):
            raise TimeoutError("exceeded")

        monkeypatch.setattr(adapter_module, "execute_readonly_sql", _raise_timeout)
        with pytest.raises(TimeoutError):
            adapter.execute_readonly("SELECT 1", 30)

    def test_sqlalchemy_error_propagates_not_swallowed(self, adapter, monkeypatch):
        def _raise_sqlalchemy_error(sql, timeout, max_rows, engine=None, params=None):
            raise SQLAlchemyError("bad column")

        monkeypatch.setattr(adapter_module, "execute_readonly_sql", _raise_sqlalchemy_error)
        with pytest.raises(SQLAlchemyError):
            adapter.execute_readonly("SELECT 1", 30)


class TestEstimateCostForwarding:
    def test_forwards_engine_sql_db_type_and_settings(self, adapter, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_estimate_query_cost(engine, sql, db_type, settings):
            captured.update(engine=engine, sql=sql, db_type=db_type, settings=settings)
            return None

        monkeypatch.setattr(adapter_module, "estimate_query_cost", _fake_estimate_query_cost)
        result = adapter.estimate_cost("SELECT 1")
        assert result is None
        assert captured["engine"] is _SENTINEL_ENGINE
        assert captured["sql"] == "SELECT 1"
        assert captured["db_type"] == "postgresql"
        assert captured["settings"] is adapter._settings

    def test_never_raises_stays_true_through_the_wrapper(self, adapter, monkeypatch):
        estimate = CostEstimate(
            estimated_rows=10.0, estimated_cost=1.0, severity="low", plan_summary="scan"
        )
        monkeypatch.setattr(
            adapter_module, "estimate_query_cost", lambda engine, sql, db_type, settings: estimate
        )
        assert adapter.estimate_cost("SELECT 1") is estimate


class TestCheckWritePrivilegesForwarding:
    def test_forwards_engine_and_connection(self, adapter, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_check_write_privileges(engine, connection):
            captured.update(engine=engine, connection=connection)
            return WritePrivilegeCheckResult(checked=True, has_write_privileges=False, message="ok")

        monkeypatch.setattr(adapter_module, "check_write_privileges", _fake_check_write_privileges)
        result = adapter.check_write_privileges()
        assert result.checked is True
        assert captured["engine"] is _SENTINEL_ENGINE
        assert captured["connection"] is adapter._connection


class TestEngineIsNeverCreatedTwice:
    def test_get_read_only_engine_called_fresh_per_method_but_never_cached_a_second_engine(
        self, monkeypatch
    ):
        """Constructing the adapter itself must never call get_read_only_engine
        -- only an actual method call does, and it always resolves through
        the same already-cached function, never a second engine/pool."""
        calls: list[object] = []

        def _fake_get_read_only_engine(conn: object) -> object:
            calls.append(conn)
            return _SENTINEL_ENGINE

        monkeypatch.setattr(adapter_module, "get_read_only_engine", _fake_get_read_only_engine)
        instance = SqlAlchemyDatabaseAdapter(_settings())
        assert calls == []  # constructing the adapter alone touches no engine
        monkeypatch.setattr(
            adapter_module,
            "test_connection",
            lambda conn: ConnectionTestResult(success=True, message="ok"),
        )
        instance.test_connection()  # doesn't need an engine at all (delegates to test_connection)
        monkeypatch.setattr(adapter_module, "introspect_schema", lambda engine, schema: [])
        instance.introspect_schema()
        assert calls == [instance._connection]


@pytest.mark.parametrize("db_type", sorted(SUPPORTED_DB_TYPES))
class TestCapabilityCompatibilityAcrossAllSupportedDbTypes:
    """The actual regression this prompt exists to prevent: a future 5th
    dict entry added to one module (`_STATEMENT_TIMEOUT_SQL`/`_STRATEGIES`)
    without a matching change elsewhere must not silently drift -- these
    assertions read the *real* dicts directly, never a hand-copied parallel
    truth table."""

    def test_sqlglot_dialect_and_driver_match_supported_db_types(self, db_type):
        caps = DatabaseCapabilities.for_db_type(db_type)
        info = SUPPORTED_DB_TYPES[db_type]
        assert caps.sqlglot_dialect == info.sqlglot_dialect
        assert caps.driver_package == info.driver_package
        assert caps.default_port == info.default_port

    def test_statement_timeout_support_matches_the_real_dict(self, db_type):
        caps = DatabaseCapabilities.for_db_type(db_type)
        assert caps.supports_driver_level_statement_timeout == (db_type in _STATEMENT_TIMEOUT_SQL)

    def test_cost_estimation_support_matches_the_real_dict(self, db_type):
        caps = DatabaseCapabilities.for_db_type(db_type)
        assert caps.supports_cost_estimation == (db_type in _STRATEGIES)

    def test_write_privilege_and_version_checks_are_not_overclaimed_false(self, db_type):
        caps = DatabaseCapabilities.for_db_type(db_type)
        assert caps.supports_write_privilege_check is True
        assert caps.supports_db_version_query is True

    def test_on_demand_cancellation_is_never_claimed(self, db_type):
        """Disclosed, not invented -- no engine has a real cancel-in-flight
        API anywhere in this codebase today."""
        caps = DatabaseCapabilities.for_db_type(db_type)
        assert caps.supports_on_demand_cancellation is False
