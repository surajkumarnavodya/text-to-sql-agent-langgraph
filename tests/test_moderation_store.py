"""Unit tests for moderation/store.py -- the SQL Server metadata store's
dedupe lookup, upsert, schema DDL, and pool configuration.

Mirrors `tests/test_rag_pdf_download.py`'s `MagicMock`-engine convention
(this repo's established pattern for `rag/store.py`-shaped modules): a bare
`MagicMock()` engine whose `.begin()`/`.connect()` yield a given connection
mock, passed directly as the `engine:` parameter -- never a real SQL
Server, never a real SQLite engine.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from moderation.exceptions import ModerationNotConfiguredError
from moderation.store import (
    ensure_schema,
    get_asset_by_hash,
    get_moderation_engine,
    record_asset,
)
from security.secrets import SecretStr


def _mock_engine(connection: MagicMock) -> MagicMock:
    """Mirrors `tests/test_rag_pdf_download.py`'s `_mock_engine` exactly."""
    engine = MagicMock()
    for entry_point in (engine.begin, engine.connect):
        entry_point.return_value.__enter__.return_value = connection
        entry_point.return_value.__exit__.return_value = False
    return engine


class TestGetModerationEngine:
    def test_raises_when_connection_string_unset(self):
        settings = Settings(moderation_store_connection_string=None)
        with pytest.raises(ModerationNotConfiguredError):
            get_moderation_engine(settings)

    def test_passes_explicit_pool_kwargs_to_create_engine(self, monkeypatch):
        captured = {}

        def _fake_create_engine(connection_string, **kwargs):
            captured["connection_string"] = connection_string
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("moderation.store.create_engine", _fake_create_engine)
        settings = Settings(
            moderation_store_connection_string=SecretStr("mssql+pyodbc://x"),
            moderation_store_pool_size=7,
            moderation_store_max_overflow=13,
            moderation_store_pool_recycle_seconds=900,
        )

        get_moderation_engine(settings)

        assert captured["pool_size"] == 7
        assert captured["max_overflow"] == 13
        assert captured["pool_recycle"] == 900
        assert captured["pool_pre_ping"] is True


class TestGetAssetByHash:
    def test_returns_none_when_no_row(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        engine = _mock_engine(connection)

        assert get_asset_by_hash(engine, "abc123") is None

    def test_deserializes_a_passed_row(self):
        connection = MagicMock()
        row = MagicMock(
            asset_id="asset-1",
            file_hash="abc123",
            media_type="image",
            moderation_status="passed",
            moderation_checks=json.dumps({"status": "passed"}),
            chunk_count=1,
            vector_ids=json.dumps(["abc123"]),
        )
        connection.execute.return_value.fetchone.return_value = row
        engine = _mock_engine(connection)

        result = get_asset_by_hash(engine, "abc123")

        assert result is not None
        assert result.moderation_status == "passed"
        assert result.vector_ids == ["abc123"]
        assert result.moderation_checks == {"status": "passed"}

    def test_deserializes_a_rejected_row_with_null_vector_ids(self):
        connection = MagicMock()
        row = MagicMock(
            asset_id="asset-2",
            file_hash="def456",
            media_type="video",
            moderation_status="rejected",
            moderation_checks=json.dumps({"status": "rejected", "triggering_categories": ["hate"]}),
            chunk_count=0,
            vector_ids=None,
        )
        connection.execute.return_value.fetchone.return_value = row
        engine = _mock_engine(connection)

        result = get_asset_by_hash(engine, "def456")

        assert result.moderation_status == "rejected"
        assert result.vector_ids is None


class TestRecordAsset:
    def test_deletes_then_inserts_keyed_on_file_hash(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "new-asset-id"
        engine = _mock_engine(connection)

        asset_id = record_asset(
            engine,
            "abc123",
            "image",
            "passed",
            {"status": "passed"},
            source_path="/media/photo.jpg",
            chunk_count=1,
            vector_ids=["abc123"],
        )

        assert asset_id == "new-asset-id"
        # First call is the DELETE (upsert-by-hash), second is the INSERT.
        first_call_sql = str(connection.execute.call_args_list[0].args[0])
        assert "DELETE" in first_call_sql
        second_call_args = connection.execute.call_args_list[1].args
        assert "INSERT" in str(second_call_args[0])
        assert second_call_args[1]["vector_ids"] == json.dumps(["abc123"])

    def test_rejected_asset_has_null_vector_ids(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "rejected-id"
        engine = _mock_engine(connection)

        record_asset(engine, "hash", "pdf", "rejected", {"status": "rejected"})

        insert_call_args = connection.execute.call_args_list[1].args
        assert insert_call_args[1]["vector_ids"] is None
        assert insert_call_args[1]["chunk_count"] == 0


class TestEnsureSchema:
    def test_executes_idempotent_ddl_without_error(self):
        connection = MagicMock()
        engine = _mock_engine(connection)

        ensure_schema(engine)  # must not raise

        assert connection.execute.call_count == 3  # schema, table, unique index
