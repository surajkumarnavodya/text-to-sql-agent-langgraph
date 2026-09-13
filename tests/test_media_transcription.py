"""Unit tests for media/transcription.py -- video-audio ASR (reusing
voice/stt.py's Whisper model loader) and segment-range bucketing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from media.transcription import TranscriptSegment, transcribe_video, transcript_for_range


def _settings() -> Settings:
    return Settings()


@dataclass
class _FakeWhisperSegment:
    start: float
    end: float
    text: str


class _FakeWhisperModel:
    def __init__(self, segments: list[_FakeWhisperSegment]):
        self._segments = segments

    def transcribe(self, _path, **_kwargs):
        return (self._segments, object())


class TestTranscribeVideo:
    def test_returns_timestamped_non_empty_segments(self, monkeypatch, tmp_path: Path):
        fake_model = _FakeWhisperModel(
            [
                _FakeWhisperSegment(0.0, 2.0, " clear the area "),
                _FakeWhisperSegment(2.0, 3.0, "   "),
            ]
        )
        monkeypatch.setattr("media.transcription.get_whisper_model", lambda settings: fake_model)

        segments = transcribe_video(tmp_path / "video.mp4", _settings())

        assert segments == [TranscriptSegment(start=0.0, end=2.0, text="clear the area")]

    def test_fails_open_to_empty_list_on_decode_error(self, monkeypatch, tmp_path: Path):
        def _raise(settings):
            raise RuntimeError("no audio track")

        monkeypatch.setattr("media.transcription.get_whisper_model", _raise)

        assert transcribe_video(tmp_path / "video.mp4", _settings()) == []


class TestTranscriptForRange:
    def test_joins_segments_whose_midpoint_falls_in_range(self):
        segments = [
            TranscriptSegment(start=0.0, end=2.0, text="first"),
            TranscriptSegment(start=4.0, end=6.0, text="second"),
            TranscriptSegment(start=10.0, end=12.0, text="third"),
        ]

        assert transcript_for_range(segments, 0.0, 7.0) == "first second"
        assert transcript_for_range(segments, 9.0, 13.0) == "third"

    def test_returns_empty_string_when_nothing_overlaps(self):
        segments = [TranscriptSegment(start=0.0, end=2.0, text="first")]
        assert transcript_for_range(segments, 100.0, 200.0) == ""
