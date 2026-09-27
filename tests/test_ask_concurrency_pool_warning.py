"""Unit tests for `api/main.py`'s `_warn_on_ask_concurrency_pool_mismatch` --
enterprise scalability assessment (2026-09-27): a startup signal for when
`MAX_CONCURRENT_ASK_REQUESTS` exceeds this process's own per-database
connection-pool capacity. Fully mocked -- no real database, no app startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from api.main import _warn_on_ask_concurrency_pool_mismatch
from config.settings import Settings
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
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


class TestWarnOnAskConcurrencyPoolMismatch:
    def test_no_warning_when_pool_capacity_covers_concurrency(self, caplog):
        settings = _settings(max_concurrent_ask_requests=10, db_pool_size=10, db_max_overflow=20)
        with caplog.at_level(logging.WARNING, logger="api.main"):
            _warn_on_ask_concurrency_pool_mismatch(settings)
        assert not any("MAX_CONCURRENT_ASK_REQUESTS" in r.message for r in caplog.records)

    def test_no_warning_when_exactly_at_capacity(self, caplog):
        settings = _settings(max_concurrent_ask_requests=30, db_pool_size=10, db_max_overflow=20)
        with caplog.at_level(logging.WARNING, logger="api.main"):
            _warn_on_ask_concurrency_pool_mismatch(settings)
        assert not any("MAX_CONCURRENT_ASK_REQUESTS" in r.message for r in caplog.records)

    def test_warns_when_concurrency_exceeds_pool_capacity(self, caplog):
        settings = _settings(max_concurrent_ask_requests=50, db_pool_size=10, db_max_overflow=20)
        with caplog.at_level(logging.WARNING, logger="api.main"):
            _warn_on_ask_concurrency_pool_mismatch(settings)
        matching = [r for r in caplog.records if "MAX_CONCURRENT_ASK_REQUESTS" in r.message]
        assert len(matching) == 1
        assert matching[0].levelno == logging.WARNING

    def test_never_raises_regardless_of_mismatch_size(self):
        """Purely a tuning signal, never a startup-blocking safety gate --
        unlike _enforce_database_write_privileges, this must never raise."""
        settings = _settings(max_concurrent_ask_requests=1000, db_pool_size=1, db_max_overflow=0)
        _warn_on_ask_concurrency_pool_mismatch(settings)  # must not raise

    def test_message_names_the_actual_configured_values(self, caplog):
        settings = _settings(max_concurrent_ask_requests=50, db_pool_size=5, db_max_overflow=5)
        with caplog.at_level(logging.WARNING, logger="api.main"):
            _warn_on_ask_concurrency_pool_mismatch(settings)
        message = next(
            r.message for r in caplog.records if "MAX_CONCURRENT_ASK_REQUESTS" in r.message
        )
        assert "50" in message
        assert "5" in message
        assert "10" in message  # pool_capacity = 5 + 5
