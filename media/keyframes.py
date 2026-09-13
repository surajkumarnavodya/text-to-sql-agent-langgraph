"""Video keyframe extraction -- scene-change boundaries, not fixed
intervals, via `PySceneDetect`. This avoids indexing many near-identical
frames from one continuous shot as if they were separate content, which
fixed-interval sampling would do on anything longer than a few seconds.

`PySceneDetect` requires `opencv-python` unconditionally at this pinned
version (0.7.1) -- confirmed against its PyPI `requires_dist` metadata
before pinning (`av`/PyAV is only an optional extra for its separate
clip-export feature, not a way to avoid OpenCV for scene detection itself;
see `requirements.txt`'s comment on this). Each detected scene's
representative frame (its midpoint) is read directly via `cv2.VideoCapture`
and saved as a small on-disk JPEG thumbnail under `media/.thumbnails/`
(gitignored, alongside `embeddings/.chroma/`'s existing local-artifact
convention) -- this is what `media/embedding.py::embed_image` embeds, and
what `api/media_library.py` serves back for a video hit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Public (not a leading-underscore module-private constant) -- `api/
# media_library.py` needs this same root to safely resolve a stored
# thumbnail path before serving it (path-traversal defense).
THUMBNAIL_DIR = Path(__file__).resolve().parent / ".thumbnails"


@dataclass(frozen=True)
class KeyframeSegment:
    """One scene-change-bounded video segment and its representative frame."""

    segment_start: float
    segment_end: float
    thumbnail_path: Path


def _thumbnail_path(video_hash: str, index: int) -> Path:
    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    return THUMBNAIL_DIR / f"{video_hash}_{index:04d}.jpg"


def extract_keyframes(video_path: Path, video_hash: str, threshold: float) -> list[KeyframeSegment]:
    """Detects scene-change boundaries and saves one representative frame
    (each scene's midpoint) per segment.

    Args:
        video_path: Path to the source video file.
        video_hash: The video's own content hash (`media/ingest.py`'s
            `_content_hash`) -- namespaces this video's thumbnail files so
            two different videos' segments never collide, and so
            re-ingesting the same file overwrites the same thumbnails
            rather than accumulating new ones.
        threshold: `Settings.media_scene_detect_threshold`.

    Returns:
        One `KeyframeSegment` per detected scene, in chronological order.
        A video with no detected scene changes (e.g. one static shot)
        still returns exactly one segment spanning the whole video. A
        frame that can't be read for a given segment is logged and
        skipped -- one bad segment must not fail the whole video's
        ingestion.
    """
    import cv2
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(video_path))
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    scene_manager.detect_scenes(video)
    scene_list = scene_manager.get_scene_list()

    if scene_list:
        boundaries = [(start.get_seconds(), end.get_seconds()) for start, end in scene_list]
    else:
        duration_seconds = video.duration.get_seconds() if video.duration else 0.0
        boundaries = [(0.0, duration_seconds)]

    capture = cv2.VideoCapture(str(video_path))
    try:
        segments: list[KeyframeSegment] = []
        for index, (start_seconds, end_seconds) in enumerate(boundaries):
            midpoint_seconds = (start_seconds + end_seconds) / 2
            capture.set(cv2.CAP_PROP_POS_MSEC, midpoint_seconds * 1000)
            read_ok, frame = capture.read()
            if not read_ok:
                logger.warning(
                    "[media] could not read frame at %.2fs in %s, skipping segment",
                    midpoint_seconds,
                    video_path,
                )
                continue
            thumbnail_path = _thumbnail_path(video_hash, index)
            cv2.imwrite(str(thumbnail_path), frame)
            segments.append(
                KeyframeSegment(
                    segment_start=start_seconds,
                    segment_end=end_seconds,
                    thumbnail_path=thumbnail_path,
                )
            )
        return segments
    finally:
        capture.release()
