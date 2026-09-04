"""Unit tests for config/settings.py's security-relevant validation and
SecretStr wiring, added by the enterprise security audit (item M: security
configuration validation)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from config.settings import ConfigurationError, Settings, get_settings
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("S3cr3t!"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="x",
    embedding_model_name="x",
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
    """A `_BASE_SETTINGS` copy with `overrides` applied -- see
    `tests/test_connection.py::_settings` for why `dataclasses.replace` is
    used here instead of spreading a dict into `Settings(**...)` directly."""
    return dataclasses.replace(_BASE_SETTINGS, **overrides)  # type: ignore[arg-type]


class TestSecretFieldCoercion:
    def test_db_password_is_wrapped_in_secretstr(self):
        settings = _settings()
        assert isinstance(settings.db_password, SecretStr)
        assert settings.db_password == "S3cr3t!"

    def test_db_connection_string_is_wrapped_in_secretstr(self):
        settings = _settings(db_connection_string="postgresql://reader:pw@host/db")
        assert isinstance(settings.db_connection_string, SecretStr)

    def test_none_password_stays_none(self):
        settings = _settings(db_password=None)
        assert settings.db_password is None

    def test_settings_repr_never_contains_the_password(self):
        settings = _settings()
        assert "S3cr3t!" not in repr(settings)


class TestPositiveValueValidation:
    @pytest.mark.parametrize(
        "field",
        [
            "max_retries",
            "max_result_rows",
            "query_timeout_seconds",
            "llm_max_tokens",
            "insight_max_tokens",
            "max_question_length",
            "question_rate_limit_per_minute",
            "llm_call_rate_limit_per_minute",
            "cost_estimation_timeout_seconds",
            "cost_moderate_row_threshold",
        ],
    )
    @pytest.mark.parametrize("bad_value", [0, -1, -100])
    def test_non_positive_value_raises(self, field, bad_value):
        with pytest.raises(ConfigurationError, match="positive"):
            _settings(**{field: bad_value})

    def test_valid_settings_do_not_raise(self):
        _settings()  # should not raise


class TestCostThresholdOrdering:
    def test_moderate_below_high_is_accepted(self):
        _settings(cost_moderate_row_threshold=100, cost_high_row_threshold=1000)

    def test_moderate_equal_to_high_is_rejected(self):
        with pytest.raises(ConfigurationError, match="strictly less than"):
            _settings(cost_moderate_row_threshold=1000, cost_high_row_threshold=1000)

    def test_moderate_above_high_is_rejected(self):
        with pytest.raises(ConfigurationError, match="strictly less than"):
            _settings(cost_moderate_row_threshold=2000, cost_high_row_threshold=1000)


class TestLogRedactionLevelValidation:
    @pytest.mark.parametrize("level", ["standard", "strict"])
    def test_known_level_is_accepted(self, level):
        settings = _settings(log_redaction_level=level)
        assert settings.log_redaction_level == level

    def test_unknown_level_raises(self):
        with pytest.raises(ConfigurationError, match="LOG_REDACTION_LEVEL"):
            _settings(log_redaction_level="verbose")


class TestMultiDatabaseConfig:
    """`Settings.databases` -- the named-connection list `db.connection.
    get_connection` and `embeddings.retriever.select_database` read from.
    See `config.settings._parse_named_connections` and `Settings.
    __post_init__`'s fallback."""

    def test_no_db_connections_falls_back_to_one_default_entry_matching_flat_fields(self):
        settings = _settings()

        assert [c.name for c in settings.databases] == ["default"]
        default = settings.databases[0]
        assert default.db_type == settings.db_type
        assert default.db_host == settings.db_host
        assert default.db_name == settings.db_name
        assert default.db_user == settings.db_user
        assert default.db_password == settings.db_password

    def test_directly_constructed_settings_without_databases_still_gets_a_default(self):
        """`__post_init__` runs on *every* construction (see its docstring)
        -- not just `get_settings()` -- so a hand-built `Settings(...)` in a
        test (or any other caller) never has an empty `.databases`.

        `databases=()` is passed explicitly alongside the overrides: `_BASE_
        SETTINGS` already has its own (postgresql-flavored) `.databases`
        baked in from its own construction, and `dataclasses.replace` only
        overwrites the fields named in **overrides -- without resetting
        `databases` too, the stale postgresql entry would be copied over
        unchanged even though `db_type` below is being changed to mysql.
        """
        settings = _settings(db_type="mysql", db_host="mysql-host", databases=())
        assert len(settings.databases) == 1
        assert settings.databases[0].db_type == "mysql"

    def test_db_connections_env_parses_named_connections(self, monkeypatch):
        monkeypatch.setenv("DB_CONNECTIONS", "sales,hr")
        monkeypatch.setenv("DB_SALES_TYPE", "postgresql")
        monkeypatch.setenv("DB_SALES_HOST", "sales-host")
        monkeypatch.setenv("DB_SALES_NAME", "salesdb")
        monkeypatch.setenv("DB_HR_TYPE", "mysql")
        monkeypatch.setenv("DB_HR_HOST", "hr-host")
        monkeypatch.setenv("DB_HR_NAME", "hrdb")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            names = [c.name for c in settings.databases]
            assert names == ["sales", "hr"]
            sales = settings.databases[0]
            assert sales.db_type == "postgresql"
            assert sales.db_host == "sales-host"
            assert sales.db_name == "salesdb"
            hr = settings.databases[1]
            assert hr.db_type == "mysql"
            assert hr.db_host == "hr-host"
        finally:
            get_settings.cache_clear()

    def test_db_connections_entry_missing_type_raises(self, monkeypatch):
        monkeypatch.setenv("DB_CONNECTIONS", "sales")
        monkeypatch.delenv("DB_SALES_TYPE", raising=False)
        get_settings.cache_clear()
        try:
            with pytest.raises(ConfigurationError, match="DB_SALES_TYPE"):
                get_settings()
        finally:
            get_settings.cache_clear()

    def test_db_connections_names_colliding_on_the_same_env_prefix_raises(self, monkeypatch):
        # "sales-east" and "sales_east" both normalize to DB_SALES_EAST_*.
        monkeypatch.setenv("DB_CONNECTIONS", "sales-east,sales_east")
        monkeypatch.setenv("DB_SALES_EAST_TYPE", "postgresql")
        get_settings.cache_clear()
        try:
            with pytest.raises(ConfigurationError, match="same env prefix"):
                get_settings()
        finally:
            get_settings.cache_clear()
