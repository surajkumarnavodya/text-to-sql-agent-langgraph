"""Deterministic region blur -- real, local, non-generative Pillow work, no
model call and no network. Added specifically because the image editor's
"Blur the selected face" quick action must default to a deterministic
filter rather than paying for generative inference (see CLAUDE.md's
image-editing design: "For deterministic blur ... use local/Pillow
processing instead of paying for generative inference"). Mirrors
`attachments/image_ops.py`'s exact style and conventions -- a sibling
capability, not a special case.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from attachments.image_processing import ImageDecodeError
from attachments.mask import decode_and_validate_mask


class BlurRegionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_bytes: bytes
    media_type: str
    width: int
    height: int
    size_bytes: int = Field(description="Byte size of the blurred output.")


def blur_region(file_bytes: bytes, mask_bytes: bytes, *, radius: int = 18) -> BlurRegionResult:
    """Applies a Gaussian blur only within the painted mask region and
    returns the edited image -- real, deterministic Pillow work. `mask_bytes`
    follows this app's canonical mask convention (`attachments.mask`'s own
    docstring): the painted/opaque region is blurred, everything else is
    left byte-identical to the source outside that region.

    Args:
        file_bytes: The source image to blur a region of.
        mask_bytes: The painted mask, in any of the shapes
            `attachments.mask.decode_and_validate_mask` accepts -- resized
            to the source image's real dimensions if it doesn't already
            match.
        radius: Gaussian blur radius in pixels.

    Raises:
        ImageDecodeError: `file_bytes` isn't a valid, decodable image, or
            the mask isn't either.
        MaskValidationError (a `ValueError` subclass): the mask has
            (near-)zero editable area.
    """
    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            transposed = ImageOps.exif_transpose(opened) or opened.copy()
            image_rgb = transposed.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc

    width, height = image_rgb.size
    decoded_mask = decode_and_validate_mask(mask_bytes, target_width=width, target_height=height)
    mask_image = Image.fromarray(decoded_mask.array, mode="L")

    blurred = image_rgb.filter(ImageFilter.GaussianBlur(radius=radius))
    # PIL.Image.composite(image1, image2, mask): image1 shows through where
    # mask is opaque (255), image2 where mask is transparent (0) -- so the
    # blurred version shows through exactly where the user painted, and the
    # original shows through everywhere else, pixel-for-pixel unchanged.
    composited = Image.composite(blurred, image_rgb, mask_image)

    buffer = io.BytesIO()
    composited.save(buffer, format="PNG", optimize=True)
    output_bytes = buffer.getvalue()

    return BlurRegionResult(
        image_bytes=output_bytes,
        media_type="image/png",
        width=width,
        height=height,
        size_bytes=len(output_bytes),
    )


__all__ = ["BlurRegionResult", "blur_region"]
