"""Unit tests for media/captioning.py -- Ollama vision-model captioning,
fail-open when unconfigured or the call fails.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from media.captioning import generate_caption


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"media_vision_model": ""}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def keyframe_path(tmp_path: Path) -> Path:
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"\xff\xd8\xff fake jpeg bytes")
    return path


class TestGenerateCaption:
    def test_returns_none_when_no_vision_model_configured(self, keyframe_path: Path):
        assert generate_caption(keyframe_path, "some transcript", _settings()) is None

    def test_returns_stripped_caption_on_success(self, monkeypatch, keyframe_path: Path):
        fake_client = MagicMock()
        fake_client.chat.return_value = {"message": {"content": "  A crane lifts a steel beam.  "}}
        monkeypatch.setattr("media.captioning.get_ollama_client", lambda settings: fake_client)

        caption = generate_caption(
            keyframe_path, "someone shouts clear", _settings(media_vision_model="llava")
        )

        assert caption == "A crane lifts a steel beam."
        _, kwargs = fake_client.chat.call_args
        assert kwargs["model"] == "llava"
        assert kwargs["messages"][1]["images"] == [keyframe_path.read_bytes()]

    def test_fails_open_to_none_on_connection_error(self, monkeypatch, keyframe_path: Path):
        fake_client = MagicMock()
        fake_client.chat.side_effect = ConnectionError("ollama not running")
        monkeypatch.setattr("media.captioning.get_ollama_client", lambda settings: fake_client)

        assert generate_caption(keyframe_path, "", _settings(media_vision_model="llava")) is None

    def test_returns_none_for_a_blank_response(self, monkeypatch, keyframe_path: Path):
        fake_client = MagicMock()
        fake_client.chat.return_value = {"message": {"content": "   "}}
        monkeypatch.setattr("media.captioning.get_ollama_client", lambda settings: fake_client)

        assert generate_caption(keyframe_path, "", _settings(media_vision_model="llava")) is None
