"""Short-lived, in-memory store for generated media bytes.

`generation_node` downloads a generated image/video's bytes once (see
`media_gen/download.py`) and puts them here, keyed by an opaque id -- never
the provider's raw CDN URL, which is never exposed to the LLM's answer text
or the API response (see `agent.orchestrator.state.MediaGenerationResult
.media_id`). `api/media.py`'s `GET /media/{media_id}` reads back through
this same module, and the React dashboard renders the bytes it returns.

Process-lifetime only, not persisted -- same accepted tradeoff as this
project's other process-global caches (`db.connection._cached_engine`,
`agent.rate_limit`'s limiters): a restart loses in-flight generated media,
which is fine for a locally-run, single-process tool. Bounded to
`_MAX_ITEMS` entries with FIFO eviction so a burst of generations (already
throttled by `agent.rate_limit.get_media_generation_limiter`) can't grow
memory unbounded from image/video bytes.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass

_MAX_ITEMS = 100


@dataclass(frozen=True)
class MediaCacheEntry:
    """One stored asset's raw bytes and its content type (for the
    `Content-Type` header `api/media.py` needs to serve it correctly)."""

    data: bytes
    content_type: str


class MediaCache:
    """Bounded id -> `MediaCacheEntry` store. Construct via
    `get_media_cache()` below rather than directly, mirroring every other
    process-lifetime singleton in this codebase (e.g.
    `agent.rate_limit.SlidingWindowRateLimiter`)."""

    def __init__(self, max_items: int = _MAX_ITEMS) -> None:
        self._max_items = max_items
        self._entries: OrderedDict[str, MediaCacheEntry] = OrderedDict()

    def put(self, data: bytes, content_type: str) -> str:
        """Stores `data` and returns a new opaque id. Evicts the oldest
        entry first if already at `max_items` -- FIFO, not LRU, since
        there's no read-recency signal worth tracking for this scale."""
        if len(self._entries) >= self._max_items:
            self._entries.popitem(last=False)
        media_id = uuid.uuid4().hex
        self._entries[media_id] = MediaCacheEntry(data=data, content_type=content_type)
        return media_id

    def get(self, media_id: str) -> MediaCacheEntry | None:
        """Returns the stored entry, or None if `media_id` was never
        stored, already evicted, or the process has since restarted."""
        return self._entries.get(media_id)


_media_cache: MediaCache | None = None


def get_media_cache() -> MediaCache:
    """Returns the process-wide media cache, creating it on first use --
    same lazy-singleton idiom as `agent.rate_limit.get_media_generation_limiter`."""
    global _media_cache
    if _media_cache is None:
        _media_cache = MediaCache()
    return _media_cache
