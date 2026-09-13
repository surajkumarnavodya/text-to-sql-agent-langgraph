"""Unit tests for moderation/provider.py -- the Azure Content Safety REST
call shape and category mapping. `httpx.post` is mocked; no real Azure
resource is ever contacted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from moderation.exceptions import ModerationNotConfiguredError
from moderation.provider import analyze_chunk
from moderation.types import ModerationChunk
from security.secrets import SecretStr


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "azure_content_safety_endpoint": "https://example.cognitiveservices.azure.com",
        "azure_content_safety_key": SecretStr("fake-key"),
    }
    base.update(overrides)
    return Settings(**base)


class TestNotConfigured:
    def test_raises_when_endpoint_missing(self):
        settings = _settings(azure_content_safety_endpoint="")
        chunk = ModerationChunk(chunk_index=0, content_type="text", text="hello")
        with pytest.raises(ModerationNotConfiguredError):
            analyze_chunk(chunk, settings)

    def test_raises_when_key_missing(self):
        settings = _settings(azure_content_safety_key=None)
        chunk = ModerationChunk(chunk_index=0, content_type="text", text="hello")
        with pytest.raises(ModerationNotConfiguredError):
            analyze_chunk(chunk, settings)


class TestAnalyzeTextChunk:
    def test_calls_text_analyze_endpoint_and_maps_categories(self, monkeypatch):
        captured = {}

        def _fake_post(url, headers, json, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse(
                {
                    "categoriesAnalysis": [
                        {"category": "Hate", "severity": 0},
                        {"category": "Violence", "severity": 4},
                    ]
                }
            )

        monkeypatch.setattr("moderation.provider.httpx.post", _fake_post)
        chunk = ModerationChunk(chunk_index=0, content_type="text", text="some extracted text")

        results = analyze_chunk(chunk, _settings())

        assert "text:analyze" in captured["url"]
        assert captured["json"]["text"] == "some extracted text"
        assert captured["headers"]["Ocp-Apim-Subscription-Key"] == "fake-key"
        by_category = {r.category: r for r in results}
        assert by_category["hate"].severity == 0
        assert by_category["violence"].severity == 4
        # analyze_chunk never applies the severity threshold itself --
        # that policy decision belongs to moderation/gate.py.
        assert all(r.triggered is False for r in results)

    def test_blank_text_short_circuits_without_a_network_call(self, monkeypatch):
        def _fail(*args, **kwargs):
            raise AssertionError("httpx.post must not be called for blank text")

        monkeypatch.setattr("moderation.provider.httpx.post", _fail)
        chunk = ModerationChunk(chunk_index=0, content_type="text", text="   ")

        results = analyze_chunk(chunk, _settings())

        assert all(r.severity == 0 and r.triggered is False for r in results)


class TestAnalyzeImageChunk:
    def test_calls_image_analyze_endpoint_with_base64_content(self, monkeypatch, tmp_path: Path):
        image_path = tmp_path / "frame.jpg"
        image_path.write_bytes(b"\xff\xd8\xff fake jpeg bytes")
        captured = {}

        def _fake_post(url, headers, json, timeout):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse({"categoriesAnalysis": [{"category": "Sexual", "severity": 2}]})

        monkeypatch.setattr("moderation.provider.httpx.post", _fake_post)
        chunk = ModerationChunk(chunk_index=0, content_type="image", image_path=image_path)

        results = analyze_chunk(chunk, _settings())

        assert "image:analyze" in captured["url"]
        assert "content" in captured["json"]["image"]
        assert results[0].category == "sexual"
        assert results[0].severity == 2

    def test_raises_value_error_when_image_path_missing(self):
        settings = _settings()
        chunk = ModerationChunk(chunk_index=0, content_type="image", image_path=None)
        with pytest.raises(ValueError):
            analyze_chunk(chunk, settings)
