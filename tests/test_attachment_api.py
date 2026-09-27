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


@pytest.fixture(autouse=True)
def _reset_api_action_limiters():
    """`api.rate_limit._limiters` is a process-wide singleton dict (the
    `enforce_api_action_rate_limit` budget every attachment route --
    upload, resize, blur-region, ai-edit, ... -- shares) -- reset before/
    after every test so the cumulative call count across this whole file's
    many upload-heavy tests can never trip another test's own limit purely
    by test order/count (same reasoning as `_reset_ip_limiters` above)."""
    import api.rate_limit as api_rate_limit

    api_rate_limit._limiters.clear()
    yield
    api_rate_limit._limiters.clear()


@pytest.fixture(autouse=True)
def _reset_ai_edit_idempotency_cache():
    """`attachments.ai_edit._idempotency_cache` is a process-wide dict --
    reset before/after every test in this whole pytest session (this
    fixture runs for every test in this file, but the cache itself is a
    module-level global other test files could in principle also touch)
    so a fixed literal key like `"retry-key-1"` can never leak between
    test runs."""
    from attachments import ai_edit

    ai_edit._idempotency_cache.clear()
    yield
    ai_edit._idempotency_cache.clear()


@pytest.fixture(autouse=True)
def _reset_media_generation_limiter():
    """Shared with plain media generation (`agent.orchestrator.nodes
    .execute_generation`) -- reset for the same reason as the other
    process-wide limiters reset above."""
    import agent.rate_limit as rate_limit_module

    rate_limit_module._media_generation_limiter = None
    yield
    rate_limit_module._media_generation_limiter = None


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
        assert body["image_blur"] is True
        assert body["native_pdf_input"] is False
        assert any(preset["name"] == "medium_800" for preset in body["resize_presets"])
        assert ".png" in body["supported_image_extensions"]
        assert ".pdf" in body["supported_document_extensions"]
        # Off by default (see Settings.enable_image_editing's own docstring)
        # -- must reflect real backend readiness, never a hardcoded true.
        assert body["image_ai_editing"] is False
        assert body["image_ai_editing_provider"] is None

    def test_reports_ai_editing_available_only_when_flag_and_key_are_both_set(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, enable_image_editing=True, ima_api_key=None),
        )
        response = client.get("/attachments/capabilities")
        assert response.json()["image_ai_editing"] is False

        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, enable_image_editing=True, ima_api_key=SecretStr("k")),
        )
        response = client.get("/attachments/capabilities")
        body = response.json()
        assert body["image_ai_editing"] is True
        assert body["image_ai_editing_provider"] == "ima_studio"


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


def _mask_data_url(width: int, height: int, *, box=None) -> str:
    """A real single-channel PNG mask, base64-encoded as a data URL --
    `box` (left, top, right, bottom) is painted white (editable); the rest
    is left black (preserved). No `box` means an all-black (zero-area)
    mask, for testing the "empty mask" rejection path."""
    mask = Image.new("L", (width, height), 0)
    if box is not None:
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                mask.putpixel((x, y), 255)
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    import base64

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class TestBlurRegion:
    """Deterministic, local Pillow blur -- never a model call, and must
    work regardless of whether AI-guided editing is configured (per this
    feature's own "keep local tools independent" / "local blur should work
    even if AI editing is off" requirement)."""

    def test_blurs_the_masked_region_and_returns_a_new_attachment(
        self, monkeypatch, client, tmp_path
    ):
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=200, height=150)
        mask = _mask_data_url(200, 150, box=(50, 50, 100, 100))

        response = client.post(
            f"/attachments/{attachment_id}/blur-region",
            json={"mask_data_url": mask, "radius": 10},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["operation"] == "blur_region"
        assert body["source_attachment_id"] == attachment_id
        assert body["attachment_id"] != attachment_id
        assert body["width"] == 200
        assert body["height"] == 150
        assert body["image_data_url"].startswith("data:image/")

    def test_works_even_when_ai_editing_is_disabled(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, enable_image_editing=False, ima_api_key=None),
        )
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=100, height=100)
        mask = _mask_data_url(100, 100, box=(10, 10, 40, 40))
        response = client.post(
            f"/attachments/{attachment_id}/blur-region", json={"mask_data_url": mask}
        )
        assert response.status_code == 200

    def test_rejects_a_zero_area_mask_with_an_actionable_message(
        self, monkeypatch, client, tmp_path
    ):
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=100, height=100)
        empty_mask = _mask_data_url(100, 100, box=None)
        response = client.post(
            f"/attachments/{attachment_id}/blur-region", json={"mask_data_url": empty_mask}
        )
        assert response.status_code == 400
        assert "too small" in response.json()["detail"].lower()

    def test_rejects_a_malformed_mask_data_url(self, monkeypatch, client, tmp_path):
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(
            f"/attachments/{attachment_id}/blur-region", json={"mask_data_url": "not-a-data-url"}
        )
        assert response.status_code == 400

    def test_404_for_unknown_attachment(self, client):
        mask = _mask_data_url(10, 10, box=(0, 0, 5, 5))
        response = client.post(
            "/attachments/does-not-exist/blur-region", json={"mask_data_url": mask}
        )
        assert response.status_code == 404


class TestAiEdit:
    """Real, generative AI-guided image editing -- the endpoint the
    frontend's "AI-guided editing" panel used to have no backend for at
    all. `attachments.ai_edit.get_image_edit_provider` is monkeypatched to
    a `FakeImageEditProvider` for every test here except the explicit
    "not configured" ones -- no real network/IMA call is ever made."""

    def _install_fake_provider(self, monkeypatch, **kwargs):
        from media_gen.image_edit_provider import FakeImageEditProvider

        fake = FakeImageEditProvider(**kwargs)
        monkeypatch.setattr("attachments.ai_edit.get_image_edit_provider", lambda settings: fake)
        return fake

    def _settings_with_editing_enabled(self, tmp_path, **overrides):
        return _settings(
            tmp_path, enable_image_editing=True, ima_api_key=SecretStr("fake-key"), **overrides
        )

    def test_not_configured_returns_a_clean_failed_status_not_a_500(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr(
            "api.attachments.get_settings",
            lambda: _settings(tmp_path, enable_image_editing=False),
        )
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={"operation": "remove_object", "prompt": "remove the person"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_code"] == "not_configured"

    def test_successful_edit_returns_a_new_attachment_and_never_mutates_the_source(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        fake = self._install_fake_provider(monkeypatch)
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=64, height=64)

        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={"operation": "remove_object", "prompt": "remove the selected object"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["source_attachment_id"] == attachment_id
        assert body["attachment_id"] != attachment_id
        assert body["image_data_url"].startswith("data:image/")
        assert body["provider"] == "fake"
        assert body["mask_provided"] is False
        # The fake provider actually received real image bytes, not a
        # filename/placeholder -- see FakeImageEditProvider's own docstring
        # for why this is exactly what this feature's own test requirement
        # asks for.
        assert len(fake.requests) == 1
        assert fake.requests[0].image_bytes.startswith(b"\x89PNG")
        assert fake.requests[0].prompt == "remove the selected object"

    def test_mask_bytes_are_passed_to_the_provider_not_just_a_flag(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        fake = self._install_fake_provider(monkeypatch)
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=100, height=100)
        mask = _mask_data_url(100, 100, box=(20, 20, 60, 60))

        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={
                "operation": "replace_background",
                "prompt": "make the background blue",
                "mask_data_url": mask,
            },
        )
        assert response.status_code == 200
        assert response.json()["mask_provided"] is True
        assert fake.requests[0].mask_bytes is not None
        assert len(fake.requests[0].mask_bytes) > 0

    def test_rejects_a_zero_area_mask_before_ever_calling_the_provider(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        fake = self._install_fake_provider(monkeypatch)
        attachment_id = _upload_image(client, monkeypatch, tmp_path, width=50, height=50)
        empty_mask = _mask_data_url(50, 50, box=None)

        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={
                "operation": "remove_object",
                "prompt": "remove it",
                "mask_data_url": empty_mask,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_code"] == "empty_mask"
        assert fake.requests == []  # never reached the (paid) provider call

    def test_rejects_unsupported_operation_at_the_schema_layer(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={"operation": "remove_watermark", "prompt": "take out the watermark"},
        )
        # Not in the Literal allowlist at all -- a 422 from Pydantic, before
        # this ever reaches attachments.ai_edit's own allowlist check. See
        # media_gen.image_edit_provider's own module docstring for why
        # "remove watermark" is deliberately never offered as an operation.
        assert response.status_code == 422

    def test_content_policy_rejection_never_reaches_the_provider(
        self, monkeypatch, client, tmp_path
    ):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        fake = self._install_fake_provider(monkeypatch)
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={"operation": "remove_object", "prompt": "make it nsfw"},
        )
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_code"] == "content_policy_rejected"
        assert fake.requests == []

    def test_provider_failure_surfaces_as_a_clean_failed_status(
        self, monkeypatch, client, tmp_path
    ):
        from media_gen.image_edit_provider import ImageEditProviderError

        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        self._install_fake_provider(
            monkeypatch,
            error=ImageEditProviderError("raw provider detail", safe_message="Try again later."),
        )
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        response = client.post(
            f"/attachments/{attachment_id}/ai-edit",
            json={"operation": "remove_object", "prompt": "remove it"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_message"] == "Try again later."
        assert "raw provider detail" not in body["error_message"]

    def test_idempotency_key_prevents_a_second_provider_call(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        fake = self._install_fake_provider(monkeypatch)
        attachment_id = _upload_image(client, monkeypatch, tmp_path)
        payload = {
            "operation": "remove_object",
            "prompt": "remove it",
            "idempotency_key": "retry-key-1",
        }
        first = client.post(f"/attachments/{attachment_id}/ai-edit", json=payload)
        second = client.post(f"/attachments/{attachment_id}/ai-edit", json=payload)
        assert first.status_code == 200 and second.status_code == 200
        assert len(fake.requests) == 1  # the second call reused the cached outcome

    def test_cannot_edit_another_owners_attachment(
        self, monkeypatch, client, tmp_path, _fresh_attachment_store
    ):
        from attachments.models import Attachment

        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        self._install_fake_provider(monkeypatch)
        image_bytes = _make_png_bytes()
        path = tmp_path / "other_owner.png"
        path.write_bytes(image_bytes)
        attachment = Attachment(
            original_filename="secret.png",
            safe_filename="secret.png",
            media_type="image/png",
            extension=".png",
            size_bytes=len(image_bytes),
            sha256="deadbeef2",
            local_path=str(path),
            owner_subject="someone-else",
            processing_status="succeeded",
        )
        _fresh_attachment_store.put(attachment)

        response = client.post(
            f"/attachments/{attachment.attachment_id}/ai-edit",
            json={"operation": "remove_object", "prompt": "remove it"},
        )
        assert response.status_code == 404

    def test_404_for_unknown_attachment(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(
            "api.attachments.get_settings", lambda: self._settings_with_editing_enabled(tmp_path)
        )
        response = client.post(
            "/attachments/does-not-exist/ai-edit",
            json={"operation": "remove_object", "prompt": "remove it"},
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
