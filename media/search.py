"""Media-search retrieval -- embeds the query text once, searches both the
image and video-segment Chroma collections (`media/store.py`), and merges
video hits that share a `segment_id` (one segment may match on its
caption/transcript/OCR text, its keyframe image, or both -- these are two
separate Chroma records, see `media/store.py`'s module docstring). Never
exposes a raw similarity score or a raw file path in anything returned
here beyond what `agent.orchestrator.nodes.media_search_node`/
`api/media_search.py` need internally -- see `MediaHit`'s own docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from config.settings import Settings
from media import embedding, store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaHit:
    """One retrieved image or video segment.

    `media_id` is the only handle a caller needs to fetch this hit's bytes
    (`GET /media/library/{media_id}`, `api/media_library.py`) -- never a
    raw filesystem path. `similarity` is kept for internal ranking/merging
    only -- `agent.orchestrator.nodes.media_search_node`'s answer-
    composition prompt never surfaces it to the user (see that node's
    docstring).
    """

    media_id: str
    media_type: Literal["image", "video"]
    caption: str
    similarity: float
    timestamp_start: float | None = None
    timestamp_end: float | None = None


def _image_hits(embedding_vector: list[float], top_k: int, settings: Settings) -> list[MediaHit]:
    hits: list[MediaHit] = []
    for row in store.query_images(embedding_vector, top_k, settings):
        metadata = row["metadata"] or {}
        source_path = str(metadata.get("source_path", ""))
        hits.append(
            MediaHit(
                media_id=row["id"],
                media_type="image",
                caption=source_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                similarity=row["similarity"],
            )
        )
    return hits


def _segment_hits(embedding_vector: list[float], top_k: int, settings: Settings) -> list[MediaHit]:
    """Queries the segment collection and merges the (up to) two records
    -- text-embedded and image-embedded -- that share one `segment_id`
    into a single hit, keeping the better-scoring modality's similarity."""
    best_by_segment: dict[str, MediaHit] = {}
    for row in store.query_segments(embedding_vector, top_k, settings):
        metadata = row["metadata"] or {}
        segment_id = metadata.get("segment_id")
        if not segment_id:
            continue
        candidate = MediaHit(
            media_id=segment_id,
            media_type="video",
            caption=str(metadata.get("caption", "")),
            similarity=row["similarity"],
            timestamp_start=metadata.get("segment_start"),
            timestamp_end=metadata.get("segment_end"),
        )
        existing = best_by_segment.get(segment_id)
        if existing is None or candidate.similarity > existing.similarity:
            best_by_segment[segment_id] = candidate
    return list(best_by_segment.values())


def search_media(
    query: str,
    settings: Settings,
    media_type: Literal["image", "video", "any"] = "any",
    top_k: int | None = None,
) -> list[MediaHit]:
    """Searches the media library for the given natural-language query.

    Args:
        query: The user's plain-English description of what to find.
        settings: Current process settings.
        media_type: Restricts results to `"image"` or `"video"` only;
            `"any"` (the default) searches both collections.
        top_k: Override for `Settings.media_search_top_k`.

    Returns:
        Up to `top_k` hits total, best-similarity first, across whichever
        collection(s) `media_type` selects. Empty list if nothing is
        indexed yet or the embedding call fails for any reason -- this is
        a retrieval aid, never a reason a question can't be answered (the
        caller falls back to "no media found," not an error).
    """
    resolved_top_k = top_k or settings.media_search_top_k
    try:
        query_vector = embedding.embed_text(query, settings)
    except Exception:
        logger.warning("[media] query embedding failed, returning no hits", exc_info=True)
        return []

    hits: list[MediaHit] = []
    if media_type in ("image", "any"):
        hits.extend(_image_hits(query_vector, resolved_top_k, settings))
    if media_type in ("video", "any"):
        hits.extend(_segment_hits(query_vector, resolved_top_k, settings))

    hits.sort(key=lambda hit: hit.similarity, reverse=True)
    return hits[:resolved_top_k]
