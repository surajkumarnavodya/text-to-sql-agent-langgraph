"""Unit tests for POST /attachments/upload, DELETE /attachments/{id}, and
/ask's attachment_ids threading (api/attachments.py, api/main.py).

Follows tests/test_api_ask.py's own conventions exactly: patches
`api.main.get_settings`/`api.attachments.get_settings` and relies on the
shared conftest.py fixtures (env isolation, singleton-cache clearing) for
everything else -- no real DB, Ollama, or Chroma is ever touched.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api.main as api_main
from attachments.store import AttachmentStore
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
)


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, "attachment_storage_dir": tmp_path, **overrides})


@pytest.fixture(autouse=True)
def _reset_ip_limiters():
    api_main._ip_limiters.clear()
    yield
    api_main._ip_limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


@pytest.fixture(autouse=True)
def _fresh_attachment_store(monkeypatch):
    """A fresh AttachmentStore per test -- the process-wide default store
    would otherwise leak attachments (and their FIFO eviction order)
    between tests."""
    store = AttachmentStore()
    monkeypatch.setattr("attachments.pipeline.get_default_attachment_store", lambda: store)
    monkeypatch.setattr("attachments.store.get_default_attachment_store", lambda: store)
    monkeypatch.setattr("api.attachments.get_default_attachment_store", lambda: store)
    return store


class TestUploadAttachments:
    def test_uploads_a_valid_text_file(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
        response = client.post(
            "/attachments/upload",
            files=[("files", ("notes.txt", b"hello world", "text/plain"))],
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["attachments"]) == 1
        assert body["attachments"][0]["filename"] == "notes.txt"
        assert body["attachments"][0]["processing_status"] == "succeeded"
        assert body["errors"] == []

    def test_rejects_unsupported_file_type_without_failing_the_whole_request(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
        response = client.post(
            "/attachments/upload",
            files=[
                ("files", ("notes.txt", b"hello", "text/plain")),
                ("files", ("virus.exe", b"MZ" + b"0" * 20, "application/octet-stream")),
            ],
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["attachments"]) == 1
        assert len(body["errors"]) == 1
        assert body["errors"][0]["code"] == "UNSUPPORTED_FILE_TYPE"

    def test_disabled_feature_flag_returns_404(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, enable_chat_attachments=False),
        )
        response = client.post(
            "/attachments/upload", files=[("files", ("notes.txt", b"hello", "text/plain"))]
        )
        assert response.status_code == 404


class TestDeleteAttachment:
    def test_deletes_an_uploaded_attachment(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
        upload = client.post(
            "/attachments/upload", files=[("files", ("notes.txt", b"hello", "text/plain"))]
        )
        attachment_id = upload.json()["attachments"][0]["attachment_id"]

        delete_response = client.delete(f"/attachments/{attachment_id}")
        assert delete_response.status_code == 204

    def test_deleting_an_unknown_id_returns_404(self, client):
        response = client.delete("/attachments/does-not-exist")
        assert response.status_code == 404


def _make_png_bytes(width: int = 200, height: int = 150, color=(200, 30, 30)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _upload_image(client: TestClient, monkeypatch, tmp_path, **png_kwargs) -> str:
    monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
    response = client.post(
        "/attachments/upload",
        files=[("files", ("photo.png", _make_png_bytes(**png_kwargs), "image/png"))],
    )
    assert response.status_code == 200
    return response.json()["attachments"][0]["attachment_id"]


class TestAttachmentCapabilities:
    def test_returns_capability_flags_and_presets(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: _settings(tmp_path, media_vision_model="llava")
        )
        response = client.get("/attachments/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["vision_input"] is True
        assert body["vision_model"] == "llava"
        assert body["image_resize"] is True
        assert body["native_pdf_input"] is False
        assert any(preset["name"] == "medium_800" for preset in body["resize_presets"])
        assert ".png" in body["supported_image_extensions"]
        assert ".pdf" in body["supported_document_extensions"]


class TestResizeAttachment:
    def test_resizes_and_returns_a_new_attachment(self, monkeypatch, client, tmp_path):
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=400, height=200)

        response = client.post(f"/attachments/{attachment_id}/resize", json={"width": 200})
        assert response.status_code == 200
        body = response.json()
        assert body["operation"] == "resize"
        assert body["width"] == 200
        assert body["height"] == 100
        assert body["original_width"] == 400
        assert body["original_height"] == 200
        assert body["source_attachment_id"] == attachment_id
        # A genuinely new attachment, not the original mutated in place.
        assert body["attachment_id"] != attachment_id
        assert body["image_data_url"].startswith("data:image/")

    def test_rejects_request_with_no_dimension(self, monkeypatch, client, tmp_path):
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(f"/attachments/{attachment_id}/resize", json={})
        assert response.status_code == 400

    def test_rejects_dimension_over_configured_cap(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, max_attachment_resize_dimension_px=100),
        )
        upload = client.post(
            "/attachments/upload",
            files=[("files", ("photo.png", _make_png_bytes(), "image/png"))],
        )
        attachment_id = upload.json()["attachments"][0]["attachment_id"]

        response = client.post(f"/attachments/{attachment_id}/resize", json={"width": 5000})
        assert response.status_code == 400

    def test_404_for_unknown_attachment(self, client):
        response = client.post("/attachments/does-not-exist/resize", json={"width": 100})
        assert response.status_code == 404

    def test_404_for_a_non_image_attachment(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
        upload = client.post(
            "/attachments/upload", files=[("files", ("notes.txt", b"hello", "text/plain"))]
        )
        attachment_id = upload.json()["attachments"][0]["attachment_id"]
        response = client.post(f"/attachments/{attachment_id}/resize", json={"width": 100})
        assert response.status_code == 404

    def test_cannot_resize_another_owners_attachment(
        self, monkeypatch, client, tmp_path, _fresh_attachment_store
    ):
        from attachments.models import Attachment

        monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
        image_bytes = _make_png_bytes()
        path = tmp_path / "other_owner.png"
        path.write_bytes(image_bytes)
        attachment = Attachment(
            original_filename="secret.png",
            safe_filename="secret.png",
            media_type="image/png",
            extension=".png",
            size_bytes=len(image_bytes),
            sha256="deadbeef",
            local_path=str(path),
            owner_subject="someone-else",
            processing_status="succeeded",
        )
        _fresh_attachment_store.put(attachment)

        # This TestClient's requests carry no authenticated identity
        # (auth_mode="none" -> owner_subject=None), which must never match
        # another caller's real owner_subject.
        response = client.post(
            f"/attachments/{attachment.attachment_id}/resize", json={"width": 100}
        )
        assert response.status_code == 404


class TestExtractText:
    def test_returns_ocr_result_shape_even_without_the_tesseract_binary(
        self, monkeypatch, client, tmp_path
    ):
        """Tesseract's binary isn't installed in this test environment (see
        tests/test_attachments_ocr_extract.py's own docstring) -- this
        verifies the route degrades to an honest empty result with a
        warning instead of a 500, which is the real, currently-reachable
        behavior of this deployment."""
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(f"/attachments/{attachment_id}/extract-text")
        assert response.status_code == 200
        body = response.json()
        assert body["operation"] == "extract_text"
        assert body["raw_text"] == ""
        assert any("not available" in warning.lower() for warning in body["warnings"])

    def test_404_for_a_non_image_attachment(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr("api.attachments.get_settings", lambda: _settings(tmp_path))
        upload = client.post(
            "/attachments/upload", files=[("files", ("notes.txt", b"hello", "text/plain"))]
        )
        attachment_id = upload.json()["attachments"][0]["attachment_id"]
        response = client.post(f"/attachments/{attachment_id}/extract-text")
        assert response.status_code == 404


class TestDetectTextRegions:
    def test_returns_empty_regions_without_the_tesseract_binary(
        self, monkeypatch, client, tmp_path
    ):
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.get(f"/attachments/{attachment_id}/detect-text-regions")
        assert response.status_code == 200
        assert response.json()["regions"] == []


class TestRemoveText:
    def test_removes_manually_specified_regions_and_returns_a_new_attachment(
        self, monkeypatch, client, tmp_path
    ):
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=200, height=150)

        response = client.post(
            f"/attachments/{attachment_id}/remove-text",
            json={"regions": [{"left": 20, "top": 20, "width": 50, "height": 20}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["operation"] == "remove_text"
        assert body["source_attachment_id"] == attachment_id
        assert body["attachment_id"] != attachment_id
        assert body["width"] == 200
        assert body["height"] == 150
        assert any(
            "classical" in w.lower() or "not a generative" in w.lower() for w in body["warnings"]
        )

    def test_rejects_empty_region_list(self, monkeypatch, client, tmp_path):
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(f"/attachments/{attachment_id}/remove-text", json={"regions": []})
        assert response.status_code == 422  # min_length=1 on the request schema

    def test_rejects_too_many_regions(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, max_text_removal_regions=2),
        )
        upload = client.post(
            "/attachments/upload",
            files=[("files", ("photo.png", _make_png_bytes(), "image/png"))],
        )
        attachment_id = upload.json()["attachments"][0]["attachment_id"]

        response = client.post(
            f"/attachments/{attachment_id}/remove-text",
            json={
                "regions": [{"left": i * 10, "top": 0, "width": 5, "height": 5} for i in range(5)]
            },
        )
        assert response.status_code == 400

    def test_404_for_unknown_attachment(self, client):
        response = client.post(
            "/attachments/does-not-exist/remove-text",
            json={"regions": [{"left": 0, "top": 0, "width": 10, "height": 10}]},
        )
        assert response.status_code == 404


class TestAskWithAttachmentIds:
    def test_attachment_ids_are_forwarded_to_run_orchestrated(self, monkeypatch, client):
        monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
        captured = {}

        def _capture(question, conversation_history=None, **kwargs):
            captured.update(kwargs)
            return {"status": "succeeded", "error_history": [], "sources_used": ["attachments"]}

        monkeypatch.setattr("api.main.run_orchestrated", _capture)

        response = client.post(
            "/ask",
            json={"question": "what's in this file?", "attachment_ids": ["att_123"]},
        )
        assert response.status_code == 200
        assert captured["attachment_ids"] == ["att_123"]

    def test_missing_attachment_ids_defaults_to_empty_list(self, monkeypatch, client):
        monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
        captured = {}

        def _capture(question, conversation_history=None, **kwargs):
            captured.update(kwargs)
            return {"status": "succeeded", "error_history": []}

        monkeypatch.setattr("api.main.run_orchestrated", _capture)

        response = client.post("/ask", json={"question": "hello"})
        assert response.status_code == 200
        assert captured["attachment_ids"] == []
