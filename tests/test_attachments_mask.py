"""Unit tests for attachments/mask.py -- the shared canonical mask decode/
validate/align logic used by both the deterministic local blur and the
generative AI-guided edit.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from attachments.image_processing import ImageDecodeError
from attachments.mask import MaskValidationError, decode_and_validate_mask, mask_array_to_png_bytes


def _grayscale_mask_png(size: tuple[int, int], box: tuple[int, int, int, int] | None) -> bytes:
    mask = Image.new("L", size, 0)
    if box is not None:
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                mask.putpixel((x, y), 255)
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    return buffer.getvalue()


def _rgba_stroke_mask_png(size: tuple[int, int], box: tuple[int, int, int, int]) -> bytes:
    """Mirrors what a Konva canvas mask layer actually exports -- a colored
    stroke on an otherwise fully-transparent RGBA layer."""
    mask = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            mask.putpixel((x, y), (239, 68, 68, 200))  # a red-ish stroke color, alpha=200
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    return buffer.getvalue()


class TestDecodeAndValidateMask:
    def test_decodes_a_flat_grayscale_mask_at_matching_dimensions(self):
        mask_bytes = _grayscale_mask_png((40, 40), box=(10, 10, 30, 30))
        decoded = decode_and_validate_mask(mask_bytes, target_width=40, target_height=40)
        assert decoded.width == 40 and decoded.height == 40
        assert decoded.array.shape == (40, 40)
        assert decoded.array[20, 20] == 255  # inside the painted box
        assert decoded.array[2, 2] == 0  # outside

    def test_decodes_an_rgba_stroke_mask_using_visibility_and_luminance(self):
        mask_bytes = _rgba_stroke_mask_png((40, 40), box=(10, 10, 30, 30))
        decoded = decode_and_validate_mask(mask_bytes, target_width=40, target_height=40)
        assert decoded.array[20, 20] > 0  # a visible, non-black stroke pixel counts as editable
        assert decoded.array[2, 2] == 0  # fully transparent -- not editable

    def test_resizes_a_mismatched_mask_to_the_source_dimensions(self):
        # A mask exported at a different resolution than the source photo --
        # must not be silently rejected, just aligned.
        mask_bytes = _grayscale_mask_png((20, 20), box=(5, 5, 15, 15))
        decoded = decode_and_validate_mask(mask_bytes, target_width=100, target_height=100)
        assert decoded.width == 100 and decoded.height == 100
        assert decoded.array.shape == (100, 100)
        # The painted region (originally 5-15 of 20, i.e. 25%-75%) should
        # land roughly in the same relative region after nearest-neighbor
        # resize to 100x100 (25-75).
        assert decoded.array[50, 50] == 255

    def test_zero_area_mask_raises_mask_validation_error(self):
        mask_bytes = _grayscale_mask_png((40, 40), box=None)
        with pytest.raises(MaskValidationError, match="too small"):
            decode_and_validate_mask(mask_bytes, target_width=40, target_height=40)

    def test_a_single_stray_pixel_below_the_fraction_threshold_still_raises(self):
        mask = Image.new("L", (200, 200), 0)
        mask.putpixel((5, 5), 255)
        buffer = io.BytesIO()
        mask.save(buffer, format="PNG")
        with pytest.raises(MaskValidationError):
            decode_and_validate_mask(buffer.getvalue(), target_width=200, target_height=200)

    def test_corrupt_bytes_raise_image_decode_error(self):
        with pytest.raises(ImageDecodeError):
            decode_and_validate_mask(b"not a real png", target_width=10, target_height=10)

    def test_custom_min_editable_fraction_is_respected(self):
        # A tiny mask that would fail the default threshold should pass a
        # deliberately lowered one -- proves the parameter is real, not
        # decorative.
        mask = Image.new("L", (100, 100), 0)
        for y in range(0, 2):
            for x in range(0, 2):
                mask.putpixel((x, y), 255)
        buffer = io.BytesIO()
        mask.save(buffer, format="PNG")
        decoded = decode_and_validate_mask(
            buffer.getvalue(), target_width=100, target_height=100, min_editable_fraction=0.0
        )
        assert decoded.editable_pixel_count == 4


class TestMaskArrayToPngBytes:
    def test_round_trips_through_a_real_decoder(self):
        array = np.zeros((10, 10), dtype=np.uint8)
        array[2:5, 2:5] = 255
        png_bytes = mask_array_to_png_bytes(array)
        with Image.open(io.BytesIO(png_bytes)) as decoded:
            decoded.verify()
        with Image.open(io.BytesIO(png_bytes)) as decoded:
            assert decoded.convert("L").getpixel((3, 3)) == 255
            assert decoded.convert("L").getpixel((0, 0)) == 0
