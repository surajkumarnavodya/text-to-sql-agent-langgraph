"""Unit tests for db/connection.py: connection-string building and connection-test classification.

Fully mocked -- no real database is ever contacted. `build_connection_url`
tests exercise the "malformed .env fails fast" requirement; `test_connection`
tests exercise the best-effort failure classification (auth/host/db-not-found
/driver-missing) by mocking `get_engine` and controlling what the fake engine
raises.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import URL

from config.settings import ConfigurationError, Settings
from db.connection import (
    ConnectionErrorCategory,
    build_connection_url,
    get_connection,
    get_sqlglot_dialect,
    list_connection_names,
)

# Aliased on import: pytest collects any module-level callable named
# `test_*` as a test case, which would otherwise pick up the real
# `db.connection.test_connection` function itself (it takes an optional
# arg, so pytest can call it with none) and run it as a bogus test.
from db.connection import test_connection as check_connection
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=None,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="ODBC Driver 17 for SQL Server",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    schema_top_k=4,
    max_retries=3,
    max_result_rows=1000,
    query_timeout_seconds=15,
    llm_max_tokens=1024,
    insight_max_tokens=120,
    max_question_length=500,
    question_rate_limit_per_minute=10,
    llm_call_rate_limit_per_minute=20,
    cost_estimation_enabled=True,
    cost_estimation_timeout_seconds=3,
    cost_moderate_row_threshold=50_000,
    cost_high_row_threshold=1_000_000,
    log_level="INFO",
    log_redaction_level="standard",
)


def _settings(**overrides: object) -> Settings:
    """A `_BASE_SETTINGS` copy with `overrides` applied, per test.

    `_BASE_SETTINGS` itself is a normal, fully type-checked `Settings(...)`
    call (every one of its ~30 fields is verified against the real dataclass
    signature). Only the per-test `**overrides` spread -- inherently
    arbitrary, since any test may override any subset of fields -- can't be
    verified statically and needs the one `type: ignore` below, same as
    `dataclasses.replace` would need spreading an untyped dict into any
    dataclass constructor. That's a much smaller surface than the previous
    pattern, which spread an untyped `dict` into the constructor for *every*
    field, defaults included.
    """
    return dataclasses.replace(_BASE_SETTINGS, **overrides)  # type: ignore[arg-type]


class TestBuildConnectionUrl:
    def test_builds_url_from_discrete_fields(self):
        url = build_connection_url(_settings())
        assert isinstance(url, URL)
        assert url.drivername == "postgresql+psycopg2"
        assert url.host == "db.example.com"
        assert url.database == "mydb"
        assert url.username == "reader"
        assert url.password == "secret"

    def test_uses_db_types_default_port_when_unset(self):
        url = build_connection_url(_settings(db_type="mysql", db_port=None))
        assert isinstance(url, URL)
        assert url.port == 3306

    def test_uses_explicit_port_when_set(self):
        url = build_connection_url(_settings(db_port=6543))
        assert isinstance(url, URL)
        assert url.port == 6543

    def test_mssql_includes_odbc_driver_query_param(self):
        url = build_connection_url(
            _settings(db_type="mssql", db_odbc_driver="ODBC Driver 18 for SQL Server")
        )
        assert isinstance(url, URL)
        assert url.drivername == "mssql+pyodbc"
        assert url.query["driver"] == "ODBC Driver 18 for SQL Server"

    def test_connection_string_override_bypasses_everything(self):
        raw = "sqlite:///:memory:"
        settings = _settings(db_type="not-a-real-type", db_host=None, db_connection_string=raw)
        assert build_connection_url(settings) == raw

    def test_raises_on_unsupported_db_type(self):
        with pytest.raises(ConfigurationError, match="Unsupported or missing DB_TYPE"):
            build_connection_url(_settings(db_type="mongodb"))

    def test_raises_on_missing_db_type(self):
        with pytest.raises(ConfigurationError):
            build_connection_url(_settings(db_type=""))

    def test_raises_on_missing_required_fields(self):
        with pytest.raises(ConfigurationError, match="DB_HOST"):
            build_connection_url(_settings(db_host=None))


class TestGetSqlglotDialect:
    @pytest.mark.parametrize(
        "db_type,expected",
        [
            ("postgresql", "postgres"),
            ("mysql", "mysql"),
            ("mssql", "tsql"),
            ("oracle", "oracle"),
        ],
    )
    def test_known_db_types(self, db_type, expected):
        assert get_sqlglot_dialect(db_type) == expected

    def test_unknown_db_type_returns_none(self):
        assert get_sqlglot_dialect("mongodb") is None

    def test_empty_db_type_returns_none(self):
        assert get_sqlglot_dialect("") is None


class TestGetConnection:
    """`get_connection`/`list_connection_names` -- how multi-database-aware
    code (embeddings.retriever.select_database, agent/nodes.py, the
    Streamlit sidebar) turns a database *name* back into its full
    `DatabaseConnectionConfig`. See `Settings.databases`'s docstring."""

    def test_single_database_setup_has_one_default_connection(self):
        settings = _settings()
        assert list_connection_names(settings) == ["default"]
        assert get_connection(settings, "default").db_type == settings.db_type

    def test_looks_up_a_named_connection_among_several(self):
        sales = dataclasses.replace(_BASE_SETTINGS.databases[0], name="sales", db_type="postgresql")
        hr = dataclasses.replace(_BASE_SETTINGS.databases[0], name="hr", db_type="mysql")
        settings = _settings(databases=(sales, hr))

        assert list_connection_names(settings) == ["sales", "hr"]
        assert get_connection(settings, "hr").db_type == "mysql"
        assert get_connection(settings, "sales").db_type == "postgresql"

    def test_unknown_connection_name_raises(self):
        settings = _settings()
        with pytest.raises(ConfigurationError, match="Unknown database connection"):
            get_connection(settings, "does_not_exist")


def _mock_engine(dialect_name: str = "postgresql") -> MagicMock:
    engine = MagicMock()
    engine.dialect.name = dialect_name
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = ("PostgreSQL 16.1",)
    engine.connect.return_value.__enter__.return_value = connection
    engine.connect.return_value.__exit__.return_value = False
    return engine


class TestConnectionClassification:
    def test_success_returns_version(self, monkeypatch):
        monkeypatch.setattr("db.connection.get_engine", lambda settings=None: _mock_engine())

        result = check_connection(_settings())

        assert result.success
        assert result.category is None
        assert result.db_version == "PostgreSQL 16.1"

    def test_configuration_error_is_classified(self, monkeypatch):
        def _raise(settings=None):
            raise ConfigurationError("Missing required connection setting(s): DB_HOST.")

        monkeypatch.setattr("db.connection.get_engine", _raise)

        result = check_connection(_settings())

        assert not result.success
        assert result.category == ConnectionErrorCategory.CONFIGURATION

    def test_auth_failure_is_classified(self, monkeypatch):
        engine = _mock_engine()
        engine.connect.side_effect = Exception(
            'FATAL: password authentication failed for user "reader"'
        )
        monkeypatch.setattr("db.connection.get_engine", lambda settings=None: engine)

        result = check_connection(_settings())

        assert not result.success
        assert result.category == ConnectionErrorCategory.AUTH_FAILURE

    def test_host_unreachable_is_classified(self, monkeypatch):
        engine = _mock_engine()
        engine.connect.side_effect = Exception("could not connect to server: Connection refused")
        monkeypatch.setattr("db.connection.get_engine", lambda settings=None: engine)

        result = check_connection(_settings())

        assert not result.success
        assert result.category == ConnectionErrorCategory.HOST_UNREACHABLE

    def test_database_not_found_is_classified(self, monkeypatch):
        engine = _mock_engine()
        engine.connect.side_effect = Exception('database "nope" does not exist')
        monkeypatch.setattr("db.connection.get_engine", lambda settings=None: engine)

        result = check_connection(_settings())

        assert not result.success
        assert result.category == ConnectionErrorCategory.DATABASE_NOT_FOUND

    def test_missing_driver_is_classified(self, monkeypatch):
        engine = _mock_engine()
        engine.connect.side_effect = ModuleNotFoundError("No module named 'psycopg2'")
        monkeypatch.setattr("db.connection.get_engine", lambda settings=None: engine)

        result = check_connection(_settings())

        assert not result.success
        assert result.category == ConnectionErrorCategory.DRIVER_MISSING

    def test_unrecognized_error_falls_back_to_unknown(self, monkeypatch):
        engine = _mock_engine()
        engine.connect.side_effect = Exception("something completely unexpected happened")
        monkeypatch.setattr("db.connection.get_engine", lambda settings=None: engine)

        result = check_connection(_settings())

        assert not result.success
        assert result.category == ConnectionErrorCategory.UNKNOWN
