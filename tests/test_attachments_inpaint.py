"""Unit tests for attachments/inpaint.py.

`remove_text` (real OpenCV `cv2.inpaint`) is fully verifiable in this
environment -- it takes explicit pixel regions, no OCR/Tesseract binary
involved, so every assertion below checks real, decoded pixel output.

`detect_text_line_regions` requires the Tesseract *binary* (not just the
`pytesseract` Python package, which is installed) -- this machine does not
have it (confirmed: `pytesseract.get_tesseract_version()` raises
`TesseractNotFoundError`). Its tests here therefore split honestly into two
kinds: (1) the real fail-open behavior against the actual missing binary
(genuinely verified against this environment), and (2) the line-merging/
coordinate-scaling logic, verified against a hand-built, realistic
`pytesseract.image_to_data`-shaped response via monkeypatch -- this proves
this module's own parsing code is correct, not that Tesseract's OCR
recognition itself was exercised.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from attachments.image_processing import ImageDecodeError
from attachments.inpaint import InpaintRegion, detect_text_line_regions, remove_text
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


def _make_textured_png_bytes(width: int, height: int) -> bytes:
    """A gradient/noise background, deliberately not a flat color -- real
    inpainting reconstructs varying nearby pixels, so a flat source image
    couldn't distinguish "genuine inpainting happened" from "a solid
    rectangle was pasted," which is exactly the failure mode this feature
    must not have (see attachments.inpaint's own module docstring)."""
    rng = np.random.default_rng(42)
    array = np.zeros((height, width, 3), dtype=np.uint8)
    for x in range(width):
        array[:, x, 0] = int(255 * x / max(1, width - 1))
    array[:, :, 1] = 128
    array[:, :, 2] = rng.integers(100, 200, size=(height, width), dtype=np.uint8)
    return_bytes = io.BytesIO()
    Image.fromarray(array).save(return_bytes, format="PNG")
    return return_bytes.getvalue()


class TestRemoveText:
    def test_edits_real_pixels_within_the_region(self):
        source = _make_textured_png_bytes(200, 200)
        original = np.array(Image.open(io.BytesIO(source)).convert("RGB"))

        result = remove_text(
            source, _settings(), regions=[InpaintRegion(left=50, top=50, width=60, height=30)]
        )

        edited = np.array(Image.open(io.BytesIO(result.image_bytes)).convert("RGB"))
        assert edited.shape == original.shape
        region_before = original[50:80, 50:110]
        region_after = edited[50:80, 50:110]
        # A real edit happened -- the masked region's pixels changed.
        assert not np.array_equal(region_before, region_after)

    def test_output_is_not_a_flat_rectangle_fill(self):
        """The core "genuine inpainting, not a solid-color rectangle"
        assertion: on a textured background, a real inpaint reconstructs
        *varying* pixel values (blended from the surrounding gradient), so
        the edited region's own standard deviation should stay well above
        zero -- a flat color fill would collapse it to (near) zero."""
        source = _make_textured_png_bytes(200, 200)
        result = remove_text(
            source, _settings(), regions=[InpaintRegion(left=40, top=40, width=100, height=40)]
        )
        edited = np.array(Image.open(io.BytesIO(result.image_bytes)).convert("RGB"))
        region = edited[40:80, 40:140].astype(np.float64)
        assert region.std() > 5.0

    def test_pixels_well_outside_the_region_are_unchanged(self):
        source = _make_textured_png_bytes(200, 200)
        original = np.array(Image.open(io.BytesIO(source)).convert("RGB"))
        result = remove_text(
            source, _settings(), regions=[InpaintRegion(left=50, top=50, width=20, height=20)]
        )
        edited = np.array(Image.open(io.BytesIO(result.image_bytes)).convert("RGB"))
        # Far corner, nowhere near the masked (and dilated) region.
        assert np.array_equal(original[0:10, 0:10], edited[0:10, 0:10])

    def test_reports_dimensions_and_honest_method_label(self):
        source = _make_textured_png_bytes(150, 100)
        result = remove_text(
            source, _settings(), regions=[InpaintRegion(left=10, top=10, width=30, height=20)]
        )
        assert (result.width, result.height) == (150, 100)
        assert result.method == "opencv_telea_inpaint"
        assert result.regions_removed == 1
        assert any(
            "not a generative AI model" in warning.lower() or "classical" in warning.lower()
            for warning in result.warnings
        )

    def test_rejects_empty_region_list(self):
        source = _make_textured_png_bytes(100, 100)
        with pytest.raises(ValueError, match="At least one region"):
            remove_text(source, _settings(), regions=[])

    def test_rejects_too_many_regions(self):
        source = _make_textured_png_bytes(100, 100)
        regions = [InpaintRegion(left=i, top=i, width=5, height=5) for i in range(5)]
        with pytest.raises(ValueError, match="At most"):
            remove_text(source, _settings(max_text_removal_regions=3), regions=regions)

    def test_corrupt_bytes_raise_image_decode_error(self):
        with pytest.raises(ImageDecodeError):
            remove_text(
                b"not an image",
                _settings(),
                regions=[InpaintRegion(left=0, top=0, width=10, height=10)],
            )

    def test_region_clipped_to_image_bounds(self):
        # A region overlapping the image edge must not crash -- it should
        # be silently clipped rather than raising an out-of-bounds error.
        source = _make_textured_png_bytes(100, 100)
        result = remove_text(
            source, _settings(), regions=[InpaintRegion(left=90, top=90, width=50, height=50)]
        )
        assert (result.width, result.height) == (100, 100)


class TestDetectTextLineRegions:
    def test_fails_open_when_tesseract_binary_is_unavailable(self):
        """Genuinely verified against this environment: the Tesseract
        binary is not installed here, so this exercises the real
        `pytesseract` failure path, not a simulated one."""
        source = _make_textured_png_bytes(100, 100)
        regions = detect_text_line_regions(source, _settings())
        assert regions == []

    def test_merges_word_level_boxes_into_line_regions(self, monkeypatch):
        """Verifies this module's own line-merging/coordinate-scaling logic
        against a hand-built, realistic `pytesseract.image_to_data` response
        -- NOT a real OCR recognition (that would need the Tesseract binary,
        unavailable here; see this file's module docstring)."""
        import attachments.inpaint as inpaint_module

        fake_data = {
            "text": ["Hello", "World", "", "Second", "line"],
            "conf": [95.0, 88.0, -1.0, 91.0, 60.0],
            "left": [10, 60, 0, 10, 55],
            "top": [10, 12, 0, 40, 41],
            "width": [40, 45, 0, 42, 30],
            "height": [15, 14, 0, 16, 15],
            "block_num": [1, 1, 1, 1, 1],
            "par_num": [1, 1, 1, 1, 1],
            "line_num": [1, 1, 1, 2, 2],
        }

        class _FakePytesseract:
            Output = type("Output", (), {"DICT": "dict"})

            @staticmethod
            def image_to_data(*args, **kwargs):
                return fake_data

        monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract())

        source = _make_textured_png_bytes(200, 100)
        regions = inpaint_module.detect_text_line_regions(source, _settings())

        assert len(regions) == 2
        line_texts = {region.text for region in regions}
        assert line_texts == {"Hello World", "Second line"}
        first_line = next(region for region in regions if region.text == "Hello World")
        assert first_line.left == 10
        assert first_line.top == 10
        assert first_line.width == (60 + 45) - 10  # union of both word boxes
        assert first_line.height == max(10 + 15, 12 + 14) - min(10, 12)

    def test_regions_capped_and_sorted_by_area(self, monkeypatch):
        import attachments.inpaint as inpaint_module

        fake_data = {
            "text": [f"word{i}" for i in range(5)],
            "conf": [90.0] * 5,
            "left": [i * 20 for i in range(5)],
            "top": [0] * 5,
            "width": [5, 50, 10, 40, 15],
            "height": [5, 50, 10, 40, 15],
            "block_num": [1] * 5,
            "par_num": [1] * 5,
            "line_num": list(range(5)),
        }

        class _FakePytesseract:
            Output = type("Output", (), {"DICT": "dict"})

            @staticmethod
            def image_to_data(*args, **kwargs):
                return fake_data

        monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract())

        source = _make_textured_png_bytes(200, 100)
        regions = inpaint_module.detect_text_line_regions(
            source, _settings(max_text_removal_regions=2)
        )

        assert len(regions) == 2
        # Largest areas first (50x50=2500, then 40x40=1600).
        assert regions[0].width * regions[0].height >= regions[1].width * regions[1].height
