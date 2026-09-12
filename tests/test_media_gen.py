"""Unit tests for media_gen/ (IMA Studio image/video/audio generation).

Fully mocked -- no real network calls. The mocked response shapes here are
not invented: they match the real, verified contract documented in
media_gen/client.py's module docstring (confirmed against IMA's own
`github.com/imastuido/ima-all-ai` reference implementation) -- a nested
product-list tree, `{"code": 0|200, "data": {"id": ...}}` on create, and
`{"code": ..., "data": {"medias": [{"resource_status": ...}]}}` on poll.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_gen.audio import generate_audio
from media_gen.client import (
    IMAClient,
    MediaGenerationError,
    MediaGenerationNotConfiguredError,
    _extract_model_params,
    _find_first_model_leaf,
    build_create_payload,
    create_and_poll,
    get_ima_client,
)
from media_gen.image import generate_image
from media_gen.video import generate_video

# One realistic product-list leaf (`type == "3"`), shaped like a real IMA
# response -- see references/models/product-list-and-create-params.md.
_SAMPLE_LEAF = {
    "type": "3",
    "id": "v1-seedream",
    "model_id": "doubao-seedream-4.5",
    "name": "SeeDream 4.5",
    "form_config": [
        {"field": "size", "value": "2K", "is_ui_virtual": False},
        {"field": "quality", "value": "std", "is_ui_virtual": True},  # skipped -- virtual
    ],
    "credit_rules": [
        {"attribute_id": 101, "points": 4, "attributes": {"default": "enabled"}},
        {"attribute_id": 102, "points": 8, "attributes": {"size": "4K"}},
    ],
}


@pytest.fixture
def client() -> IMAClient:
    return IMAClient(api_key="ima_test_key", base_url="https://api.imastudio.test")


class TestIMAClientRequest:
    def test_requires_an_api_key(self):
        with pytest.raises(ValueError):
            IMAClient(api_key="")

    def test_http_error_raises(self, client):
        with patch.object(client.session, "request") as mock_request:
            mock_request.return_value.status_code = 404
            mock_request.return_value.json.side_effect = ValueError
            mock_request.return_value.text = "Not Found"
            with pytest.raises(MediaGenerationError) as exc_info:
                client._request("POST", "/open/v1/tasks/create", json={"prompt": "x"})
            assert exc_info.value.status_code == 404

    def test_business_error_code_raises_even_on_http_200(self, client):
        """The real contract: a 401/business error can arrive with HTTP 200
        -- the response body's own `code` field is the source of truth."""
        with patch.object(client.session, "request") as mock_request:
            mock_request.return_value.status_code = 200
            mock_request.return_value.json.return_value = {"code": 401, "message": "invalid key"}
            with pytest.raises(MediaGenerationError, match="code=401"):
                client._request("POST", "/open/v1/tasks/create", json={"prompt": "x"})

    def test_network_error_is_wrapped(self, client):
        import requests

        with (
            patch.object(client.session, "request", side_effect=requests.ConnectionError("boom")),
            pytest.raises(MediaGenerationError, match="Network error"),
        ):
            client._request("POST", "/open/v1/tasks/create", json={"prompt": "x"})


class TestGetImaClient:
    def test_raises_when_not_configured(self):
        from config.settings import Settings

        settings = Settings(ima_api_key=None)
        with pytest.raises(MediaGenerationNotConfiguredError):
            get_ima_client(settings)

    def test_constructs_when_configured(self):
        from config.settings import Settings
        from security.secrets import SecretStr

        settings = Settings(
            ima_api_key=SecretStr("ima_x"), ima_api_base_url="https://api.imastudio.com"
        )
        client = get_ima_client(settings)
        assert client.api_key == "ima_x"
        assert client.base_url == "https://api.imastudio.com"


class TestFindFirstModelLeaf:
    def test_finds_a_top_level_leaf(self):
        assert _find_first_model_leaf([_SAMPLE_LEAF]) == _SAMPLE_LEAF

    def test_finds_a_nested_leaf(self):
        tree = [{"type": "1", "children": [{"type": "2", "children": [_SAMPLE_LEAF]}]}]
        assert _find_first_model_leaf(tree) == _SAMPLE_LEAF

    def test_returns_none_when_no_leaf(self):
        assert _find_first_model_leaf([{"type": "1", "children": []}]) is None


class TestExtractModelParams:
    def test_selects_the_default_rule_and_skips_virtual_fields(self):
        params = _extract_model_params(_SAMPLE_LEAF)
        assert params["attribute_id"] == 101  # the default=enabled rule, not the 4K one
        assert params["credit"] == 4
        assert params["form_params"] == {"size": "2K"}  # "quality" skipped (is_ui_virtual)

    def test_raises_when_no_credit_rules(self):
        leaf = {**_SAMPLE_LEAF, "credit_rules": []}
        with pytest.raises(MediaGenerationError, match="credit_rules"):
            _extract_model_params(leaf)

    def test_raises_when_attribute_id_is_zero(self):
        leaf = {
            **_SAMPLE_LEAF,
            "credit_rules": [{"attribute_id": 0, "points": 1, "attributes": {}}],
        }
        with pytest.raises(MediaGenerationError, match="attribute_id=0"):
            _extract_model_params(leaf)


class TestBuildCreatePayload:
    def test_matches_the_verified_shape(self):
        model_params = _extract_model_params(_SAMPLE_LEAF)
        payload = build_create_payload("text_to_image", model_params, "a red fox")

        assert payload["task_type"] == "text_to_image"
        assert payload["enable_multi_model"] is False
        inner = payload["parameters"][0]
        assert inner["model_id"] == "doubao-seedream-4.5"
        assert inner["category"] == "text_to_image"
        assert inner["credit"] == 4
        assert inner["attribute_id"] == 101
        assert inner["parameters"]["prompt"] == "a red fox"
        assert inner["parameters"]["n"] == 1
        assert inner["parameters"]["cast"] == {"points": 4, "attribute_id": 101}
        assert inner["parameters"]["size"] == "2K"


class TestCreateAndPoll:
    def test_full_flow_success(self, client):
        with (
            patch.object(client, "get_product_list", return_value=[_SAMPLE_LEAF]),
            patch.object(client, "create_task", return_value="task_123") as mock_create,
            patch.object(
                client,
                "poll_task",
                return_value={"resource_status": 1, "url": "https://cdn.example/img.png"},
            ) as mock_poll,
        ):
            media, model_name = create_and_poll(
                client, task_type="text_to_image", prompt="a red fox"
            )
            assert media["url"] == "https://cdn.example/img.png"
            assert model_name == "SeeDream 4.5"
            mock_create.assert_called_once()
            mock_poll.assert_called_once_with(
                "task_123", interval_seconds=5.0, timeout_seconds=600.0
            )

    def test_raises_when_no_model_available(self, client):
        with (
            patch.object(client, "get_product_list", return_value=[]),
            pytest.raises(MediaGenerationError, match="No IMA model available"),
        ):
            create_and_poll(client, task_type="text_to_image", prompt="a red fox")


class TestIMAClientPollTask:
    def test_resource_status_2_raises_failed(self, client):
        with (
            patch.object(
                client,
                "get_task_detail",
                return_value={
                    "code": 0,
                    "data": {"medias": [{"resource_status": 2, "error_msg": "boom"}]},
                },
            ),
            pytest.raises(MediaGenerationError, match="boom"),
        ):
            client.poll_task("task_123", interval_seconds=0.01, timeout_seconds=1.0)

    def test_resource_status_3_raises_deleted(self, client):
        with (
            patch.object(
                client,
                "get_task_detail",
                return_value={"code": 0, "data": {"medias": [{"resource_status": 3}]}},
            ),
            pytest.raises(MediaGenerationError, match="deleted"),
        ):
            client.poll_task("task_123", interval_seconds=0.01, timeout_seconds=1.0)

    def test_resource_status_1_with_status_failed_raises(self, client):
        with (
            patch.object(
                client,
                "get_task_detail",
                return_value={
                    "code": 0,
                    "data": {
                        "medias": [
                            {
                                "resource_status": 1,
                                "status": "failed",
                                "error_msg": "model rejected",
                            }
                        ]
                    },
                },
            ),
            pytest.raises(MediaGenerationError, match="model rejected"),
        ):
            client.poll_task("task_123", interval_seconds=0.01, timeout_seconds=1.0)

    def test_pending_then_ready_succeeds(self, client):
        responses = [
            {"code": 0, "data": {"medias": [{"resource_status": 0}]}},
            {
                "code": 0,
                "data": {"medias": [{"resource_status": 1, "url": "https://cdn.example/img.png"}]},
            },
        ]
        with (
            patch.object(client, "get_task_detail", side_effect=responses),
            patch("time.sleep", return_value=None),
        ):
            media = client.poll_task("task_123", interval_seconds=0.01, timeout_seconds=5.0)
            assert media["url"] == "https://cdn.example/img.png"

    def test_resource_status_1_without_url_yet_keeps_polling(self, client):
        """Regression test for a real bug caught via a live call: IMA can
        return `resource_status: 1` with `status: "processing"` and no
        url/watermark_url/preview_url well before the asset is actually
        ready -- resource_status==1 alone is NOT the terminal signal."""
        responses = [
            {
                "code": 0,
                "data": {"medias": [{"resource_status": 1, "status": "processing", "url": None}]},
            },
            {
                "code": 0,
                "data": {"medias": [{"resource_status": 1, "url": "https://cdn.example/img.png"}]},
            },
        ]
        with (
            patch.object(client, "get_task_detail", side_effect=responses),
            patch("time.sleep", return_value=None),
        ):
            media = client.poll_task("task_123", interval_seconds=0.01, timeout_seconds=5.0)
            assert media["url"] == "https://cdn.example/img.png"

    def test_timeout_raises(self, client):
        with (
            patch.object(
                client,
                "get_task_detail",
                return_value={"code": 0, "data": {"medias": [{"resource_status": 0}]}},
            ),
            patch("time.sleep", return_value=None),
            patch("time.monotonic", side_effect=[0.0, 0.0, 10.0]),
            pytest.raises(MediaGenerationError, match="timed out"),
        ):
            client.poll_task("task_123", interval_seconds=0.01, timeout_seconds=1.0)


class TestGenerateImage:
    def test_success(self, client):
        with patch(
            "media_gen.image.create_and_poll",
            return_value=({"url": "https://cdn.example/img.png"}, "SeeDream 4.5"),
        ):
            result = generate_image(client, prompt="a bar chart of top merchants")
            assert result.ok
            assert result.url == "https://cdn.example/img.png"
            assert result.model == "SeeDream 4.5"

    def test_failure_is_caught_and_returned_not_raised(self, client):
        with patch("media_gen.image.create_and_poll", side_effect=MediaGenerationError("boom")):
            result = generate_image(client, prompt="anything")
            assert not result.ok
            assert result.status == "failed"
            assert "boom" in result.error


class TestGenerateVideo:
    def test_success(self, client):
        with patch(
            "media_gen.video.create_and_poll",
            return_value=({"url": "https://cdn.example/video.mp4"}, "Wan 2.6"),
        ):
            result = generate_video(client, prompt="a crane lifting a beam")
            assert result.ok
            assert result.url == "https://cdn.example/video.mp4"


class TestDownloadMediaBytes:
    def test_success_returns_bytes_and_content_type(self):
        from media_gen.download import download_media_bytes

        mock_response = type(
            "Resp",
            (),
            {"status_code": 200, "headers": {"Content-Type": "image/png"}, "content": b"abc"},
        )()
        with patch("requests.get", return_value=mock_response):
            data, content_type = download_media_bytes("https://cdn.example/img.png")
        assert data == b"abc"
        assert content_type == "image/png"

    def test_http_error_raises_media_generation_error(self):
        from media_gen.download import download_media_bytes

        mock_response = type("Resp", (), {"status_code": 404, "headers": {}, "content": b""})()
        with (
            patch("requests.get", return_value=mock_response),
            pytest.raises(MediaGenerationError, match="404"),
        ):
            download_media_bytes("https://cdn.example/missing.png")

    def test_network_error_is_wrapped(self):
        import requests

        from media_gen.download import download_media_bytes

        with (
            patch("requests.get", side_effect=requests.ConnectionError("boom")),
            pytest.raises(MediaGenerationError, match="Network error"),
        ):
            download_media_bytes("https://cdn.example/img.png")


class TestGenerateAudio:
    def test_speech_uses_text_to_speech_task_type(self, client):
        with patch("media_gen.audio.create_and_poll") as mock_create_and_poll:
            mock_create_and_poll.return_value = (
                {"url": "https://cdn.example/speech.mp3"},
                "seed-tts-2.0",
            )
            result = generate_audio(client, text="hello", mode="speech")
            assert result.ok
            assert mock_create_and_poll.call_args.kwargs["task_type"] == "text_to_speech"

    def test_music_uses_text_to_music_task_type(self, client):
        with patch("media_gen.audio.create_and_poll") as mock_create_and_poll:
            mock_create_and_poll.return_value = ({"url": "https://cdn.example/song.mp3"}, "GenBGM")
            result = generate_audio(client, text="a lo-fi beat", mode="music")
            assert result.ok
            assert mock_create_and_poll.call_args.kwargs["task_type"] == "text_to_music"
