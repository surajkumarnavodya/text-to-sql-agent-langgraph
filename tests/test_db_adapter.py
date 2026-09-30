"""Isolated contract tests for db/adapter.py -- DatabaseCapabilities
construction and the two new capability-accessor functions it backs, plus
a hand-built fake proving the DatabaseAdapter Protocol shape. No real
database, no SqlAlchemyDatabaseAdapter (see
tests/test_db_adapter_sqlalchemy.py for that).
"""

from __future__ import annotations

import pytest

from config.settings import ConfigurationError
from db.adapter import DatabaseAdapter, DatabaseCapabilities
from db.connection import ConnectionTestResult, WritePrivilegeCheckResult
from db.execution import _STATEMENT_TIMEOUT_SQL, supports_driver_level_statement_timeout
from db.query_cost import _STRATEGIES, CostEstimate, supports_cost_estimation
from db.schema_introspection import TableSchemaInfo


class TestSupportsDriverLevelStatementTimeout:
    @pytest.mark.parametrize("db_type", ["postgresql", "mysql"])
    def test_true_for_engines_with_a_simple_set_statement(self, db_type):
        assert supports_driver_level_statement_timeout(db_type) is True

    @pytest.mark.parametrize("db_type", ["mssql", "oracle"])
    def test_false_for_engines_without_one(self, db_type):
        assert supports_driver_level_statement_timeout(db_type) is False

    def test_matches_the_real_dict_exactly(self):
        """The actual regression this function exists to prevent: it must
        never diverge from `_STATEMENT_TIMEOUT_SQL`'s real keys."""
        for db_type in ("postgresql", "mysql", "mssql", "oracle", "unknown"):
            assert supports_driver_level_statement_timeout(db_type) == (
                db_type in _STATEMENT_TIMEOUT_SQL
            )


class TestSupportsCostEstimation:
    @pytest.mark.parametrize("db_type", ["postgresql", "mysql", "mssql", "oracle"])
    def test_true_for_every_supported_engine(self, db_type):
        assert supports_cost_estimation(db_type) is True

    def test_false_for_an_unrecognized_engine(self):
        assert supports_cost_estimation("duckdb") is False

    def test_matches_the_real_dict_exactly(self):
        for db_type in ("postgresql", "mysql", "mssql", "oracle", "duckdb"):
            assert supports_cost_estimation(db_type) == (db_type in _STRATEGIES)


class TestDatabaseCapabilitiesForDbType:
    def test_postgresql_capabilities(self):
        caps = DatabaseCapabilities.for_db_type("postgresql")
        assert caps.db_type == "postgresql"
        assert caps.sqlglot_dialect == "postgres"
        assert caps.driver_package == "psycopg2-binary"
        assert caps.default_port == 5432
        assert caps.supports_cost_estimation is True
        assert caps.supports_driver_level_statement_timeout is True

    def test_mssql_capabilities_lack_driver_level_timeout(self):
        caps = DatabaseCapabilities.for_db_type("mssql")
        assert caps.sqlglot_dialect == "tsql"
        assert caps.supports_driver_level_statement_timeout is False
        assert caps.supports_cost_estimation is True  # showplan-based, still real

    def test_oracle_capabilities_lack_driver_level_timeout(self):
        caps = DatabaseCapabilities.for_db_type("oracle")
        assert caps.supports_driver_level_statement_timeout is False

    def test_unsupported_db_type_raises_configuration_error(self):
        with pytest.raises(ConfigurationError):
            DatabaseCapabilities.for_db_type("duckdb")

    def test_capability_defaults_disclosed_honestly(self):
        """These are the two facts this module deliberately doesn't
        overclaim: no engine has on-demand cancellation, and parameter
        binding is a real, always-available capability nothing exercises
        on the main execution path yet."""
        caps = DatabaseCapabilities.for_db_type("postgresql")
        assert caps.supports_on_demand_cancellation is False
        assert caps.supports_parameter_binding is True
        assert caps.supports_write_privilege_check is True
        assert caps.supports_db_version_query is True

    def test_equality(self):
        assert DatabaseCapabilities.for_db_type("postgresql") == DatabaseCapabilities.for_db_type(
            "postgresql"
        )
        assert DatabaseCapabilities.for_db_type("postgresql") != DatabaseCapabilities.for_db_type(
            "mysql"
        )

    def test_repr_does_not_raise(self):
        assert "DatabaseCapabilities(" in repr(DatabaseCapabilities.for_db_type("postgresql"))


class _FakeDatabaseAdapter:
    """A minimal, hand-built stand-in proving the Protocol's shape is
    genuinely satisfiable by an independent implementation -- not just by
    SqlAlchemyDatabaseAdapter."""

    def __init__(self) -> None:
        self._capabilities = DatabaseCapabilities.for_db_type("postgresql")

    @property
    def capabilities(self) -> DatabaseCapabilities:
        return self._capabilities

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(success=True, message="ok")

    def introspect_schema(self, schema: str | None = None) -> list[TableSchemaInfo]:
        return []

    def get_schema_fingerprint(self, tables: list[TableSchemaInfo]) -> str:
        return "fake-fingerprint"

    def execute_readonly(
        self,
        sql: str,
        timeout_seconds: int,
        max_result_rows: int | None = None,
        params: dict[str, object] | None = None,
    ) -> tuple[list[str], list[tuple]]:
        return [], []

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        return None

    def check_write_privileges(self) -> WritePrivilegeCheckResult:
        return WritePrivilegeCheckResult(checked=True, has_write_privileges=False, message="ok")


class TestDatabaseAdapterProtocol:
    def test_a_conforming_implementation_satisfies_the_protocol(self):
        assert isinstance(_FakeDatabaseAdapter(), DatabaseAdapter)

    def test_a_non_conforming_object_does_not_satisfy_the_protocol(self):
        class _NotAnAdapter:
            pass

        assert not isinstance(_NotAnAdapter(), DatabaseAdapter)
