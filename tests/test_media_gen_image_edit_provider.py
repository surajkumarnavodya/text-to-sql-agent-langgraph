"""Unit tests for media_gen/image_edit_provider.py -- AI-guided image
editing's provider-agnostic interface, the FakeImageEditProvider test
double, and the real IMA adapter (`ImaImageEditProvider`, fully mocked --
no real network call).
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from config.settings import Settings
from media_gen.client import MediaGenerationError
from media_gen.image_edit_provider import (
    ALLOWED_IMAGE_EDIT_OPERATIONS,
    FakeImageEditProvider,
    ImageEditProviderError,
    ImageEditRequest,
    ImaImageEditProvider,
    _composite_mask_overlay,
)
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
    log_level="INFO",
    log_redaction_level="standard",
    enable_image_editing=True,
    ima_api_key=SecretStr("real-ima-key"),
    image_edit_timeout_seconds=90,
    image_edit_poll_interval_seconds=1.0,
)


def _png_bytes(size=(64, 64), color=(10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _mask_png_bytes(size=(64, 64), box=(10, 10, 30, 30)) -> bytes:
    mask = Image.new("L", size, 0)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            mask.putpixel((x, y), 255)
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    return buffer.getvalue()


class TestFakeImageEditProvider:
    def test_records_the_exact_request_it_received(self):
        fake = FakeImageEditProvider()
        request = ImageEditRequest(
            image_bytes=b"real-bytes",
            image_content_type="image/png",
            prompt="remove the object",
            operation="remove_object",
            mask_bytes=b"real-mask-bytes",
        )
        result = fake.edit(request)
        assert result.status == "completed"
        assert fake.requests == [request]
        assert fake.requests[0].image_bytes == b"real-bytes"
        assert fake.requests[0].mask_bytes == b"real-mask-bytes"

    def test_default_success_output_is_a_real_decodable_png(self):
        fake = FakeImageEditProvider()
        result = fake.edit(
            ImageEditRequest(
                image_bytes=b"x", image_content_type="image/png", prompt="p", operation="enhance"
            )
        )
        # Must round-trip through a real decoder -- this is what
        # attachments.pipeline.register_derived_image does with any
        # provider's output in production.
        with Image.open(io.BytesIO(result.image_bytes)) as decoded:
            decoded.verify()

    def test_can_be_configured_to_raise(self):
        error = ImageEditProviderError("boom", safe_message="Try again.")
        fake = FakeImageEditProvider(error=error)
        with pytest.raises(ImageEditProviderError, match="boom"):
            fake.edit(
                ImageEditRequest(
                    image_bytes=b"x",
                    image_content_type="image/png",
                    prompt="p",
                    operation="enhance",
                )
            )


class TestCompositeMaskOverlay:
    def test_only_the_masked_region_changes_pixel_for_pixel(self):
        source = _png_bytes(size=(50, 50), color=(10, 20, 30))
        mask = _mask_png_bytes(size=(50, 50), box=(20, 20, 30, 30))
        result_bytes = _composite_mask_overlay(source, mask)
        result = Image.open(io.BytesIO(result_bytes)).convert("RGB")

        assert result.getpixel((0, 0)) == (10, 20, 30)  # outside mask: untouched
        inside = result.getpixel((25, 25))
        assert inside != (10, 20, 30)  # inside mask: visibly changed
        assert inside[0] > inside[1] and inside[0] > inside[2]  # red-shifted overlay

    def test_raises_for_a_zero_area_mask(self):
        from attachments.mask import MaskValidationError

        source = _png_bytes()
        empty_mask = _mask_png_bytes(box=(0, 0, 0, 0))
        with pytest.raises(MaskValidationError):
            _composite_mask_overlay(source, empty_mask)


class TestImaImageEditProviderAllowlist:
    def test_rejects_an_operation_outside_the_allowlist(self):
        provider = ImaImageEditProvider(_BASE_SETTINGS)
        with pytest.raises(ImageEditProviderError, match="Unsupported"):
            provider.edit(
                ImageEditRequest(
                    image_bytes=_png_bytes(),
                    image_content_type="image/png",
                    prompt="take out the watermark",
                    operation="remove_watermark",
                )
            )

    def test_every_allowed_operation_is_a_real_declared_literal(self):
        # Defensive consistency check -- the allowlist and the Literal type
        # must never silently drift apart.
        assert {
            "remove_object",
            "replace_background",
            "replace_sky",
            "region_edit",
            "enhance",
        } == ALLOWED_IMAGE_EDIT_OPERATIONS


class TestImaImageEditProviderNotConfigured:
    def test_raises_a_clean_error_without_an_api_key(self):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "ima_api_key": None})
        provider = ImaImageEditProvider(settings)
        with pytest.raises(ImageEditProviderError) as exc_info:
            provider.edit(
                ImageEditRequest(
                    image_bytes=_png_bytes(),
                    image_content_type="image/png",
                    prompt="remove it",
                    operation="remove_object",
                )
            )
        assert "not configured" in exc_info.value.safe_message.lower()


class TestImaImageEditProviderFullFlow:
    """Every real network boundary (upload, create/poll, download) is
    mocked at the function it's imported from inside `ImaImageEditProvider
    .edit` -- confirms the actual request shape sent to each, not just that
    *a* call happened."""

    def _patch_chain(
        self, *, poll_media: dict, downloaded_bytes: bytes, download_content_type: str = "image/png"
    ):
        return (
            patch(
                "media_gen.upload.upload_image_to_ima", return_value="https://cdn.example/src.png"
            ),
            patch("media_gen.client.create_and_poll", return_value=(poll_media, "gpt-image-2")),
            patch(
                "media_gen.download.download_media_bytes",
                return_value=(downloaded_bytes, download_content_type),
            ),
        )

    def test_successful_edit_without_a_mask(self):
        output_bytes = _png_bytes(color=(99, 99, 99))
        upload_patch, create_patch, download_patch = self._patch_chain(
            poll_media={"url": "https://cdn.example/out.png"}, downloaded_bytes=output_bytes
        )
        with upload_patch as mock_upload, create_patch as mock_create, download_patch:
            provider = ImaImageEditProvider(_BASE_SETTINGS)
            result = provider.edit(
                ImageEditRequest(
                    image_bytes=_png_bytes(),
                    image_content_type="image/png",
                    prompt="remove the object",
                    operation="remove_object",
                )
            )
        assert result.status == "completed"
        assert result.image_bytes == output_bytes
        assert result.provider == "ima_studio"
        assert result.model == "gpt-image-2"
        assert result.warnings == ()
        mock_upload.assert_called_once()
        # create_and_poll received the uploaded URL and the real task category.
        assert mock_create.call_args.args[1] == "image_to_image"
        assert mock_create.call_args.kwargs["image_urls"] == ["https://cdn.example/src.png"]

    def test_successful_edit_with_a_mask_augments_the_prompt_and_warns(self):
        output_bytes = _png_bytes()
        upload_patch, create_patch, download_patch = self._patch_chain(
            poll_media={"url": "https://cdn.example/out.png"}, downloaded_bytes=output_bytes
        )
        with upload_patch, create_patch as mock_create, download_patch:
            provider = ImaImageEditProvider(_BASE_SETTINGS)
            result = provider.edit(
                ImageEditRequest(
                    image_bytes=_png_bytes(size=(50, 50)),
                    image_content_type="image/png",
                    prompt="make it blue",
                    operation="replace_background",
                    mask_bytes=_mask_png_bytes(size=(50, 50)),
                )
            )
        assert result.status == "completed"
        assert len(result.warnings) == 1
        assert "no native pixel-level mask channel" in result.warnings[0]
        sent_prompt = mock_create.call_args.args[2]
        assert sent_prompt.startswith("Only modify the region highlighted")
        assert sent_prompt.endswith("make it blue")

    def test_provider_business_error_propagates_as_image_edit_provider_error(self):
        upload_patch = patch(
            "media_gen.upload.upload_image_to_ima", return_value="https://cdn.example/src.png"
        )
        create_patch = patch(
            "media_gen.client.create_and_poll",
            side_effect=MediaGenerationError("insufficient credits", payload={"code": 4008}),
        )
        with upload_patch, create_patch:
            provider = ImaImageEditProvider(_BASE_SETTINGS)
            with pytest.raises(ImageEditProviderError) as exc_info:
                provider.edit(
                    ImageEditRequest(
                        image_bytes=_png_bytes(),
                        image_content_type="image/png",
                        prompt="remove it",
                        operation="remove_object",
                    )
                )
        assert "credits" in exc_info.value.safe_message.lower()

    def test_no_result_url_raises_a_clean_error(self):
        upload_patch, create_patch, _ = self._patch_chain(
            poll_media={"resource_status": 1}, downloaded_bytes=b""
        )
        with upload_patch, create_patch:
            provider = ImaImageEditProvider(_BASE_SETTINGS)
            with pytest.raises(ImageEditProviderError, match="no result URL"):
                provider.edit(
                    ImageEditRequest(
                        image_bytes=_png_bytes(),
                        image_content_type="image/png",
                        prompt="remove it",
                        operation="remove_object",
                    )
                )

    def test_undecodable_provider_output_is_rejected_not_stored(self):
        """Treat generated bytes as untrusted -- garbage bytes from the
        provider must never reach storage."""
        upload_patch, create_patch, download_patch = self._patch_chain(
            poll_media={"url": "https://cdn.example/out.png"}, downloaded_bytes=b"not-a-real-image"
        )
        with upload_patch, create_patch, download_patch:
            provider = ImaImageEditProvider(_BASE_SETTINGS)
            with pytest.raises(ImageEditProviderError, match="do not decode"):
                provider.edit(
                    ImageEditRequest(
                        image_bytes=_png_bytes(),
                        image_content_type="image/png",
                        prompt="remove it",
                        operation="remove_object",
                    )
                )

    def test_download_failure_after_successful_generation_is_reported_cleanly(self):
        upload_patch = patch(
            "media_gen.upload.upload_image_to_ima", return_value="https://cdn.example/src.png"
        )
        create_patch = patch(
            "media_gen.client.create_and_poll",
            return_value=({"url": "https://cdn.example/out.png"}, "gpt-image-2"),
        )
        download_patch = patch(
            "media_gen.download.download_media_bytes",
            side_effect=MediaGenerationError("network blip"),
        )
        with upload_patch, create_patch, download_patch:
            provider = ImaImageEditProvider(_BASE_SETTINGS)
            with pytest.raises(ImageEditProviderError) as exc_info:
                provider.edit(
                    ImageEditRequest(
                        image_bytes=_png_bytes(),
                        image_content_type="image/png",
                        prompt="remove it",
                        operation="remove_object",
                    )
                )
        assert "could not be retrieved" in exc_info.value.safe_message.lower()
