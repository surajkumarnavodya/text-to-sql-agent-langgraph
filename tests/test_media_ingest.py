"""Unit tests for media/ingest.py -- file-type sniffing, size/type
validation, content hashing, and the image/video pipeline dispatch,
including the pre-ingestion moderation gate's dedupe short-circuit and
reject-blocks-storage behavior.

The actual embedding/keyframe/OCR/ASR/captioning/moderation calls are
mocked -- these test ingest.py's own orchestration logic (validation,
hashing, skip-if-unchanged, and what gets passed to media/store.py and
moderation/store.py), not the underlying model/provider behavior (covered
by each module's own test file).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from media.exceptions import MediaFileTooLargeError, UnsupportedMediaTypeError
from media.ingest import _content_hash, _sniff_media_type, ingest_file
from media.keyframes import KeyframeSegment
from media.transcription import TranscriptSegment
from moderation.store import MediaAssetRecord
from moderation.types import ModerationDecision


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "media_max_file_mb": 200,
        "media_scene_detect_threshold": 27.0,
        "media_image_tile_threshold_px": 2048,
    }
    base.update(overrides)
    return Settings(**base)


def _mock_moderation_plumbing(monkeypatch, *, existing=None, decision=None):
    """Wires the moderation-store/gate call sites `media/ingest.py` uses,
    so tests never need a real SQL Server or moderation provider.

    Args:
        existing: what `get_asset_by_hash` should return (None for "not
            seen before", or a `MediaAssetRecord` to simulate a dedupe hit).
        decision: what `moderate_chunks` should return for genuinely-new
            content -- defaults to a passing decision.
    """
    monkeypatch.setattr("media.ingest.get_moderation_engine", lambda settings: object())
    monkeypatch.setattr("media.ingest.ensure_schema", lambda engine: None)
    monkeypatch.setattr("media.ingest.get_asset_by_hash", lambda engine, file_hash: existing)
    recorded: list[dict] = []
    monkeypatch.setattr(
        "media.ingest.record_asset",
        lambda engine, file_hash, media_type, status, checks, **kw: (
            recorded.append(
                {"file_hash": file_hash, "media_type": media_type, "status": status, **kw}
            ),
            "fake-asset-id",
        )[1],
    )
    monkeypatch.setattr(
        "media.ingest.moderate_chunks",
        lambda asset_hash, chunks, settings: decision or ModerationDecision(status="passed"),
    )
    return recorded


class TestSniffMediaType:
    def test_recognizes_jpeg(self):
        assert _sniff_media_type(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image"

    def test_recognizes_png(self):
        assert _sniff_media_type(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") == "image"

    def test_recognizes_mp4_via_ftyp_box(self):
        assert _sniff_media_type(b"\x00\x00\x00\x18ftypmp42") == "video"

    def test_recognizes_webm_via_ebml_header(self):
        assert _sniff_media_type(b"\x1a\x45\xdf\xa3\x9f\x42\x86") == "video"

    def test_recognizes_riff_webp_as_image_and_riff_avi_as_video(self):
        assert _sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image"
        assert _sniff_media_type(b"RIFF\x00\x00\x00\x00AVI LIST") == "video"

    def test_returns_none_for_unrecognized_content(self):
        assert _sniff_media_type(b"not a real media file header") is None


class TestContentHash:
    def test_deterministic_for_identical_content(self, tmp_path: Path):
        file1 = tmp_path / "a.bin"
        file2 = tmp_path / "b.bin"
        file1.write_bytes(b"same content")
        file2.write_bytes(b"same content")
        assert _content_hash(file1) == _content_hash(file2)

    def test_differs_for_different_content(self, tmp_path: Path):
        file1 = tmp_path / "a.bin"
        file2 = tmp_path / "b.bin"
        file1.write_bytes(b"content one")
        file2.write_bytes(b"content two")
        assert _content_hash(file1) != _content_hash(file2)


class TestIngestFileValidation:
    def test_rejects_a_file_over_the_size_cap(self, tmp_path: Path):
        big_file = tmp_path / "big.jpg"
        big_file.write_bytes(b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024))  # ~2MB
        settings = _settings(media_max_file_mb=1)

        with pytest.raises(MediaFileTooLargeError):
            ingest_file(big_file, settings)

    def test_rejects_unsupported_content(self, tmp_path: Path):
        bad_file = tmp_path / "notes.txt"
        bad_file.write_text("just some plain text, not media")

        with pytest.raises(UnsupportedMediaTypeError):
            ingest_file(bad_file, _settings())


class TestIngestImage:
    def test_ingests_a_new_image_and_stores_its_embedding(self, monkeypatch, tmp_path: Path):
        from PIL import Image

        image_path = tmp_path / "photo.jpg"
        Image.new("RGB", (10, 6)).save(image_path)
        settings = _settings()

        recorded = _mock_moderation_plumbing(monkeypatch)
        monkeypatch.setattr("media.ingest.embedding.embed_image", lambda path, s: [0.1, 0.2])
        stored: dict[str, object] = {}
        monkeypatch.setattr(
            "media.ingest.store.upsert_image",
            lambda media_id, vector, *, source_path, width, height, settings: stored.update(
                media_id=media_id, vector=vector, width=width, height=height
            ),
        )

        result = ingest_file(image_path, settings)

        assert result.media_type == "image"
        assert result.segments_indexed == 0
        assert result.moderation_status == "passed"
        assert stored["width"] == 10
        assert stored["height"] == 6
        assert stored["vector"] == [0.1, 0.2]
        assert recorded[0]["status"] == "passed"
        assert recorded[0]["vector_ids"] == [result.media_id]

    def test_skips_an_already_indexed_image_unless_forced(self, monkeypatch, tmp_path: Path):
        from PIL import Image

        image_path = tmp_path / "photo.jpg"
        Image.new("RGB", (10, 6)).save(image_path)
        settings = _settings()

        existing = MediaAssetRecord(
            asset_id="existing-id",
            file_hash=_content_hash(image_path),
            media_type="image",
            moderation_status="passed",
            moderation_checks={},
            chunk_count=0,
            vector_ids=[_content_hash(image_path)],
        )
        _mock_moderation_plumbing(monkeypatch, existing=existing)

        def _fail(*args, **kwargs):
            raise AssertionError("embed_image must not be called for an unchanged file")

        monkeypatch.setattr("media.ingest.embedding.embed_image", _fail)

        result = ingest_file(image_path, settings)

        assert result.segments_indexed == 0
        assert result.moderation_status == "passed"

    def test_a_moderation_rejection_never_calls_store_upsert(self, monkeypatch, tmp_path: Path):
        from PIL import Image

        image_path = tmp_path / "photo.jpg"
        Image.new("RGB", (10, 6)).save(image_path)
        settings = _settings()

        recorded = _mock_moderation_plumbing(
            monkeypatch,
            decision=ModerationDecision(status="rejected", triggering_categories=("violence",)),
        )

        def _fail(*args, **kwargs):
            raise AssertionError("upsert_image must not be called for a rejected asset")

        monkeypatch.setattr("media.ingest.store.upsert_image", _fail)
        monkeypatch.setattr("media.ingest.embedding.embed_image", _fail)

        result = ingest_file(image_path, settings)

        assert result.moderation_status == "rejected"
        assert result.segments_indexed == 0
        assert recorded[0]["status"] == "rejected"
        assert "vector_ids" not in recorded[0]  # never set for a rejected asset


class TestIngestVideo:
    def _keyframe(self, thumb_path: Path) -> KeyframeSegment:
        return KeyframeSegment(segment_start=0.0, segment_end=5.0, thumbnail_path=thumb_path)

    def _wire_video_pipeline(self, monkeypatch, thumb_path: Path):
        monkeypatch.setattr("media.ingest._probe_video_metadata", lambda path: (1920, 1080, 30.0))
        monkeypatch.setattr(
            "media.ingest.extract_keyframes",
            lambda path, video_hash, threshold: [self._keyframe(thumb_path)],
        )
        monkeypatch.setattr(
            "media.ingest.transcribe_video",
            lambda path, settings: [TranscriptSegment(start=1.0, end=3.0, text="a crane lifts a beam")],
        )
        monkeypatch.setattr("media.ingest.extract_text", lambda path: "")
        monkeypatch.setattr("media.ingest.generate_caption", lambda path, ctx, s: None)

    def test_ingests_each_keyframe_as_one_segment(self, monkeypatch, tmp_path: Path):
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
        settings = _settings()

        thumb_path = tmp_path / "thumb0.jpg"
        thumb_path.write_bytes(b"\xff\xd8\xff")

        recorded = _mock_moderation_plumbing(monkeypatch)
        self._wire_video_pipeline(monkeypatch, thumb_path)
        monkeypatch.setattr("media.ingest.embedding.embed_text", lambda text, s: [0.5])
        monkeypatch.setattr("media.ingest.embedding.embed_image", lambda path, s: [0.6])

        calls = []
        monkeypatch.setattr(
            "media.ingest.store.upsert_segment",
            lambda segment_id, **kwargs: calls.append((segment_id, kwargs)),
        )

        result = ingest_file(video_path, settings)

        assert result.media_type == "video"
        assert result.segments_indexed == 1
        assert result.moderation_status == "passed"
        segment_id, kwargs = calls[0]
        assert kwargs["video_hash"] == result.media_id
        assert kwargs["caption"] == "a crane lifts a beam"
        assert kwargs["text_embedding"] == [0.5]
        assert kwargs["image_embedding"] == [0.6]
        assert recorded[0]["vector_ids"] == [segment_id]
        assert recorded[0]["chunk_count"] == 1

    def test_skips_an_already_indexed_video_unless_forced(self, monkeypatch, tmp_path: Path):
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
        settings = _settings()

        existing = MediaAssetRecord(
            asset_id="existing-id",
            file_hash=_content_hash(video_path),
            media_type="video",
            moderation_status="passed",
            moderation_checks={},
            chunk_count=3,
            vector_ids=["a", "b", "c"],
        )
        _mock_moderation_plumbing(monkeypatch, existing=existing)

        def _fail(*args, **kwargs):
            raise AssertionError("extract_keyframes must not run for an unchanged video")

        monkeypatch.setattr("media.ingest.extract_keyframes", _fail)

        result = ingest_file(video_path, settings)

        assert result.segments_indexed == 3

    def test_a_rejected_segment_blocks_the_whole_video(self, monkeypatch, tmp_path: Path):
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
        settings = _settings()

        thumb_path = tmp_path / "thumb0.jpg"
        thumb_path.write_bytes(b"\xff\xd8\xff")

        recorded = _mock_moderation_plumbing(
            monkeypatch,
            decision=ModerationDecision(status="rejected", triggering_categories=("hate",)),
        )
        self._wire_video_pipeline(monkeypatch, thumb_path)

        def _fail(*args, **kwargs):
            raise AssertionError("upsert_segment must not be called for a rejected video")

        monkeypatch.setattr("media.ingest.store.upsert_segment", _fail)

        result = ingest_file(video_path, settings)

        assert result.moderation_status == "rejected"
        assert result.segments_indexed == 0
        assert recorded[0]["status"] == "rejected"
