"""Unit tests for media/keyframes.py -- scene-change keyframe extraction.

`cv2`/`scenedetect` are real, installed dependencies (not optional here),
so these patch their actual functions/classes directly rather than faking
module imports -- the SUT's own local `import cv2`/`from scenedetect
import ...` inside `extract_keyframes` resolves to the same (now patched)
module objects.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from media.keyframes import extract_keyframes


class _FakeTimecode:
    def __init__(self, seconds: float):
        self._seconds = seconds

    def get_seconds(self) -> float:
        return self._seconds


class TestExtractKeyframes:
    def test_one_segment_per_detected_scene(self, monkeypatch, tmp_path: Path):
        import cv2
        import scenedetect

        fake_scene_manager = MagicMock()
        fake_scene_manager.get_scene_list.return_value = [
            (_FakeTimecode(0.0), _FakeTimecode(5.0)),
            (_FakeTimecode(5.0), _FakeTimecode(12.0)),
        ]
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: fake_scene_manager)
        monkeypatch.setattr(scenedetect, "open_video", lambda path: MagicMock())
        monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold: MagicMock())

        fake_capture = MagicMock()
        fake_capture.read.return_value = (True, "fake-frame-array")
        monkeypatch.setattr(cv2, "VideoCapture", lambda path: fake_capture)
        written = []
        monkeypatch.setattr(cv2, "imwrite", lambda path, frame: written.append(path))

        segments = extract_keyframes(tmp_path / "video.mp4", "vhash123", threshold=27.0)

        assert len(segments) == 2
        assert segments[0].segment_start == 0.0
        assert segments[0].segment_end == 5.0
        assert segments[1].segment_start == 5.0
        assert len(written) == 2
        fake_capture.release.assert_called_once()

    def test_no_detected_scenes_falls_back_to_one_whole_video_segment(
        self, monkeypatch, tmp_path: Path
    ):
        import cv2
        import scenedetect

        fake_scene_manager = MagicMock()
        fake_scene_manager.get_scene_list.return_value = []
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: fake_scene_manager)
        fake_video = MagicMock()
        fake_video.duration = _FakeTimecode(20.0)
        monkeypatch.setattr(scenedetect, "open_video", lambda path: fake_video)
        monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold: MagicMock())

        fake_capture = MagicMock()
        fake_capture.read.return_value = (True, "fake-frame-array")
        monkeypatch.setattr(cv2, "VideoCapture", lambda path: fake_capture)
        monkeypatch.setattr(cv2, "imwrite", lambda path, frame: None)

        segments = extract_keyframes(tmp_path / "video.mp4", "vhash123", threshold=27.0)

        assert len(segments) == 1
        assert segments[0].segment_start == 0.0
        assert segments[0].segment_end == 20.0

    def test_unreadable_frame_is_skipped_not_fatal(self, monkeypatch, tmp_path: Path):
        import cv2
        import scenedetect

        fake_scene_manager = MagicMock()
        fake_scene_manager.get_scene_list.return_value = [
            (_FakeTimecode(0.0), _FakeTimecode(5.0)),
            (_FakeTimecode(5.0), _FakeTimecode(10.0)),
        ]
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: fake_scene_manager)
        monkeypatch.setattr(scenedetect, "open_video", lambda path: MagicMock())
        monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold: MagicMock())

        fake_capture = MagicMock()
        fake_capture.read.side_effect = [(False, None), (True, "frame")]
        monkeypatch.setattr(cv2, "VideoCapture", lambda path: fake_capture)
        monkeypatch.setattr(cv2, "imwrite", lambda path, frame: None)

        segments = extract_keyframes(tmp_path / "video.mp4", "vhash123", threshold=27.0)

        assert len(segments) == 1
        assert segments[0].segment_start == 5.0
