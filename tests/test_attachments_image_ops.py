"""Unit tests for attachments/image_ops.py -- deterministic (Pillow-only,
no model, no OCR) image resizing. Fully verifiable in this environment (no
external binary dependency, unlike OCR/vision) -- every assertion here
checks real, decoded output dimensions/format, not a mock.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from attachments.image_ops import resize_image
from attachments.image_processing import ImageDecodeError
from config.settings import Settings
from security.secrets import SecretStr


def _settings(**overrides) -> Settings:
    base = Settings(
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
        chroma_persist_dir="/tmp/chroma",
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
    return Settings(**{**base.__dict__, **overrides})


def _make_png_bytes(width: int, height: int, color=(255, 0, 0)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _make_jpeg_with_exif_orientation(width: int, height: int, orientation: int) -> bytes:
    """A JPEG tagged with an EXIF `Orientation` value -- for verifying
    `resize_image` corrects rotation before computing target dimensions
    (orientation 6 = "rotate 90 CW to display correctly", i.e. the stored
    pixel grid is actually `height x width` relative to how it should
    display)."""
    image = Image.new("RGB", (width, height), (0, 128, 255))
    exif = Image.Exif()
    exif[0x0112] = orientation  # 0x0112 == the Orientation tag
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class TestResizeImage:
    def test_width_only_preserves_aspect_ratio(self):
        source = _make_png_bytes(800, 400)
        result = resize_image(source, _settings(), width=400)
        assert result.width == 400
        assert result.height == 200
        # Round-trips through Pillow to confirm the bytes are real, decodable
        # output at the claimed dimensions -- not just a self-reported number.
        decoded = Image.open(io.BytesIO(result.image_bytes))
        assert decoded.size == (400, 200)

    def test_height_only_preserves_aspect_ratio(self):
        source = _make_png_bytes(800, 400)
        result = resize_image(source, _settings(), height=100)
        assert result.width == 200
        assert result.height == 100

    def test_contain_fit_keeps_entire_image_within_box(self):
        source = _make_png_bytes(800, 400)  # 2:1 aspect
        result = resize_image(source, _settings(), width=300, height=300, fit="contain")
        # Contain never exceeds the box on either axis, and preserves aspect.
        assert result.width <= 300
        assert result.height <= 300
        assert result.width == 300
        assert result.height == 150

    def test_cover_fit_fills_the_exact_box(self):
        source = _make_png_bytes(800, 400)
        result = resize_image(source, _settings(), width=300, height=300, fit="cover")
        assert (result.width, result.height) == (300, 300)
        decoded = Image.open(io.BytesIO(result.image_bytes))
        assert decoded.size == (300, 300)

    def test_stretch_fit_ignores_aspect_ratio(self):
        source = _make_png_bytes(800, 400)
        result = resize_image(source, _settings(), width=100, height=100, fit="stretch")
        assert (result.width, result.height) == (100, 100)

    def test_original_dimensions_are_reported(self):
        source = _make_png_bytes(800, 400)
        result = resize_image(source, _settings(), width=400)
        assert result.original_width == 800
        assert result.original_height == 400
        assert result.original_size_bytes == len(source)

    def test_output_format_conversion(self):
        source = _make_png_bytes(200, 200)
        result = resize_image(source, _settings(), width=100, output_format="jpeg")
        assert result.media_type == "image/jpeg"
        assert result.output_format == "jpeg"
        decoded = Image.open(io.BytesIO(result.image_bytes))
        assert decoded.format == "JPEG"

    def test_keeps_source_format_when_not_specified(self):
        source = _make_png_bytes(200, 200)
        result = resize_image(source, _settings(), width=100)
        assert result.media_type == "image/png"

    def test_exif_orientation_is_applied_before_resizing(self):
        # Stored pixel grid is 300 wide x 600 tall; orientation 6 means it
        # should *display* as 600 wide x 300 tall -- resize_image must apply
        # that rotation before computing "original" dimensions, or a
        # width-only resize request would come out sideways.
        source = _make_jpeg_with_exif_orientation(300, 600, orientation=6)
        result = resize_image(source, _settings(), width=200)
        assert result.original_width == 600
        assert result.original_height == 300
        assert result.width == 200
        assert result.height == 100

    def test_rejects_when_no_dimension_given(self):
        source = _make_png_bytes(200, 200)
        with pytest.raises(ValueError, match="width or height"):
            resize_image(source, _settings())

    def test_rejects_dimension_exceeding_configured_cap(self):
        source = _make_png_bytes(200, 200)
        with pytest.raises(ValueError, match="may not exceed"):
            resize_image(source, _settings(max_attachment_resize_dimension_px=500), width=1000)

    def test_rejects_non_positive_dimension(self):
        source = _make_png_bytes(200, 200)
        with pytest.raises(ValueError, match="positive"):
            resize_image(source, _settings(), width=0)

    def test_corrupt_bytes_raise_image_decode_error(self):
        with pytest.raises(ImageDecodeError):
            resize_image(b"not an image", _settings(), width=100)

    def test_rgba_source_flattened_correctly_for_jpeg_output(self):
        image = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        result = resize_image(buffer.getvalue(), _settings(), width=50, output_format="jpeg")
        decoded = Image.open(io.BytesIO(result.image_bytes))
        assert decoded.mode == "RGB"
        assert decoded.size == (50, 50)
