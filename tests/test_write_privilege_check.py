"""Unit tests for db/connection.py's check_write_privileges (item A: strong
database least-privilege design). Fully mocked -- no real database is ever
contacted."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from config.settings import Settings
from db.connection import check_write_privileges


def _settings(**overrides) -> Settings:
    defaults = dict(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=5432,
        db_name="mydb",
        db_user="reader",
        db_password="secret",
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
    defaults.update(overrides)
    return Settings(**defaults)


def _mock_engine(count: int | None) -> MagicMock:
    engine = MagicMock()
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (count,) if count is not None else None
    engine.connect.return_value.__enter__.return_value = connection
    engine.connect.return_value.__exit__.return_value = False
    return engine


class TestCheckWritePrivileges:
    def test_no_write_grants_reports_clean(self):
        result = check_write_privileges(_mock_engine(0), _settings())
        assert result.checked is True
        assert result.has_write_privileges is False
        assert "does not appear to have write privileges" in result.message

    def test_write_grants_present_is_flagged(self):
        result = check_write_privileges(_mock_engine(3), _settings())
        assert result.checked is True
        assert result.has_write_privileges is True
        assert "write privileges" in result.message.lower()

    def test_unsupported_db_type_fails_open_without_querying(self):
        engine = _mock_engine(0)
        result = check_write_privileges(engine, _settings(db_type="not_a_real_engine"))
        assert result.checked is False
        assert result.has_write_privileges is None
        engine.connect.assert_not_called()

    def test_query_error_fails_open(self):
        engine = _mock_engine(0)
        engine.connect.side_effect = RuntimeError("insufficient privilege to read catalog")
        result = check_write_privileges(engine, _settings())
        assert result.checked is False
        assert result.has_write_privileges is None
        assert result.message  # always set, even on failure

    def test_null_row_result_treated_as_no_privileges(self):
        result = check_write_privileges(_mock_engine(None), _settings())
        assert result.checked is True
        assert result.has_write_privileges is False

    def test_covers_all_four_supported_db_types(self):
        """Every DB_TYPE this app supports must have a strategy -- an
        engine silently missing from _WRITE_PRIVILEGE_QUERIES would mean
        this check quietly never runs for that engine."""
        for db_type in ("postgresql", "mysql", "mssql", "oracle"):
            result = check_write_privileges(_mock_engine(0), _settings(db_type=db_type))
            assert result.checked is True, f"{db_type} should have a strategy"
