"""Per-file media ingestion -- detects image vs. video by actual content
(magic bytes, never a trusted file extension), validates size before any
processing, then runs the appropriate pipeline:

- **Image**: one CLIP embedding, one Chroma record (`media/store.py
  ::upsert_image`).
- **Video**: scene-change keyframes (`media/keyframes.py`) -> per-segment
  ASR transcript (`media/transcription.py`) + OCR text
  (`media/ocr.py`) + an optional dense caption (`media/captioning.py`,
  fails open) -> up to two embeddings per segment (its combined text, and
  its keyframe image -- `media/embedding.py`) -> one `media/store.py
  ::upsert_segment` call per segment.

Every id is content-hash-derived, never random: re-ingesting the exact
same file is an idempotent upsert (same `media_id`/`segment_id`s), not a
duplicate -- `scripts/build_media_index.py` relies on this to make re-runs
cheap and safe.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config.settings import Settings
from media import embedding, store
from media.captioning import generate_caption
from media.exceptions import MediaFileTooLargeError, UnsupportedMediaTypeError
from media.keyframes import extract_keyframes
from media.ocr import extract_text
from media.transcription import transcribe_video, transcript_for_range

logger = logging.getLogger(__name__)

# Magic bytes at offset 0 for every supported image format -- checked in
# order. A video container's real signature lives at a small,
# format-specific offset, so those are checked separately below rather
# than folded into this table.
_IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"BM",  # BMP
)


def _sniff_media_type(header: bytes) -> Literal["image", "video"] | None:
    """Identifies a file's real media type from its first bytes -- never
    trusts the file extension. Returns `None` for anything unrecognized."""
    if any(header.startswith(signature) for signature in _IMAGE_SIGNATURES):
        return "image"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image"
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "video"
    if header[4:8] == b"ftyp":  # MP4/MOV/M4V family
        return "video"
    if header[:4] == b"\x1a\x45\xdf\xa3":  # WebM/Matroska (EBML header)
        return "video"
    return None


def _content_hash(path: Path) -> str:
    """SHA-256 of the file's bytes -- the deterministic id every stored
    record is keyed on, so re-ingesting identical content is an idempotent
    upsert (see this module's docstring)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_video_metadata(path: Path) -> tuple[int, int, float]:
    """Cheap width/height/duration probe -- separate from
    `media/keyframes.py::extract_keyframes` since that function's own
    `cv2.VideoCapture` is scoped to keyframe extraction, not general
    metadata (kept independent so a keyframe-extraction failure and a
    metadata-probe failure are each recoverable on their own)."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        duration = (frame_count / fps) if fps else 0.0
        return width, height, duration
    finally:
        capture.release()


def _segment_id(video_hash: str, start: float, end: float) -> str:
    digest_input = f"{video_hash}\x00{start:.3f}\x00{end:.3f}".encode()
    return hashlib.sha256(digest_input).hexdigest()


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one `ingest_file` call."""

    media_id: str
    media_type: Literal["image", "video"]
    segments_indexed: int


def _validate_file(path: Path, settings: Settings) -> Literal["image", "video"]:
    if not path.is_file():
        raise UnsupportedMediaTypeError(f"{path} is not a file.")

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > settings.media_max_file_mb:
        raise MediaFileTooLargeError(
            f"{path.name} is {size_mb:.1f}MB, exceeding the "
            f"{settings.media_max_file_mb}MB cap (Settings.media_max_file_mb)."
        )

    with path.open("rb") as handle:
        header = handle.read(16)
    media_type = _sniff_media_type(header)
    if media_type is None:
        raise UnsupportedMediaTypeError(
            f"{path.name}'s content doesn't match a supported image/video "
            "format (checked via file signature, not its extension)."
        )
    return media_type


def _ingest_image(
    path: Path, content_hash: str, settings: Settings, *, force: bool
) -> IngestResult:
    if not force and store.is_image_indexed(content_hash, settings):
        logger.info("[media] %s unchanged since last index, skipping", path)
        return IngestResult(media_id=content_hash, media_type="image", segments_indexed=0)

    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size

    vector = embedding.embed_image(path, settings)
    store.upsert_image(
        content_hash, vector, source_path=str(path), width=width, height=height, settings=settings
    )
    return IngestResult(media_id=content_hash, media_type="image", segments_indexed=0)


def _ingest_video(
    path: Path, content_hash: str, settings: Settings, *, force: bool
) -> IngestResult:
    if not force and store.is_video_indexed(content_hash, settings):
        logger.info("[media] %s unchanged since last index, skipping", path)
        return IngestResult(media_id=content_hash, media_type="video", segments_indexed=0)

    width, height, duration = _probe_video_metadata(path)
    keyframe_segments = extract_keyframes(path, content_hash, settings.media_scene_detect_threshold)
    transcript_segments = transcribe_video(path, settings)

    indexed = 0
    for keyframe in keyframe_segments:
        transcript_text = transcript_for_range(
            transcript_segments, keyframe.segment_start, keyframe.segment_end
        )
        ocr_text = extract_text(keyframe.thumbnail_path)
        caption = generate_caption(keyframe.thumbnail_path, transcript_text, settings)

        # Best available human-readable text for this segment, in priority
        # order -- what search results actually cite. Never all blank: a
        # segment with no caption/transcript/OCR text still gets a plain
        # placeholder rather than an empty citation.
        display_text = (
            caption or transcript_text or ocr_text or "Video segment (no detected text or speech)."
        )
        combined_text = " ".join(filter(None, [caption, transcript_text, ocr_text])).strip()

        text_embedding = embedding.embed_text(combined_text, settings) if combined_text else None
        image_embedding = embedding.embed_image(keyframe.thumbnail_path, settings)

        store.upsert_segment(
            _segment_id(content_hash, keyframe.segment_start, keyframe.segment_end),
            video_hash=content_hash,
            text_embedding=text_embedding,
            image_embedding=image_embedding,
            source_path=str(path),
            thumbnail_path=str(keyframe.thumbnail_path),
            segment_start=keyframe.segment_start,
            segment_end=keyframe.segment_end,
            caption=display_text,
            video_width=width,
            video_height=height,
            video_duration=duration,
            settings=settings,
        )
        indexed += 1

    return IngestResult(media_id=content_hash, media_type="video", segments_indexed=indexed)


def ingest_file(path: Path, settings: Settings, *, force: bool = False) -> IngestResult:
    """Ingests one image or video file into the media-search index.

    Args:
        path: The file to ingest.
        settings: Current process settings.
        force: Re-embeds even if this exact content is already indexed
            (by content hash). Default `False` -- an unchanged file is
            skipped, the same skip-if-unchanged cost-saving behavior
            `scripts/build_embeddings.py --force` mirrors for schema
            indexing (re-running the full ASR/OCR/captioning/embedding
            pipeline for an unchanged video is genuinely expensive, unlike
            a cheap content-hash check).

    Raises:
        UnsupportedMediaTypeError: the file's real content (not its
            extension) doesn't match a supported image/video format.
        MediaFileTooLargeError: the file exceeds `Settings.media_max_file_mb`.

    Never raises for a downstream pipeline step (ASR/OCR/captioning) --
    those each fail open on their own (see their own module docstrings);
    only file-validity/size problems here are treated as hard failures,
    since indexing genuinely can't proceed at all in that case.
    """
    media_type = _validate_file(path, settings)
    content_hash = _content_hash(path)
    logger.info("[media] ingesting %s (%s, hash=%s)", path, media_type, content_hash[:12])

    if media_type == "image":
        return _ingest_image(path, content_hash, settings, force=force)
    return _ingest_video(path, content_hash, settings, force=force)
