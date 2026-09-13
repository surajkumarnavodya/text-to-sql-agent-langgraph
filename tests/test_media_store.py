"""Unit tests for media/store.py -- Chroma collection management for the
media-search library.

Mirrors tests/test_golden_examples.py's mocked-Chroma style: the Chroma
collection is mocked so these check the storage/retrieval *logic* (upsert
shape, id-based lookup, fail-open on an empty collection), not a real
embedding backend or on-disk Chroma index.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from media.store import (
    get_image_metadata,
    get_segment_metadata,
    is_image_indexed,
    is_video_indexed,
    query_images,
    query_segments,
    upsert_image,
    upsert_segment,
)


class TestUpsertImage:
    def test_upserts_with_id_and_metadata(self, monkeypatch):
        mock_collection = MagicMock()
        monkeypatch.setattr("media.store.get_image_collection", lambda settings: mock_collection)

        upsert_image(
            "abc123", [0.1, 0.2], source_path="/lib/photo.jpg", width=100, height=50, settings=None
        )

        mock_collection.upsert.assert_called_once_with(
            ids=["abc123"],
            embeddings=[[0.1, 0.2]],
            metadatas=[
                {"source_path": "/lib/photo.jpg", "media_type": "image", "width": 100, "height": 50}
            ],
        )


class TestUpsertSegment:
    def test_requires_at_least_one_embedding(self):
        with pytest.raises(ValueError):
            upsert_segment(
                "seg1",
                video_hash="vhash",
                text_embedding=None,
                image_embedding=None,
                source_path="/lib/video.mp4",
                thumbnail_path="/thumbs/seg1.jpg",
                segment_start=0.0,
                segment_end=5.0,
                caption="a caption",
                video_width=1920,
                video_height=1080,
                video_duration=30.0,
                settings=None,
            )

    def test_upserts_both_records_sharing_segment_id(self, monkeypatch):
        mock_collection = MagicMock()
        monkeypatch.setattr("media.store.get_segment_collection", lambda settings: mock_collection)

        upsert_segment(
            "seg1",
            video_hash="vhash",
            text_embedding=[0.1],
            image_embedding=[0.2],
            source_path="/lib/video.mp4",
            thumbnail_path="/thumbs/seg1.jpg",
            segment_start=1.0,
            segment_end=4.0,
            caption="a caption",
            video_width=1920,
            video_height=1080,
            video_duration=30.0,
            settings=None,
        )

        _, kwargs = mock_collection.upsert.call_args
        assert kwargs["ids"] == ["seg1:text", "seg1:image"]
        assert kwargs["embeddings"] == [[0.1], [0.2]]
        assert kwargs["metadatas"][0]["segment_id"] == "seg1"
        assert kwargs["metadatas"][0]["modality"] == "text"
        assert kwargs["metadatas"][1]["modality"] == "image"
        assert kwargs["metadatas"][0]["thumbnail_path"] == "/thumbs/seg1.jpg"

    def test_upserts_only_the_given_embedding(self, monkeypatch):
        mock_collection = MagicMock()
        monkeypatch.setattr("media.store.get_segment_collection", lambda settings: mock_collection)

        upsert_segment(
            "seg1",
            video_hash="vhash",
            text_embedding=None,
            image_embedding=[0.2],
            source_path="/lib/video.mp4",
            thumbnail_path="/thumbs/seg1.jpg",
            segment_start=1.0,
            segment_end=4.0,
            caption="a caption",
            video_width=1920,
            video_height=1080,
            video_duration=30.0,
            settings=None,
        )

        _, kwargs = mock_collection.upsert.call_args
        assert kwargs["ids"] == ["seg1:image"]


class TestQuery:
    def test_query_images_returns_empty_on_empty_collection(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        monkeypatch.setattr("media.store.get_image_collection", lambda settings: mock_collection)

        assert query_images([0.1], 5, None) == []
        mock_collection.query.assert_not_called()

    def test_query_images_zips_ids_metadata_and_similarity(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 2
        mock_collection.query.return_value = {
            "ids": [["img1", "img2"]],
            "metadatas": [[{"media_type": "image"}, {"media_type": "image"}]],
            "distances": [[0.1, 0.4]],
        }
        monkeypatch.setattr("media.store.get_image_collection", lambda settings: mock_collection)

        hits = query_images([0.1], 5, None)

        assert hits[0] == {"id": "img1", "metadata": {"media_type": "image"}, "similarity": 0.9}
        assert hits[1]["similarity"] == 0.6

    def test_query_segments_returns_empty_on_empty_collection(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        monkeypatch.setattr("media.store.get_segment_collection", lambda settings: mock_collection)

        assert query_segments([0.1], 5, None) == []


class TestMetadataLookup:
    def test_get_image_metadata_returns_none_when_unknown(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"metadatas": []}
        monkeypatch.setattr("media.store.get_image_collection", lambda settings: mock_collection)

        assert get_image_metadata("unknown", None) is None

    def test_get_image_metadata_returns_stored_metadata(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"metadatas": [{"source_path": "/x.jpg"}]}
        monkeypatch.setattr("media.store.get_image_collection", lambda settings: mock_collection)

        assert get_image_metadata("abc", None) == {"source_path": "/x.jpg"}

    def test_get_segment_metadata_looks_up_by_shared_segment_id(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"metadatas": [{"thumbnail_path": "/t.jpg"}]}
        monkeypatch.setattr("media.store.get_segment_collection", lambda settings: mock_collection)

        result = get_segment_metadata("seg1", None)

        assert result == {"thumbnail_path": "/t.jpg"}
        mock_collection.get.assert_called_once_with(where={"segment_id": "seg1"}, limit=1)


class TestIndexedChecks:
    def test_is_image_indexed_true_when_metadata_exists(self, monkeypatch):
        monkeypatch.setattr("media.store.get_image_metadata", lambda media_id, settings: {"x": 1})
        assert is_image_indexed("abc", None) is True

    def test_is_image_indexed_false_when_absent(self, monkeypatch):
        monkeypatch.setattr("media.store.get_image_metadata", lambda media_id, settings: None)
        assert is_image_indexed("abc", None) is False

    def test_is_video_indexed_checks_video_hash_metadata_field(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": ["seg1:text"]}
        monkeypatch.setattr("media.store.get_segment_collection", lambda settings: mock_collection)

        assert is_video_indexed("vhash", None) is True
        mock_collection.get.assert_called_once_with(where={"video_hash": "vhash"}, limit=1)

    def test_is_video_indexed_false_when_no_match(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": []}
        monkeypatch.setattr("media.store.get_segment_collection", lambda settings: mock_collection)

        assert is_video_indexed("vhash", None) is False
