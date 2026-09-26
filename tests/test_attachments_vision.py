"""Unit tests for attachments/vision.py -- the one function that actually
sends image bytes to a configured Ollama vision model
(`attachments.graph.call_model_node` is the only caller).

Uses a fake Ollama client that records exactly what it was called with,
per this feature's own requirement: a test asserting the frontend renders
something is not proof the model ever saw the image -- only inspecting the
literal bytes handed to the client call is.
"""

from __future__ import annotations

from attachments.vision import describe_images, model_capabilities, supports_images
from config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class _FakeVisionClient:
    """Records every `.chat(...)` call verbatim -- the real
    `ollama.Client.chat` signature `attachments.vision.describe_images`
    calls, not a paraphrase of it."""

    def __init__(self, response_content: str | None = "The image contains readable text."):
        self.received_calls: list[dict] = []
        self._response_content = response_content

    def chat(self, *, model, messages, options=None):
        self.received_calls.append({"model": model, "messages": messages, "options": options})
        return {"message": {"content": self._response_content}}


class _ErroringVisionClient:
    def chat(self, **kwargs):
        raise ConnectionError("vision model unreachable")


class TestSupportsImagesAndCapabilities:
    def test_false_when_no_vision_model_configured(self):
        assert supports_images(_settings(media_vision_model="")) is False

    def test_true_when_vision_model_configured(self):
        assert supports_images(_settings(media_vision_model="llava")) is True

    def test_capabilities_reflect_live_settings_not_a_hardcoded_dict(self):
        off = model_capabilities(_settings(media_vision_model=""))
        assert off["supports_images"] is False
        assert off["vision_model"] is None

        on = model_capabilities(_settings(media_vision_model="qwen3.8:27b"))
        assert on["supports_images"] is True
        assert on["vision_model"] == "qwen3.8:27b"
        assert on["supports_data_urls"] is True


class TestDescribeImages:
    def test_sends_the_actual_image_bytes_and_question_to_the_configured_model(self, monkeypatch):
        """The core assertion this whole module exists to prove: the exact
        bytes passed in are what reaches the client call's `images` field
        -- not a filename, not a thumbnail URL, not attachment metadata."""
        fake_client = _FakeVisionClient()
        monkeypatch.setattr("attachments.vision.get_ollama_client", lambda settings: fake_client)
        settings = _settings(media_vision_model="qwen3.8:27b")
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"not-a-real-png-but-a-real-bytes-object"

        answer = describe_images([image_bytes], "What does this image say?", "", settings)

        assert answer == "The image contains readable text."
        assert len(fake_client.received_calls) == 1
        call = fake_client.received_calls[0]
        assert call["model"] == "qwen3.8:27b"
        user_message = next(m for m in call["messages"] if m["role"] == "user")
        assert user_message["content"] == "What does this image say?"
        assert user_message["images"] == [image_bytes]

    def test_returns_none_without_calling_the_client_when_no_vision_model_configured(
        self, monkeypatch
    ):
        fake_client = _FakeVisionClient()
        monkeypatch.setattr("attachments.vision.get_ollama_client", lambda settings: fake_client)
        settings = _settings(media_vision_model="")

        assert describe_images([b"real-bytes"], "describe this", "", settings) is None
        assert fake_client.received_calls == []

    def test_returns_none_without_calling_the_client_when_no_images_given(self, monkeypatch):
        fake_client = _FakeVisionClient()
        monkeypatch.setattr("attachments.vision.get_ollama_client", lambda settings: fake_client)
        settings = _settings(media_vision_model="qwen3.8:27b")

        assert describe_images([], "describe this", "", settings) is None
        assert fake_client.received_calls == []

    def test_folds_extra_document_context_into_the_prompt_when_given(self, monkeypatch):
        fake_client = _FakeVisionClient()
        monkeypatch.setattr("attachments.vision.get_ollama_client", lambda settings: fake_client)
        settings = _settings(media_vision_model="qwen3.8:27b")

        describe_images([b"img"], "What is this?", "Doc context: total is 42", settings)

        user_message = next(
            m for m in fake_client.received_calls[0]["messages"] if m["role"] == "user"
        )
        assert "Doc context: total is 42" in user_message["content"]
        assert "untrusted data" in user_message["content"]

    def test_fails_open_to_none_on_a_model_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "attachments.vision.get_ollama_client", lambda settings: _ErroringVisionClient()
        )
        settings = _settings(media_vision_model="qwen3.8:27b")

        assert describe_images([b"img"], "q", "", settings) is None

    def test_returns_none_when_the_model_answers_with_only_whitespace(self, monkeypatch):
        fake_client = _FakeVisionClient(response_content="   ")
        monkeypatch.setattr("attachments.vision.get_ollama_client", lambda settings: fake_client)
        settings = _settings(media_vision_model="qwen3.8:27b")

        assert describe_images([b"img"], "q", "", settings) is None
