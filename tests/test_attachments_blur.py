"""Unit tests for attachments/blur.py -- deterministic, local Pillow region
blur (never a model call). See CLAUDE.md's image-editing design: this exists
specifically so "Blur the selected face" defaults to a free, local filter
instead of the metered generative AI-edit path.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from attachments.blur import blur_region
from attachments.image_processing import ImageDecodeError
from attachments.mask import MaskValidationError


def _checkerboard_png(size=(100, 100), cell=5) -> bytes:
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = (255, 0, 0) if (x // cell + y // cell) % 2 == 0 else (0, 0, 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _mask_png(size=(100, 100), box=(30, 30, 70, 70)) -> bytes:
    mask = Image.new("L", size, 0)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            mask.putpixel((x, y), 255)
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG")
    return buffer.getvalue()


class TestBlurRegion:
    def test_outside_mask_is_byte_identical_to_the_source(self):
        source = _checkerboard_png()
        mask = _mask_png()
        result = blur_region(source, mask, radius=10)

        original = Image.open(io.BytesIO(source)).convert("RGB")
        blurred = Image.open(io.BytesIO(result.image_bytes)).convert("RGB")
        for point in [(0, 0), (5, 90), (95, 5), (99, 99)]:
            assert blurred.getpixel(point) == original.getpixel(point)

    def test_inside_mask_is_visibly_softened(self):
        source = _checkerboard_png()
        mask = _mask_png()
        result = blur_region(source, mask, radius=10)
        original = Image.open(io.BytesIO(source)).convert("RGB")
        blurred = Image.open(io.BytesIO(result.image_bytes)).convert("RGB")
        assert blurred.getpixel((50, 50)) != original.getpixel((50, 50))

    def test_returns_correct_dimensions_and_media_type(self):
        source = _checkerboard_png(size=(120, 80))
        mask = _mask_png(size=(120, 80), box=(10, 10, 40, 40))
        result = blur_region(source, mask, radius=5)
        assert result.width == 120
        assert result.height == 80
        assert result.media_type == "image/png"
        assert result.size_bytes == len(result.image_bytes)

    def test_mismatched_mask_size_is_aligned_not_rejected(self):
        source = _checkerboard_png(size=(100, 100))
        small_mask = _mask_png(size=(50, 50), box=(15, 15, 35, 35))  # different resolution
        result = blur_region(source, small_mask, radius=5)
        assert result.width == 100 and result.height == 100

    def test_corrupt_image_bytes_raise_image_decode_error(self):
        with pytest.raises(ImageDecodeError):
            blur_region(b"not a real image", _mask_png())

    def test_zero_area_mask_raises_mask_validation_error(self):
        source = _checkerboard_png()
        empty_mask = _mask_png(box=(0, 0, 0, 0))
        with pytest.raises(MaskValidationError):
            blur_region(source, empty_mask)

    def test_larger_radius_produces_more_blur(self):
        import numpy as np

        # A coarse-celled checkerboard (unlike the fine 5px one the other
        # tests use): a small radius leaves real local contrast between
        # cells intact, while a large one fully saturates it -- a fine
        # cell size would make both radii converge to the same flattened
        # average, masking any real difference between them.
        source = _checkerboard_png(cell=15)
        mask = _mask_png()
        light = blur_region(source, mask, radius=2)
        heavy = blur_region(source, mask, radius=20)
        light_region = np.array(Image.open(io.BytesIO(light.image_bytes)).convert("RGB"))[
            30:70, 30:70
        ]
        heavy_region = np.array(Image.open(io.BytesIO(heavy.image_bytes)).convert("RGB"))[
            30:70, 30:70
        ]
        # A heavier blur smooths out more of the checkerboard's local
        # contrast -- the masked region's own pixel variance should shrink
        # as radius grows, a more robust signal than any single pixel
        # (a periodic checkerboard's exact center can coincidentally
        # saturate to the same average at more than one radius).
        assert heavy_region.std() < light_region.std()
