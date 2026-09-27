"""Unit tests for `api/main.py`'s `_shutdown_ask_executor` -- enterprise
scalability assessment (2026-09-27): draining the bounded `/ask` thread pool
on process shutdown instead of abandoning in-flight requests. Fully
mocked -- no real thread pool, no app startup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from api.main import _get_ask_executor, _shutdown_ask_executor
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


class TestShutdownAskExecutor:
    def test_shuts_down_the_same_cached_executor_requests_already_use(self, monkeypatch):
        """`_get_ask_executor` is `@cache`d by `max_workers` -- shutdown
        must retrieve the exact same instance `/ask` requests were handed,
        not create a fresh, never-used pool and shut that down instead."""
        fake_executor = MagicMock()
        monkeypatch.setattr("api.main._get_ask_executor", lambda max_workers: fake_executor)

        _shutdown_ask_executor(_BASE_SETTINGS)

        fake_executor.shutdown.assert_called_once_with(wait=True)

    def test_waits_for_in_flight_work_rather_than_abandoning_it(self, monkeypatch):
        """wait=True, never wait=False/cancel_futures=True -- an in-flight
        request must be allowed to finish, not cut off mid-generation."""
        fake_executor = MagicMock()
        monkeypatch.setattr("api.main._get_ask_executor", lambda max_workers: fake_executor)

        _shutdown_ask_executor(_BASE_SETTINGS)

        _, kwargs = fake_executor.shutdown.call_args
        assert kwargs.get("wait") is True

    def test_uses_the_configured_max_concurrent_ask_requests_as_the_cache_key(self, monkeypatch):
        captured = {}

        def _fake_get_executor(max_workers):
            captured["max_workers"] = max_workers
            return MagicMock()

        monkeypatch.setattr("api.main._get_ask_executor", _fake_get_executor)
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "max_concurrent_ask_requests": 7})

        _shutdown_ask_executor(settings)

        assert captured["max_workers"] == 7

    def test_against_a_real_thread_pool_executor(self):
        """End-to-end against the real executor type -- confirms this
        doesn't just satisfy a mock's interface. Safe to shut down for
        real: `tests/conftest.py`'s autouse `_clear_process_singleton_caches`
        clears `_get_ask_executor`'s cache before every test (this one
        included), so no other test can ever be handed this now-shutdown
        instance."""
        executor = _get_ask_executor(_BASE_SETTINGS.max_concurrent_ask_requests)
        results: list[int] = []
        executor.submit(results.append, 1)

        _shutdown_ask_executor(_BASE_SETTINGS)

        assert results == [1]
        assert executor._shutdown is True
