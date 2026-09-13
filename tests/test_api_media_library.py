"""Unit tests for GET /media/library/{media_id} (api/media_library.py) --
the persistent-library counterpart to `tests/test_api_media.py`'s ephemeral
generated-media route. `media.store.get_image_metadata`/`get_segment_metadata`
are mocked at `api.media_library` (where they're looked up from); real
temp files are used so the path-resolution/streaming logic is exercised
for real, matching this project's "fully mocked but not fake-shaped"
testing style (see `tests/test_voice_stt.py`'s own docstring).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config.settings import Settings
from security.secrets import SecretStr


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base = dict(
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
        chroma_collection_name="schema_ddl",
        embedding_model_name="all-MiniLM-L6-v2",
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
        enable_media_search=True,
        media_library_path=tmp_path,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestMediaLibraryDisabled:
    def test_404s_when_disabled(self, monkeypatch, client, tmp_path):
        settings = _settings(tmp_path, enable_media_search=False)
        monkeypatch.setattr("api.media_library.get_settings", lambda: settings)

        response = client.get("/media/library/anything")

        assert response.status_code == 404


class TestGetLibraryMedia:
    def test_serves_an_indexed_image(self, monkeypatch, client, tmp_path):
        image_path = tmp_path / "photo.jpg"
        image_path.write_bytes(b"\xff\xd8\xff fake jpeg bytes")
        settings = _settings(tmp_path)
        monkeypatch.setattr("api.media_library.get_settings", lambda: settings)
        monkeypatch.setattr(
            "api.media_library.get_image_metadata",
            lambda media_id, s: {"source_path": str(image_path)},
        )
        monkeypatch.setattr("api.media_library.get_segment_metadata", lambda media_id, s: None)

        response = client.get("/media/library/abc123")

        assert response.status_code == 200
        assert response.content == b"\xff\xd8\xff fake jpeg bytes"
        assert response.headers["content-type"] == "image/jpeg"

    def test_serves_a_video_segment_thumbnail(self, monkeypatch, client, tmp_path):
        # api/media_library.py imports THUMBNAIL_DIR via `from media.keyframes
        # import THUMBNAIL_DIR` -- a local binding, not a live reference to
        # the module attribute -- so it must be patched on api.media_library
        # itself, not on media.keyframes.
        monkeypatch.setattr("api.media_library.THUMBNAIL_DIR", tmp_path)
        thumb_path = tmp_path / "seg1.jpg"
        thumb_path.write_bytes(b"thumbnail bytes")
        settings = _settings(tmp_path)
        monkeypatch.setattr("api.media_library.get_settings", lambda: settings)
        monkeypatch.setattr("api.media_library.get_image_metadata", lambda media_id, s: None)
        monkeypatch.setattr(
            "api.media_library.get_segment_metadata",
            lambda media_id, s: {"thumbnail_path": str(thumb_path)},
        )

        response = client.get("/media/library/seg1")

        assert response.status_code == 200
        assert response.content == b"thumbnail bytes"

    def test_unknown_id_returns_404(self, monkeypatch, client, tmp_path):
        settings = _settings(tmp_path)
        monkeypatch.setattr("api.media_library.get_settings", lambda: settings)
        monkeypatch.setattr("api.media_library.get_image_metadata", lambda media_id, s: None)
        monkeypatch.setattr("api.media_library.get_segment_metadata", lambda media_id, s: None)

        response = client.get("/media/library/does-not-exist")

        assert response.status_code == 404

    def test_rejects_a_path_outside_the_library_root(self, monkeypatch, client, tmp_path):
        """A stored path that (however it happened) no longer resolves
        inside the configured library root must not be served -- the
        local-path-traversal defense this route exists to enforce."""
        outside_dir = tmp_path.parent / "outside_the_library"
        outside_dir.mkdir(exist_ok=True)
        secret_file = outside_dir / "secret.jpg"
        secret_file.write_bytes(b"should never be served")
        library_root = tmp_path / "library"
        library_root.mkdir()
        settings = _settings(library_root)
        monkeypatch.setattr("api.media_library.get_settings", lambda: settings)
        monkeypatch.setattr(
            "api.media_library.get_image_metadata",
            lambda media_id, s: {"source_path": str(secret_file)},
        )
        monkeypatch.setattr("api.media_library.get_segment_metadata", lambda media_id, s: None)

        response = client.get("/media/library/abc123")

        assert response.status_code == 404
