"""Turns raw uploaded image bytes into a model-compatible representation.

Pipeline, in order (per this feature's own requirement): decode/verify -->
detect real format --> resize if oversized (aspect ratio preserved) -->
normalize to a model-friendly format --> strip metadata --> base64 data URL.

Deliberately uses Pillow's own decode as the *real* corruption check --
`attachments.validation`'s magic-byte sniff only proves the first few bytes
look right; a file that passes that but fails to fully decode here is
exactly the "corrupted image" case this module exists to catch before it
ever reaches a prompt.
"""

from __future__ import annotations

import base64
import io

from PIL import Image, UnidentifiedImageError

from config.settings import Settings

_FORMAT_TO_MEDIA_TYPE: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class ImageDecodeError(Exception):
    """Raised when `file_bytes` cannot be decoded as an image at all -- the
    real corruption check, distinct from `attachments.validation`'s cheaper
    magic-byte sniff."""


def normalize_image(file_bytes: bytes, settings: Settings) -> tuple[bytes, str]:
    """Decodes, verifies, resizes (if oversized), and re-encodes an image.

    Returns:
        `(normalized_bytes, media_type)` -- `media_type` is one of this
        module's supported output types (never GIF, even if the input was
        one -- see below), suitable for both display and
        `image_bytes_to_data_url`.

    Raises:
        ImageDecodeError: the bytes don't decode as a real image at all
            (truncated, corrupted, or not actually image data despite
            passing the magic-byte check).
    """
    try:
        with Image.open(io.BytesIO(file_bytes)) as probe:
            probe.verify()  # raises on a structurally broken file
        # Image.verify() leaves the file object unusable for further
        # operations (Pillow's own documented behavior) -- reopen fresh for
        # the real decode/resize/re-encode below.
        with Image.open(io.BytesIO(file_bytes)) as opened:
            detected_format = (opened.format or "").upper()
            # A single-frame view of an animated GIF -- this app sends a
            # static image to the vision model, never an animation; the
            # first frame is a reasonable, deterministic choice.
            image: Image.Image = opened.convert(
                "RGBA" if opened.mode in ("RGBA", "LA", "P") else "RGB"
            )

            max_dim = settings.max_attachment_image_dimension_px
            if image.width > max_dim or image.height > max_dim:
                image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

            # Re-encoding through a fresh buffer (rather than saving the
            # original bytes as-is) is what strips EXIF/ICC/XMP metadata --
            # Image.save() only embeds metadata it's explicitly given via an
            # `exif=`/`icc_profile=` kwarg, neither of which is passed here.
            buffer = io.BytesIO()
            if detected_format == "PNG" or image.mode == "RGBA":
                image.save(buffer, format="PNG", optimize=True)
                media_type = "image/png"
            else:
                # JPEG has no alpha channel -- image is already RGB in this
                # branch (never RGBA), so this never silently drops
                # transparency.
                image.save(buffer, format="JPEG", quality=90, optimize=True)
                media_type = "image/jpeg"
            return buffer.getvalue(), media_type
    except UnidentifiedImageError as exc:
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc
    except OSError as exc:
        # Pillow raises plain OSError for a truncated/corrupt file in some
        # decode paths (not just UnidentifiedImageError) -- both are the
        # same "this isn't a valid, complete image" outcome to this caller.
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc


def image_bytes_to_data_url(image_bytes: bytes, media_type: str) -> str:
    """Encodes `image_bytes` as a base64 data URL -- the model-compatible
    representation this app actually sends (never a browser-only path like
    `C:\\Users\\...` or `blob:http://localhost/...`, neither of which the
    model process can read).

    Example output: `data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ...`
    """
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def image_file_to_data_url(path: str, settings: Settings) -> str:
    """Reads an image file from disk, normalizes it, and returns a data URL
    -- the on-disk-path equivalent of `image_bytes_to_data_url`, for a
    caller that only has `Attachment.local_path` (matches the spec's own
    named helper signature)."""
    with open(path, "rb") as file:
        file_bytes = file.read()
    normalized_bytes, media_type = normalize_image(file_bytes, settings)
    return image_bytes_to_data_url(normalized_bytes, media_type)
