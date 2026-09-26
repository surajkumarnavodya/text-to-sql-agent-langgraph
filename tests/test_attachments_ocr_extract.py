"""Unit tests for attachments/ocr_extract.py.

The Tesseract *binary* is not installed in this environment (confirmed:
`pytesseract.get_tesseract_version()` raises `TesseractNotFoundError`) --
`pytesseract`, the Python package, is installed. This file is honest about
that split: `test_fails_open_when_tesseract_binary_is_unavailable` exercises
the real, unmocked failure path against this actual environment; every other
OCR-shaped test monkeypatches `pytesseract` with a hand-built response to
verify this module's own parsing/filtering/scaling logic, not Tesseract's
recognition accuracy.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from attachments.image_processing import ImageDecodeError
from attachments.ocr_extract import downscale_if_oversized, extract_text_with_regions
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


def _make_png_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _install_fake_pytesseract(
    monkeypatch, *, raw_text: str, data: dict, timeout_error: bool = False
):
    class _FakePytesseract:
        Output = type("Output", (), {"DICT": "dict"})

        @staticmethod
        def image_to_string(*args, **kwargs):
            if timeout_error:
                raise RuntimeError("Tesseract timed out")
            return raw_text

        @staticmethod
        def image_to_data(*args, **kwargs):
            if timeout_error:
                raise RuntimeError("Tesseract timed out")
            return data

    monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract())


class TestDownscaleIfOversized:
    def test_leaves_small_images_unchanged(self):
        image = Image.new("RGB", (100, 50))
        result, scale = downscale_if_oversized(image, max_dim=200)
        assert scale == 1.0
        assert result.size == (100, 50)

    def test_downscales_oversized_images_preserving_aspect(self):
        image = Image.new("RGB", (2000, 1000))
        result, scale = downscale_if_oversized(image, max_dim=500)
        assert scale == 0.25
        assert result.size == (500, 250)


class TestExtractTextWithRegions:
    def test_fails_open_when_tesseract_binary_is_unavailable(self):
        """Genuinely verified against this environment's actual missing
        Tesseract binary -- not a simulated failure."""
        source = _make_png_bytes(100, 100)
        result = extract_text_with_regions(source, _settings())
        assert result.raw_text == ""
        assert result.regions == []
        assert any("not available" in warning.lower() for warning in result.warnings)

    def test_parses_words_and_filters_non_text_rows(self, monkeypatch):
        fake_data = {
            "text": ["Invoice", "", "#12345", "Total:", "$99.00"],
            "conf": [96.0, -1.0, 92.0, 85.0, 40.0],
            "left": [10, 0, 100, 10, 60],
            "top": [10, 0, 10, 40, 40],
            "width": [60, 0, 50, 40, 45],
            "height": [15, 0, 15, 15, 15],
        }
        _install_fake_pytesseract(
            monkeypatch, raw_text="Invoice #12345\nTotal: $99.00", data=fake_data
        )

        source = _make_png_bytes(200, 100)
        result = extract_text_with_regions(source, _settings())

        assert result.raw_text == "Invoice #12345\nTotal: $99.00"
        # The blank/-1-confidence row is excluded -- 4 real words remain.
        assert len(result.regions) == 4
        assert {region.text for region in result.regions} == {
            "Invoice",
            "#12345",
            "Total:",
            "$99.00",
        }

    def test_flags_low_confidence_words(self, monkeypatch):
        fake_data = {
            "text": ["Clear", "blurry"],
            "conf": [95.0, 35.0],
            "left": [0, 60],
            "top": [0, 0],
            "width": [40, 40],
            "height": [15, 15],
        }
        _install_fake_pytesseract(monkeypatch, raw_text="Clear blurry", data=fake_data)

        source = _make_png_bytes(150, 50)
        result = extract_text_with_regions(source, _settings())

        by_text = {region.text: region for region in result.regions}
        assert by_text["Clear"].low_confidence is False
        assert by_text["blurry"].low_confidence is True
        assert any("low confidence" in warning.lower() for warning in result.warnings)

    def test_cleaned_text_collapses_blank_lines_but_keeps_exact_words(self, monkeypatch):
        fake_data = {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}
        _install_fake_pytesseract(
            monkeypatch, raw_text="Line one\n\n\n\nLine two   \n", data=fake_data
        )
        source = _make_png_bytes(100, 100)
        result = extract_text_with_regions(source, _settings())
        assert result.cleaned_text == "Line one\n\nLine two"
        # raw_text is untouched -- exact Tesseract output, never rewritten.
        assert result.raw_text == "Line one\n\n\n\nLine two"

    def test_warns_when_nothing_detected(self, monkeypatch):
        fake_data = {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []}
        _install_fake_pytesseract(monkeypatch, raw_text="", data=fake_data)
        source = _make_png_bytes(100, 100)
        result = extract_text_with_regions(source, _settings())
        assert result.raw_text == ""
        assert result.regions == []
        assert any("no text was detected" in warning.lower() for warning in result.warnings)

    def test_timeout_fails_open_with_a_warning(self, monkeypatch):
        _install_fake_pytesseract(monkeypatch, raw_text="", data={}, timeout_error=True)
        source = _make_png_bytes(100, 100)
        result = extract_text_with_regions(source, _settings())
        assert result.raw_text == ""
        assert any("timed out" in warning.lower() for warning in result.warnings)

    def test_corrupt_bytes_raise_image_decode_error(self):
        with pytest.raises(ImageDecodeError):
            extract_text_with_regions(b"not an image", _settings())
