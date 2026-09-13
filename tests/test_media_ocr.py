"""Unit tests for media/ocr.py -- Tesseract OCR, fail-open on any error."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from media.ocr import extract_text


class TestExtractText:
    def test_returns_extracted_text_on_success(self, monkeypatch, tmp_path: Path):
        import pytesseract

        image_path = tmp_path / "sign.png"
        Image.new("RGB", (10, 10)).save(image_path)
        monkeypatch.setattr(pytesseract, "image_to_string", lambda image: "DANGER: HIGH VOLTAGE\n")

        assert extract_text(image_path) == "DANGER: HIGH VOLTAGE"

    def test_fails_open_to_empty_string_on_missing_tesseract_binary(
        self, monkeypatch, tmp_path: Path
    ):
        import pytesseract

        image_path = tmp_path / "sign.png"
        Image.new("RGB", (10, 10)).save(image_path)

        def _raise(image):
            raise pytesseract.TesseractNotFoundError()

        monkeypatch.setattr(pytesseract, "image_to_string", _raise)

        assert extract_text(image_path) == ""

    def test_fails_open_to_empty_string_on_a_corrupt_frame(self, tmp_path: Path):
        corrupt_path = tmp_path / "corrupt.jpg"
        corrupt_path.write_bytes(b"not a real image")

        assert extract_text(corrupt_path) == ""
