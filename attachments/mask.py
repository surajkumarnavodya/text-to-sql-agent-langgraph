"""Shared mask decode/validate/align logic for image-editing operations
that take a user-painted region -- both the deterministic local blur
(`attachments.blur`) and the generative AI-guided edit
(`media_gen.image_edit_provider`) use this, so there is exactly one place
that defines what a "mask" means in this codebase.

**Canonical internal mask convention** (this app's own -- never assumed to
match any particular provider's own polarity/alpha convention): a
single-channel image, resolved to the *same pixel dimensions as the image
it applies to*, where a pixel's intensity (0-255) is how strongly
"editable"/"to be blurred" it is -- 255 (white/fully opaque) = fully
editable, 0 (black/fully transparent) = preserved untouched. The frontend's
mask-drawing layer (a semi-transparent colored stroke on its own Konva
layer) is expected to export roughly this shape; this module is
deliberately tolerant of the two realistic export forms (a flat grayscale
PNG, or an RGBA PNG where only the painted strokes are non-transparent)
rather than assuming one specific canvas-export implementation detail.

See `media_gen.image_edit_provider`'s own docstring for why the one real
generative provider this app integrates (IMA Studio) does *not* receive
this as a native pixel-level mask channel -- it has none -- and instead
gets a baked-in visual overlay plus an explicit instruction. This module
only defines the *internal* representation; provider-specific conversion
is each adapter's own job.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, UnidentifiedImageError

from attachments.image_processing import ImageDecodeError

# Antialiased stroke edges (from freehand mouse/touch drawing) leave a lot
# of very-low-intensity pixels around a paint stroke's boundary -- treating
# every nonzero pixel as "editable" would make masks fatter than what the
# user actually painted look like. This is a "did the user meaningfully
# mark this pixel" threshold, not a security boundary.
_EDITABLE_INTENSITY_THRESHOLD = 32

# Below this fraction of the source image's total pixels, a mask is
# treated as "effectively empty" -- catches both a genuinely zero-area
# mask (the user opened the mask tool but never actually painted anything)
# and a stray single click that produced only a few antialiased pixels,
# neither of which is a usable edit region. See this module's own
# `decode_and_validate_mask` docstring.
_DEFAULT_MIN_EDITABLE_FRACTION = 0.0005


class MaskValidationError(ValueError):
    """A structurally-valid-but-unusable mask (zero/near-zero editable
    area) -- a `ValueError` subclass so it's caught by the same
    `except ValueError` call sites `attachments.image_ops.resize_image`'s
    own contract already establishes, without those call sites needing to
    know a new exception type exists."""


@dataclass(frozen=True)
class DecodedMask:
    """`array` is `uint8`, shape `(height, width)`, values 0-255 -- the
    canonical form every consumer of a decoded mask (blur compositing, the
    AI-edit provider's overlay baking) works with directly."""

    array: np.ndarray
    width: int
    height: int
    editable_pixel_count: int
    editable_fraction: float


def _mask_channel_from_image(opened: Image.Image) -> Image.Image:
    """Reduces a decoded mask image to one "how editable is this pixel"
    grayscale channel, tolerant of the two realistic export shapes (see
    this module's own docstring)."""
    if opened.mode in ("RGBA", "LA"):
        alpha = opened.split()[-1]
        luminance = opened.convert("L")
        # A pixel counts as editable only where it's both visible
        # (alpha > 0) AND not pure black -- matches a colored stroke
        # painted on an otherwise-transparent canvas layer, the shape the
        # frontend's own mask layer actually produces.
        visible_mask = alpha.point(lambda p: 255 if p > 0 else 0)
        return Image.composite(luminance, Image.new("L", opened.size, 0), visible_mask)
    return opened.convert("L")


def decode_and_validate_mask(
    mask_bytes: bytes,
    *,
    target_width: int,
    target_height: int,
    min_editable_fraction: float = _DEFAULT_MIN_EDITABLE_FRACTION,
) -> DecodedMask:
    """Decodes `mask_bytes`, aligns it to `(target_width, target_height)`
    (the *source image's* real pixel dimensions -- never assumed to already
    match, since a canvas-exported mask can legitimately be a different
    size than the original photo) via nearest-neighbor resize (a mask is a
    hard region boundary, not photographic content -- smoothing/
    antialiasing here would blur the very edge the user drew), and rejects
    a mask with too little editable area with an actionable message.

    Raises:
        ImageDecodeError: `mask_bytes` isn't a valid, decodable image.
        MaskValidationError: decodes fine but has (near-)zero editable area
            once resolved against the source image's real dimensions --
            this is what makes "the user opened the mask tool but never
            painted anything" (or a single stray click) fail with an
            actionable message instead of silently sending a no-op mask to
            a paid provider or producing a no-visible-change blur result.
    """
    try:
        with Image.open(io.BytesIO(mask_bytes)) as opened:
            channel = _mask_channel_from_image(opened)
            if channel.size != (target_width, target_height):
                channel = channel.resize((target_width, target_height), Image.Resampling.NEAREST)
            array = np.array(channel, dtype=np.uint8)
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError(f"Could not decode mask image: {exc}") from exc

    total_pixels = array.size
    editable_pixel_count = int(np.count_nonzero(array > _EDITABLE_INTENSITY_THRESHOLD))
    editable_fraction = editable_pixel_count / total_pixels if total_pixels else 0.0
    if editable_fraction < min_editable_fraction:
        raise MaskValidationError(
            "The selected region is empty or too small to edit. Paint a larger area with "
            "the mask tool and try again."
        )

    return DecodedMask(
        array=array,
        width=target_width,
        height=target_height,
        editable_pixel_count=editable_pixel_count,
        editable_fraction=editable_fraction,
    )


def mask_array_to_png_bytes(array: np.ndarray) -> bytes:
    """Round-trips a decoded mask array back to single-channel PNG bytes --
    used by a caller (`media_gen.image_edit_provider`) that needs mask
    bytes in the canonical grayscale form rather than whatever shape the
    original upload was in."""
    buffer = io.BytesIO()
    Image.fromarray(array, mode="L").save(buffer, format="PNG")
    return buffer.getvalue()


__all__ = [
    "DecodedMask",
    "MaskValidationError",
    "decode_and_validate_mask",
    "mask_array_to_png_bytes",
]
