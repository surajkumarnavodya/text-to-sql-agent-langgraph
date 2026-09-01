"""Unit tests for config/settings.py's security-relevant validation and
SecretStr wiring, added by the enterprise security audit (item M: security
configuration validation)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from config.settings import ConfigurationError, Settings
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
