"""Unit tests for media/embedding.py -- the local-CLIP embedding provider.

The real `SentenceTransformer` model is never loaded here (mocked at
`media.embedding._load_clip_model`'s own module-level `cache`) -- these
check the provider-dispatch/caching *logic*, not embedding quality.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from media.embedding import embed_image, embed_text
from media.exceptions import MediaEmbeddingModelNotFoundError


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"media_embedding_provider": "local_clip"}
    base.update(overrides)
    return Settings(**base)


class TestEmbedText:
    def test_dispatches_to_local_clip_and_normalizes_to_python_floats(self, monkeypatch):
        fake_model = MagicMock()
        fake_model.encode.return_value = [0.1, 0.2, 0.3]
        monkeypatch.setattr("media.embedding._load_clip_model", lambda name: fake_model)

        vector = embed_text("a photo of a cat", _settings())

        assert vector == [0.1, 0.2, 0.3]
        assert all(isinstance(x, float) for x in vector)
        fake_model.encode.assert_called_once()

    def test_unknown_provider_raises_key_error(self, monkeypatch):
        # Settings.media_embedding_provider is a Literal["local_clip"], so a
        # bad value can't reach this in practice via normal config loading --
        # this documents what happens if the provider dict and the Literal
        # type ever drift out of sync.
        settings = _settings()
        object.__setattr__(settings, "media_embedding_provider", "nonexistent")
        with pytest.raises(KeyError):
            embed_text("x", settings)


class TestEmbedImage:
    def test_loads_and_converts_image_to_rgb_before_encoding(self, monkeypatch, tmp_path: Path):
        from PIL import Image

        image_path = tmp_path / "photo.jpg"
        Image.new("RGB", (4, 4), color="red").save(image_path)

        fake_model = MagicMock()
        fake_model.encode.return_value = [0.5, 0.6]
        monkeypatch.setattr("media.embedding._load_clip_model", lambda name: fake_model)

        vector = embed_image(image_path, _settings())

        assert vector == [0.5, 0.6]
        fake_model.encode.assert_called_once()


class TestLoadClipModel:
    def test_load_failure_is_wrapped_as_media_embedding_model_not_found_error(self, monkeypatch):
        import media.embedding as embedding_module

        embedding_module._load_clip_model.cache_clear()

        def _raise(_name: str):
            raise OSError("no network, model not cached")

        monkeypatch.setattr("sentence_transformers.SentenceTransformer", _raise)

        with pytest.raises(MediaEmbeddingModelNotFoundError):
            embedding_module._load_clip_model("clip-ViT-B-32")

        embedding_module._load_clip_model.cache_clear()
