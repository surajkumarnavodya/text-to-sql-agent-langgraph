"""Unit tests for media/search.py -- retrieval + video-segment merge/de-dupe logic."""

from __future__ import annotations

from media.search import search_media


def _row(record_id: str, metadata: dict, similarity: float) -> dict:
    return {"id": record_id, "metadata": metadata, "similarity": similarity}


class TestSearchMedia:
    def test_fails_open_to_empty_list_on_embedding_error(self, monkeypatch):
        def _raise(query, settings):
            raise RuntimeError("model not loaded")

        monkeypatch.setattr("media.search.embedding.embed_text", _raise)

        assert search_media("a cat", settings=None, top_k=5) == []

    def test_returns_only_images_when_media_type_is_image(self, monkeypatch):
        monkeypatch.setattr("media.search.embedding.embed_text", lambda q, s: [0.1])
        monkeypatch.setattr(
            "media.search.store.query_images",
            lambda vec, k, s: [_row("img1", {"source_path": "/a.jpg"}, 0.9)],
        )
        monkeypatch.setattr(
            "media.search.store.query_segments",
            lambda vec, k, s: (_ for _ in ()).throw(
                AssertionError("query_segments must not be called for media_type='image'")
            ),
        )

        hits = search_media("a cat", settings=None, media_type="image", top_k=5)

        assert len(hits) == 1
        assert hits[0].media_type == "image"
        assert hits[0].media_id == "img1"

    def test_merges_text_and_image_records_sharing_a_segment_id(self, monkeypatch):
        monkeypatch.setattr("media.search.embedding.embed_text", lambda q, s: [0.1])
        monkeypatch.setattr("media.search.store.query_images", lambda vec, k, s: [])
        monkeypatch.setattr(
            "media.search.store.query_segments",
            lambda vec, k, s: [
                _row(
                    "seg1:text",
                    {
                        "segment_id": "seg1",
                        "caption": "a crane lifting a beam",
                        "segment_start": 10.0,
                        "segment_end": 15.0,
                    },
                    0.7,
                ),
                _row(
                    "seg1:image",
                    {
                        "segment_id": "seg1",
                        "caption": "a crane lifting a beam",
                        "segment_start": 10.0,
                        "segment_end": 15.0,
                    },
                    0.85,
                ),
            ],
        )

        hits = search_media("crane lifting beam", settings=None, media_type="video", top_k=5)

        # Two Chroma records for the same segment collapse into one hit,
        # keeping the better-scoring modality.
        assert len(hits) == 1
        assert hits[0].media_id == "seg1"
        assert hits[0].similarity == 0.85
        assert hits[0].timestamp_start == 10.0
        assert hits[0].timestamp_end == 15.0

    def test_sorts_by_similarity_and_truncates_to_top_k(self, monkeypatch):
        monkeypatch.setattr("media.search.embedding.embed_text", lambda q, s: [0.1])
        monkeypatch.setattr(
            "media.search.store.query_images",
            lambda vec, k, s: [
                _row("img1", {"source_path": "/a.jpg"}, 0.5),
                _row("img2", {"source_path": "/b.jpg"}, 0.95),
                _row("img3", {"source_path": "/c.jpg"}, 0.7),
            ],
        )
        monkeypatch.setattr("media.search.store.query_segments", lambda vec, k, s: [])

        hits = search_media("x", settings=None, media_type="any", top_k=2)

        assert [hit.media_id for hit in hits] == ["img2", "img3"]
