"""Unit tests for media_gen/cache.py -- the short-lived in-memory store
generated media bytes are cached under (see agent/orchestrator/nodes.py
::generation_node and api/media.py)."""

from __future__ import annotations

from media_gen.cache import MediaCache, get_media_cache


class TestMediaCache:
    def test_put_then_get_roundtrips(self):
        cache = MediaCache()
        media_id = cache.put(b"image-bytes", "image/png")
        entry = cache.get(media_id)
        assert entry is not None
        assert entry.data == b"image-bytes"
        assert entry.content_type == "image/png"

    def test_get_missing_id_returns_none(self):
        cache = MediaCache()
        assert cache.get("does-not-exist") is None

    def test_put_returns_a_distinct_id_each_time(self):
        cache = MediaCache()
        first = cache.put(b"a", "image/png")
        second = cache.put(b"b", "image/png")
        assert first != second

    def test_fifo_eviction_once_max_items_exceeded(self):
        cache = MediaCache(max_items=2)
        first_id = cache.put(b"first", "image/png")
        cache.put(b"second", "image/png")
        third_id = cache.put(b"third", "image/png")

        assert cache.get(first_id) is None  # evicted -- oldest goes first
        assert cache.get(third_id) is not None


class TestGetMediaCache:
    def test_returns_the_same_instance_across_calls(self):
        assert get_media_cache() is get_media_cache()
