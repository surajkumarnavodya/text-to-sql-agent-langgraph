"""Unit tests for api/main.py's `_enforce_database_write_privileges` -- the
2026 Phase 3 fix wiring `db.connection.check_write_privileges` into actual
application startup (previously only reachable via the manual
`scripts/test_db_connection.py` CLI, so a deployment that never ran that
script by hand got no signal at all that its "read-only" DB_USER wasn't
really read-only). Fully mocked -- no real database, no LangGraph graph
build, no Ollama client; this only exercises the plain function, not the
whole ASGI app.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import ConfigurationError, Settings
from db.connection import WritePrivilegeCheckResult
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
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="x",
    embedding_model_name="x",
    schema_top_k=4,
    max_retries=3,
    complex_query_max_retry_bonus=2,
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
    api_auth_token=SecretStr("dummy-token"),
)


def _settings(**overrides: object) -> Settings:
    """Mirrors `tests/test_write_privilege_check.py`'s own `_settings`
    helper -- rebuilds via `Settings(**{**_BASE_SETTINGS.__dict__, **overrides})`
    (not `model_copy`, which would skip validation)."""
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


def _result(checked: bool, has_write_privileges: bool | None) -> WritePrivilegeCheckResult:
    return WritePrivilegeCheckResult(
        checked=checked, has_write_privileges=has_write_privileges, message="test"
    )


class TestEnforceDatabaseWritePrivileges:
    def test_no_write_privileges_never_raises(self):
        from api.main import _enforce_database_write_privileges

        with patch("api.main.check_write_privileges", return_value=_result(True, False)):
            _enforce_database_write_privileges(
                {"default": object()}, _settings(environment="production")
            )  # must not raise

    def test_check_not_run_fails_open_even_in_production(self):
        """A database whose check itself couldn't run (unsupported DB_TYPE,
        insufficient privilege to read the catalog) must never be treated
        as evidence of write access -- see check_write_privileges' own
        fail-open contract."""
        from api.main import _enforce_database_write_privileges

        with patch("api.main.check_write_privileges", return_value=_result(False, None)):
            _enforce_database_write_privileges(
                {"default": object()}, _settings(environment="production")
            )  # must not raise

    def test_write_privileges_detected_raises_in_production(self):
        from api.main import _enforce_database_write_privileges

        with (
            patch("api.main.check_write_privileges", return_value=_result(True, True)),
            pytest.raises(ConfigurationError, match="write privileges"),
        ):
            _enforce_database_write_privileges(
                {"default": object()}, _settings(environment="production")
            )

    def test_write_privileges_detected_only_warns_outside_production(self):
        from api.main import _enforce_database_write_privileges

        with patch("api.main.check_write_privileges", return_value=_result(True, True)):
            _enforce_database_write_privileges(
                {"default": object()}, _settings(environment="development")
            )  # must not raise -- development is warn-only

    def test_write_privileges_message_names_the_affected_database(self):
        from api.main import _enforce_database_write_privileges

        with (
            patch("api.main.check_write_privileges", return_value=_result(True, True)),
            pytest.raises(ConfigurationError, match="default"),
        ):
            _enforce_database_write_privileges(
                {"default": object()}, _settings(environment="production")
            )

    def test_emits_critical_audit_event_when_detected(self):
        from api.main import _enforce_database_write_privileges

        with (
            patch("api.main.check_write_privileges", return_value=_result(True, True)),
            patch("api.main.log_security_event") as mock_log,
        ):
            with pytest.raises(ConfigurationError):
                _enforce_database_write_privileges(
                    {"default": object()}, _settings(environment="production")
                )
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == "db_write_privileges_detected"
            assert args[1] == "critical"
            assert kwargs["database"] == "default"

    def test_no_audit_event_when_clean(self):
        from api.main import _enforce_database_write_privileges

        with (
            patch("api.main.check_write_privileges", return_value=_result(True, False)),
            patch("api.main.log_security_event") as mock_log,
        ):
            _enforce_database_write_privileges({"default": object()}, _settings())
            mock_log.assert_not_called()
