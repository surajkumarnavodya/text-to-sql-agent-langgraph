"""Per-file media ingestion -- detects image vs. video by actual content
(magic bytes, never a trusted file extension), validates size before any
processing, runs every file through the pre-ingestion moderation gate
(`moderation/gate.py`), then -- only on a pass -- runs the appropriate
pipeline:

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

**Moderation gate (mandatory, not a feature flag -- see
`config/settings.py`'s `moderation_provider` docstring).** Before anything
above ever runs, `moderation.store.get_asset_by_hash` checks whether this
exact content has already been moderated: a prior "passed" or "rejected"
result short-circuits immediately (no re-running the provider call or the
embedding pipeline, unless `force=True`). For genuinely new content,
`moderation.gate.moderate_chunks` runs against every chunk (the whole image,
or an NxN tile grid for a very large one; every video segment's keyframe
image plus its combined caption/transcript/OCR text) *before* any Chroma
write -- a hard-reject on any chunk means the **entire** file is rejected:
nothing is embedded or stored anywhere, and `moderation.store.record_asset`
records only the hash/category/timestamp, never the content. See
`moderation/taxonomy.py` for the category table and
`docs/RISK_REGISTER.md`/`SECURITY.md` for what this does and doesn't cover.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
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
from moderation.gate import decision_summary, moderate_chunks
from moderation.store import ensure_schema, get_asset_by_hash, get_moderation_engine, record_asset
from moderation.types import ModerationChunk

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
    moderation_status: Literal["passed", "rejected"] = "passed"


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


def _tile_image_for_moderation(path: Path, threshold_px: int) -> tuple[list[Path], list[Path]]:
    """Splits an image into an NxN grid of temp-file tiles if it's larger
    than `threshold_px` (pixels) in either dimension -- on the theory that a
    moderation classifier's own internal downsampling could otherwise shrink
    away a small region of concern in a very large image. An image at or
    under the threshold is returned as a single chunk, no tiling, no temp
    files.

    Returns:
        `(chunk_image_paths, temp_files_to_clean_up)` -- the second list is
        empty when no tiling happened; the caller is responsible for
        deleting every path in it once moderation has run.
    """
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        if width <= threshold_px and height <= threshold_px:
            return [path], []

        cols = max(1, -(-width // threshold_px))  # ceil division
        rows = max(1, -(-height // threshold_px))
        tile_w = -(-width // cols)
        tile_h = -(-height // rows)

        tiles: list[Path] = []
        for row in range(rows):
            for col in range(cols):
                box = (
                    col * tile_w,
                    row * tile_h,
                    min((col + 1) * tile_w, width),
                    min((row + 1) * tile_h, height),
                )
                tile_image = image.crop(box)
                fd, tmp_path_str = tempfile.mkstemp(suffix=".png", prefix="media_tile_")
                os.close(fd)  # the fd must be closed before Pillow can write to this path (Windows)
                tmp_path = Path(tmp_path_str)
                tile_image.save(tmp_path, format="PNG")
                tiles.append(tmp_path)
        return tiles, tiles


def _ingest_image(
    path: Path, content_hash: str, settings: Settings, *, force: bool
) -> IngestResult:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size

    chunk_paths, temp_files = _tile_image_for_moderation(path, settings.media_image_tile_threshold_px)
    try:
        chunks = [
            ModerationChunk(chunk_index=i, content_type="image", image_path=tile_path)
            for i, tile_path in enumerate(chunk_paths)
        ]
        decision = moderate_chunks(content_hash, chunks, settings)
    finally:
        for temp_path in temp_files:
            temp_path.unlink(missing_ok=True)

    engine = get_moderation_engine(settings)
    ensure_schema(engine)

    if not decision.passed:
        record_asset(
            engine,
            content_hash,
            "image",
            "rejected",
            decision_summary(decision),
            source_path=str(path),
        )
        return IngestResult(
            media_id=content_hash, media_type="image", segments_indexed=0, moderation_status="rejected"
        )

    vector = embedding.embed_image(path, settings)
    store.upsert_image(
        content_hash, vector, source_path=str(path), width=width, height=height, settings=settings
    )
    record_asset(
        engine,
        content_hash,
        "image",
        "passed",
        decision_summary(decision),
        source_path=str(path),
        chunk_count=1,
        vector_ids=[content_hash],
    )
    return IngestResult(media_id=content_hash, media_type="image", segments_indexed=0)


@dataclass(frozen=True)
class _SegmentPrep:
    """Everything computed about one video segment before the moderation
    decision is known -- reused in phase 2 (embed + store) without
    redoing any ASR/OCR/captioning work, so moderation adds one extra pass
    over already-extracted data, not a second expensive extraction pass."""

    keyframe: object  # media.keyframes.KeyframeSegment -- avoids a hard import cycle here
    display_text: str
    combined_text: str


def _ingest_video(
    path: Path, content_hash: str, settings: Settings, *, force: bool
) -> IngestResult:
    width, height, duration = _probe_video_metadata(path)
    keyframe_segments = extract_keyframes(path, content_hash, settings.media_scene_detect_threshold)
    transcript_segments = transcribe_video(path, settings)

    # Phase 1: extract every segment's text/OCR/caption and build the full
    # set of moderation chunks *before* embedding or storing anything --
    # per this feature's decision rule, a hard-reject on even one segment
    # rejects the whole video, so nothing may be stored until every
    # segment has been checked.
    preps: list[_SegmentPrep] = []
    chunks: list[ModerationChunk] = []
    for index, keyframe in enumerate(keyframe_segments):
        transcript_text = transcript_for_range(
            transcript_segments, keyframe.segment_start, keyframe.segment_end
        )
        ocr_text = extract_text(keyframe.thumbnail_path)
        caption = generate_caption(keyframe.thumbnail_path, transcript_text, settings)

        display_text = (
            caption or transcript_text or ocr_text or "Video segment (no detected text or speech)."
        )
        combined_text = " ".join(filter(None, [caption, transcript_text, ocr_text])).strip()
        preps.append(_SegmentPrep(keyframe=keyframe, display_text=display_text, combined_text=combined_text))

        chunks.append(
            ModerationChunk(chunk_index=index, content_type="image", image_path=keyframe.thumbnail_path)
        )
        if combined_text:
            chunks.append(ModerationChunk(chunk_index=index, content_type="text", text=combined_text))

    decision = moderate_chunks(content_hash, chunks, settings)
    engine = get_moderation_engine(settings)
    ensure_schema(engine)

    if not decision.passed:
        record_asset(
            engine,
            content_hash,
            "video",
            "rejected",
            decision_summary(decision),
            source_path=str(path),
        )
        return IngestResult(
            media_id=content_hash, media_type="video", segments_indexed=0, moderation_status="rejected"
        )

    # Phase 2: every segment passed -- embed and store, unchanged from the
    # pre-moderation implementation, reusing phase 1's already-computed text.
    indexed = 0
    vector_ids: list[str] = []
    for prep in preps:
        keyframe = prep.keyframe
        text_embedding = embedding.embed_text(prep.combined_text, settings) if prep.combined_text else None
        image_embedding = embedding.embed_image(keyframe.thumbnail_path, settings)

        segment_id = _segment_id(content_hash, keyframe.segment_start, keyframe.segment_end)
        store.upsert_segment(
            segment_id,
            video_hash=content_hash,
            text_embedding=text_embedding,
            image_embedding=image_embedding,
            source_path=str(path),
            thumbnail_path=str(keyframe.thumbnail_path),
            segment_start=keyframe.segment_start,
            segment_end=keyframe.segment_end,
            caption=prep.display_text,
            video_width=width,
            video_height=height,
            video_duration=duration,
            settings=settings,
        )
        vector_ids.append(segment_id)
        indexed += 1

    record_asset(
        engine,
        content_hash,
        "video",
        "passed",
        decision_summary(decision),
        source_path=str(path),
        chunk_count=indexed,
        vector_ids=vector_ids,
    )
    return IngestResult(media_id=content_hash, media_type="video", segments_indexed=indexed)


def ingest_file(path: Path, settings: Settings, *, force: bool = False) -> IngestResult:
    """Ingests one image or video file into the media-search index.

    Args:
        path: The file to ingest.
        settings: Current process settings.
        force: Bypasses the moderation-store dedupe short-circuit and
            re-runs moderation + (if it passes) re-embeds, even if this
            exact content was already processed -- the same skip-if-
            unchanged cost-saving behavior `scripts/build_embeddings.py
            --force` mirrors for schema indexing, now gating a real,
            metered moderation-provider call too, not just embedding.
            Note: if a previously-*passed* asset is force-re-moderated and
            now hard-rejects (e.g. after a threshold/blocklist change),
            this does not retroactively remove its already-stored Chroma
            records -- that cleanup is a separate, not-yet-built operation.

    Raises:
        UnsupportedMediaTypeError: the file's real content (not its
            extension) doesn't match a supported image/video format.
        MediaFileTooLargeError: the file exceeds `Settings.media_max_file_mb`.
        ModerationNotConfiguredError: the moderation provider and/or
            metadata store aren't configured -- this gate is mandatory
            whenever `ENABLE_MEDIA_SEARCH` is on, so ingestion fails closed
            rather than silently skipping the check.

    Never raises for a downstream pipeline step (ASR/OCR/captioning) --
    those each fail open on their own (see their own module docstrings);
    only file-validity/size problems and moderation-configuration problems
    here are treated as hard failures, since indexing genuinely can't
    proceed at all in either case.
    """
    media_type = _validate_file(path, settings)
    content_hash = _content_hash(path)
    logger.info("[media] ingesting %s (%s, hash=%s)", path, media_type, content_hash[:12])

    if not force:
        engine = get_moderation_engine(settings)
        ensure_schema(engine)
        existing = get_asset_by_hash(engine, content_hash)
        if existing is not None:
            if existing.moderation_status == "rejected":
                logger.info("[media] %s previously rejected by moderation, skipping", path)
                return IngestResult(
                    media_id=content_hash,
                    media_type=media_type,
                    segments_indexed=0,
                    moderation_status="rejected",
                )
            logger.info("[media] %s unchanged since last index, skipping", path)
            return IngestResult(
                media_id=content_hash,
                media_type=media_type,
                segments_indexed=existing.chunk_count if media_type == "video" else 0,
            )

    if media_type == "image":
        return _ingest_image(path, content_hash, settings, force=force)
    return _ingest_video(path, content_hash, settings, force=force)
