"""Chroma collection management for the media-search library -- mirrors
`embeddings/schema_indexer.py`'s collection pattern (get-or-create,
`hnsw:space="cosine"`) but stores/queries with explicit precomputed
embeddings (via `media/embedding.py`) rather than Chroma's own
text-embedding-function callback, since CLIP embeds images and text
through one shared model that Chroma's `EmbeddingFunction` interface
(text-only) doesn't natively support.

Two collections, not one, mirroring `embeddings/golden_examples.py`'s
"one collection per distinct purpose" convention:

- `media_images`: one record per ingested image, id = the image's own
  content hash (`media/ingest.py`'s `_content_hash`) -- re-ingesting the
  same file is an idempotent upsert, not a duplicate.
- `media_video_segments`: up to two records per detected video segment
  (one embedded from the segment's caption/transcript/OCR text, one from
  its representative keyframe image), sharing a `segment_id` in their
  metadata so `media/search.py` can merge/de-dupe hits from either
  modality, and so `api/media_library.py` can resolve a hit's thumbnail by
  that shared id regardless of which modality actually matched.

Both collections live in the same Chroma persist directory
(`Settings.chroma_persist_dir`) and reuse `embeddings.schema_indexer
.get_chroma_client`'s process-lifetime-cached `PersistentClient` --
creating a second `PersistentClient` against the same on-disk directory
within one process is a real, previously-reproduced bug (see that
function's own docstring), so every Chroma-backed module in this codebase
shares the one cached client rather than constructing its own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Embeddings, Metadatas, QueryResult

from config.settings import Settings
from embeddings.schema_indexer import get_chroma_client

logger = logging.getLogger(__name__)

_IMAGE_COLLECTION_NAME = "media_images"
_SEGMENT_COLLECTION_NAME = "media_video_segments"


def _get_or_create(client: chromadb.ClientAPI, name: str) -> Collection:
    return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def get_image_collection(settings: Settings) -> Collection:
    """Gets or creates the ingested-image collection."""
    return _get_or_create(get_chroma_client(settings), _IMAGE_COLLECTION_NAME)


def get_segment_collection(settings: Settings) -> Collection:
    """Gets or creates the video-segment collection."""
    return _get_or_create(get_chroma_client(settings), _SEGMENT_COLLECTION_NAME)


def upsert_image(
    media_id: str,
    embedding: list[float],
    *,
    source_path: str,
    width: int,
    height: int,
    settings: Settings,
) -> None:
    """Idempotent upsert of one ingested image -- re-ingesting the same
    content (same content-hash-derived `media_id`) overwrites the same
    record rather than accumulating a duplicate."""
    get_image_collection(settings).upsert(
        ids=[media_id],
        embeddings=cast(Embeddings, [embedding]),
        metadatas=cast(
            Metadatas,
            [
                {
                    "source_path": source_path,
                    "media_type": "image",
                    "width": width,
                    "height": height,
                }
            ],
        ),
    )


def upsert_segment(
    segment_id: str,
    *,
    video_hash: str,
    text_embedding: list[float] | None,
    image_embedding: list[float] | None,
    source_path: str,
    thumbnail_path: str,
    segment_start: float,
    segment_end: float,
    caption: str,
    video_width: int,
    video_height: int,
    video_duration: float,
    settings: Settings,
) -> None:
    """Idempotent upsert of one video segment's up-to-two records (a text
    embedding of its caption/transcript/OCR text, an image embedding of
    its representative keyframe -- at least one is required). Both records
    carry identical metadata except their own `modality`, so either one
    alone is enough to resolve the segment's thumbnail/caption later.
    """
    if text_embedding is None and image_embedding is None:
        raise ValueError("upsert_segment requires at least one of text_embedding/image_embedding")

    base_metadata: dict[str, Any] = {
        "segment_id": segment_id,
        "video_hash": video_hash,
        "media_type": "video",
        "source_path": source_path,
        "thumbnail_path": thumbnail_path,
        "segment_start": segment_start,
        "segment_end": segment_end,
        "caption": caption,
        "width": video_width,
        "height": video_height,
        "duration": video_duration,
    }
    ids: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict[str, Any]] = []
    if text_embedding is not None:
        ids.append(f"{segment_id}:text")
        embeddings.append(text_embedding)
        metadatas.append({**base_metadata, "modality": "text"})
    if image_embedding is not None:
        ids.append(f"{segment_id}:image")
        embeddings.append(image_embedding)
        metadatas.append({**base_metadata, "modality": "image"})

    get_segment_collection(settings).upsert(
        ids=ids,
        embeddings=cast(Embeddings, embeddings),
        metadatas=cast(Metadatas, metadatas),
    )


def _zip_query_result(result: QueryResult) -> list[dict[str, Any]]:
    ids = (result.get("ids") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return [
        {"id": record_id, "metadata": metadata, "similarity": round(1.0 - distance, 4)}
        for record_id, metadata, distance in zip(ids, metadatas, distances, strict=True)
    ]


def query_images(embedding: list[float], top_k: int, settings: Settings) -> list[dict[str, Any]]:
    """Nearest-neighbor search over ingested images. Empty list on an
    empty collection (never an error -- mirrors
    `embeddings.golden_examples.retrieve_golden_examples`'s fail-open
    posture on a not-yet-populated store)."""
    collection = get_image_collection(settings)
    count = collection.count()
    if count == 0:
        return []
    result = collection.query(
        query_embeddings=cast(Embeddings, [embedding]), n_results=min(top_k, count)
    )
    return _zip_query_result(result)


def query_segments(embedding: list[float], top_k: int, settings: Settings) -> list[dict[str, Any]]:
    """Nearest-neighbor search over video-segment records (both text- and
    image-embedded records are searched together; `media/search.py` merges
    hits that share a `segment_id`)."""
    collection = get_segment_collection(settings)
    count = collection.count()
    if count == 0:
        return []
    result = collection.query(
        query_embeddings=cast(Embeddings, [embedding]), n_results=min(top_k, count)
    )
    return _zip_query_result(result)


def get_image_metadata(media_id: str, settings: Settings) -> Mapping[str, Any] | None:
    """Direct id lookup (not similarity search) -- what
    `api/media_library.py` uses to resolve which file to serve for a given
    `media_id`. `None` if never indexed or since evicted (this is a
    persistent Chroma collection, not the ephemeral `media_gen.cache
    .MediaCache`, so this should only be `None` for a genuinely unknown id
    or a moved/deleted source file)."""
    result = get_image_collection(settings).get(ids=[media_id])
    metadatas = result.get("metadatas") or []
    return metadatas[0] if metadatas else None


def is_image_indexed(media_id: str, settings: Settings) -> bool:
    """Whether an image with this content hash is already stored --
    `scripts/build_media_index.py` uses this to skip re-embedding
    unchanged files on a re-run, mirroring
    `db.schema_introspection.get_schema_fingerprint`'s skip-if-unchanged
    cost-saving pattern."""
    return get_image_metadata(media_id, settings) is not None


def is_video_indexed(video_hash: str, settings: Settings) -> bool:
    """Whether any segment of this video (by its own content hash, not a
    segment id) is already stored -- same skip-if-unchanged purpose as
    `is_image_indexed`, for the video pipeline."""
    result = get_segment_collection(settings).get(where={"video_hash": video_hash}, limit=1)
    return bool(result.get("ids"))


def get_segment_metadata(segment_id: str, settings: Settings) -> Mapping[str, Any] | None:
    """Looks up a video segment's metadata by its shared `segment_id` (not
    the internal `:text`/`:image`-suffixed Chroma record id) -- either
    record has identical source/thumbnail metadata, so the first match is
    enough."""
    result = get_segment_collection(settings).get(where={"segment_id": segment_id}, limit=1)
    metadatas = result.get("metadatas") or []
    return metadatas[0] if metadatas else None
