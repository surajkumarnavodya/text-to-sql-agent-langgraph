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

    def test_insufficient_points_gets_a_friendly_safe_message(self, client):
        """Regression test: a real, reported bug -- IMA's raw
        `{'code': 4008, 'message': 'Insufficient points', ...}` dict repr
        was reaching the UI verbatim as the answer text. `.safe_message`
        must instead be an actionable, human sentence, never that repr."""
        with patch.object(client.session, "request") as mock_request:
            mock_request.return_value.status_code = 500
            mock_request.return_value.json.return_value = {
                "code": 4008,
                "message": "Insufficient points",
                "timestamp": 1789305859,
            }
            with pytest.raises(MediaGenerationError) as exc_info:
                client._request("POST", "/open/v1/tasks/create", json={"prompt": "x"})
            safe_message = exc_info.value.safe_message.lower()
            assert "credits" in safe_message or "points" in safe_message
            assert "top up" in safe_message
            assert "4008" not in exc_info.value.safe_message
            # str(exc) (the internal detail, for logs) is unaffected -- it
            # still carries the raw provider payload.
            assert "4008" in str(exc_info.value)

    def test_business_error_code_also_gets_a_safe_message_on_http_200(self, client):
        with patch.object(client.session, "request") as mock_request:
            mock_request.return_value.status_code = 200
            mock_request.return_value.json.return_value = {
                "code": 4008,
                "message": "Insufficient points",
            }
            with pytest.raises(MediaGenerationError) as exc_info:
                client._request("POST", "/open/v1/tasks/create", json={"prompt": "x"})
            assert "top up" in exc_info.value.safe_message.lower()

    def test_unrecognized_code_falls_back_to_the_generic_safe_message(self, client):
        with patch.object(client.session, "request") as mock_request:
            mock_request.return_value.status_code = 500
            mock_request.return_value.json.return_value = {"code": 9999, "message": "who knows"}
            with pytest.raises(MediaGenerationError) as exc_info:
                client._request("POST", "/open/v1/tasks/create", json={"prompt": "x"})
            assert exc_info.value.safe_message == (
                "Media generation failed due to a provider error. Please try again in a moment."
            )

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

    def test_no_image_urls_produces_empty_lists_backward_compatibly(self):
        """Every pre-existing (text-only) caller must see byte-identical
        behavior after the 2026-09-27 image_to_image extension."""
        model_params = _extract_model_params(_SAMPLE_LEAF)
        payload = build_create_payload("text_to_image", model_params, "a red fox")
        assert payload["src_img_url"] == []
        assert payload["parameters"][0]["parameters"]["input_images"] == []

    def test_image_urls_populate_both_top_level_and_inner_fields(self):
        """Verified against the real reference doc (`references/models/
        product-list-and-create-params.md`): "top-level src_img_url and
        inner input_images" -- both must carry the URL, not just one."""
        model_params = _extract_model_params(_SAMPLE_LEAF)
        payload = build_create_payload(
            "image_to_image", model_params, "remove the object", image_urls=["https://cdn/src.png"]
        )
        assert payload["src_img_url"] == ["https://cdn/src.png"]
        assert payload["parameters"][0]["parameters"]["input_images"] == ["https://cdn/src.png"]


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

    def test_image_urls_reach_create_task_in_both_required_fields(self, client):
        """image_to_image/AI-guided-editing support (2026-09-27) --
        `image_urls` must flow all the way through to the real
        `create_task` payload, not just `build_create_payload` in
        isolation (see `TestBuildCreatePayload` above for that)."""
        captured_payload = {}

        def _capture_create_task(payload):
            captured_payload.update(payload)
            return "task_456"

        with (
            patch.object(client, "get_product_list", return_value=[_SAMPLE_LEAF]),
            patch.object(client, "create_task", side_effect=_capture_create_task),
            patch.object(
                client,
                "poll_task",
                return_value={"resource_status": 1, "url": "https://cdn.example/edited.png"},
            ),
        ):
            create_and_poll(
                client,
                task_type="image_to_image",
                prompt="remove the object",
                image_urls=["https://cdn.example/source.png"],
            )
        assert captured_payload["src_img_url"] == ["https://cdn.example/source.png"]
        assert captured_payload["parameters"][0]["parameters"]["input_images"] == [
            "https://cdn.example/source.png"
        ]


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
            # error is always the short, user-facing safe_message (never the
            # raw internal detail) -- detail carries "boom" instead, for logs.
            assert result.error == (
                "Media generation failed due to a provider error. Please try again in a moment."
            )
            assert "boom" in result.detail


class TestGenerateVideo:
    def test_success(self, client):
        with patch(
            "media_gen.video.create_and_poll",
            return_value=({"url": "https://cdn.example/video.mp4"}, "Wan 2.6"),
        ):
            result = generate_video(client, prompt="a crane lifting a beam")
            assert result.ok
            assert result.url == "https://cdn.example/video.mp4"

    def test_duration_seconds_is_passed_as_a_form_override(self, client):
        """Settings.media_gen_video_duration_seconds flows through to the
        real create-task payload as a `duration` form override -- verified
        live against a real IMA account: the auto-selected video model
        declares `duration` as an integer 4-15 form field, so this is what
        actually changes clip length within that model's real range."""
        with patch("media_gen.video.create_and_poll") as mock_create_and_poll:
            mock_create_and_poll.return_value = (
                {"url": "https://cdn.example/video.mp4"},
                "Seedance 2.0",
            )
            generate_video(client, prompt="a crane lifting a beam", duration_seconds=10)
            assert mock_create_and_poll.call_args.kwargs["form_overrides"] == {"duration": 10}

    def test_no_duration_means_no_override(self, client):
        with patch("media_gen.video.create_and_poll") as mock_create_and_poll:
            mock_create_and_poll.return_value = (
                {"url": "https://cdn.example/video.mp4"},
                "Seedance 2.0",
            )
            generate_video(client, prompt="a crane lifting a beam")
            assert mock_create_and_poll.call_args.kwargs["form_overrides"] is None


class TestCreateAndPollFormOverrides:
    def test_form_overrides_replace_the_models_own_default(self, client):
        """The model's own `form_config` default ("duration": 5, in this
        codebase's real verified case) must be overridable, not just
        additive -- confirms `.update()` semantics, not append/merge that
        could leave a stale default key alongside the override."""
        leaf = {**_SAMPLE_LEAF, "form_config": [{"field": "duration", "value": 5}]}
        with (
            patch.object(client, "get_product_list", return_value=[leaf]),
            patch.object(client, "create_task", return_value="task_123") as mock_create,
            patch.object(
                client,
                "poll_task",
                return_value={"resource_status": 1, "url": "https://cdn.example/v.mp4"},
            ),
        ):
            create_and_poll(
                client, task_type="text_to_video", prompt="x", form_overrides={"duration": 10}
            )
            sent_payload = mock_create.call_args[0][0]
            assert sent_payload["parameters"][0]["parameters"]["duration"] == 10


_PUBLIC_ADDRINFO = [(2, 1, 6, "", ("93.184.216.34", 0))]  # a real, public IPv4 (example.com)


class _MockStreamResponse:
    """A `requests.Response` stand-in supporting exactly what
    `download_media_bytes` needs since it moved to a streamed,
    size-capped download (2026 Phase 2 security review): the context-
    manager protocol (`with response:`) and `.iter_content()`, on top of
    the plain `status_code`/`headers` attributes the pre-existing mocks
    already provided."""

    def __init__(self, status_code: int, headers: dict, body: bytes):
        self.status_code = status_code
        self.headers = headers
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def iter_content(self, chunk_size: int = 1024):
        if self._body:
            yield self._body


class TestDownloadMediaBytes:
    """`_validate_download_url` resolves the hostname for real (see its own
    docstring for why -- SSRF defense) before ever reaching `requests.get`,
    so every test here mocks `socket.getaddrinfo` to a fixed public address
    rather than depending on live DNS for a fake `cdn.example` host."""

    def test_success_returns_bytes_and_content_type(self):
        from media_gen.download import download_media_bytes

        mock_response = _MockStreamResponse(200, {"Content-Type": "image/png"}, b"abc")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=mock_response),
        ):
            data, content_type = download_media_bytes("https://cdn.example/img.png")
        assert data == b"abc"
        assert content_type == "image/png"

    def test_video_and_audio_content_types_pass_through(self):
        from media_gen.download import download_media_bytes

        for declared in ("video/mp4", "audio/mpeg", "IMAGE/PNG"):  # case-insensitive
            mock_response = _MockStreamResponse(200, {"Content-Type": declared}, b"abc")
            with (
                patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
                patch("requests.get", return_value=mock_response),
            ):
                _, content_type = download_media_bytes("https://cdn.example/f")
            assert content_type == declared

    def test_disallowed_content_type_falls_back_to_generic_binary(self):
        """2026 Phase 3 file-upload security review (finding G1): this app
        only ever generates image/video/audio -- a provider response
        claiming `text/html` (or anything else) must not be reflected
        verbatim into `GET /media/{media_id}`'s response Content-Type, since
        that could let a compromised/malicious provider serve
        script-executing content under this app's own origin."""
        from media_gen.download import download_media_bytes

        mock_response = _MockStreamResponse(200, {"Content-Type": "text/html"}, b"<script>")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=mock_response),
        ):
            data, content_type = download_media_bytes("https://cdn.example/f")
        assert content_type == "application/octet-stream"
        assert data == b"<script>"  # bytes themselves are still returned, only the type is capped

    def test_http_error_raises_media_generation_error(self):
        from media_gen.download import download_media_bytes

        mock_response = _MockStreamResponse(404, {}, b"")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=mock_response),
            pytest.raises(MediaGenerationError, match="404"),
        ):
            download_media_bytes("https://cdn.example/missing.png")

    def test_declared_content_length_over_limit_is_rejected_before_streaming(self):
        from media_gen.download import download_media_bytes

        mock_response = _MockStreamResponse(
            200, {"Content-Type": "image/png", "Content-Length": "999999999"}, b"abc"
        )
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=mock_response),
            pytest.raises(MediaGenerationError, match="declared size"),
        ):
            download_media_bytes("https://cdn.example/huge.png", max_bytes=1000)

    def test_actual_streamed_size_over_limit_is_rejected_even_without_content_length(self):
        """A response that omits or lies about Content-Length must still be
        caught while streaming -- the declared-length check alone isn't
        sufficient."""
        from media_gen.download import download_media_bytes

        mock_response = _MockStreamResponse(200, {"Content-Type": "image/png"}, b"x" * 2000)
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=mock_response),
            pytest.raises(MediaGenerationError, match="exceeded the"),
        ):
            download_media_bytes("https://cdn.example/huge.png", max_bytes=1000)

    def test_size_within_limit_succeeds(self):
        from media_gen.download import download_media_bytes

        mock_response = _MockStreamResponse(200, {"Content-Type": "image/png"}, b"x" * 500)
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=mock_response),
        ):
            data, _ = download_media_bytes("https://cdn.example/ok.png", max_bytes=1000)
        assert len(data) == 500

    def test_network_error_is_wrapped(self):
        import requests

        from media_gen.download import download_media_bytes

        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", side_effect=requests.ConnectionError("boom")),
            pytest.raises(MediaGenerationError, match="Network error"),
        ):
            download_media_bytes("https://cdn.example/img.png")

    def test_rejects_non_https_scheme(self):
        from media_gen.download import download_media_bytes

        with pytest.raises(MediaGenerationError, match="non-HTTPS"):
            download_media_bytes("http://cdn.example/img.png")

    def test_rejects_url_with_no_hostname(self):
        from media_gen.download import download_media_bytes

        with pytest.raises(MediaGenerationError, match="no hostname"):
            download_media_bytes("https:///img.png")

    def test_rejects_unresolvable_host(self):
        import socket as socket_module

        from media_gen.download import download_media_bytes

        with (
            patch("socket.getaddrinfo", side_effect=socket_module.gaierror("nope")),
            pytest.raises(MediaGenerationError, match="Could not resolve"),
        ):
            download_media_bytes("https://does-not-exist.invalid/img.png")

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC1918 private
            "169.254.169.254",  # cloud metadata endpoint
            "192.168.1.1",  # RFC1918 private
            "::1",  # IPv6 loopback
        ],
    )
    def test_rejects_private_and_internal_addresses(self, ip):
        from media_gen.download import download_media_bytes

        family = 10 if ":" in ip else 2
        with (
            patch("socket.getaddrinfo", return_value=[(family, 1, 6, "", (ip, 0))]),
            pytest.raises(MediaGenerationError, match="private/internal address"),
        ):
            download_media_bytes(f"https://malicious.example/{ip}")

    def test_allows_a_genuinely_public_address(self):
        from media_gen.download import _validate_download_url

        with patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO):
            _validate_download_url("https://cdn.example/img.png")  # must not raise


class TestDownloadMediaBytesAuditLogging:
    """Prompt 21 (enterprise security & data governance hardening): every
    SSRF rejection in `_validate_download_url` must leave a structured
    `security.audit_log` trail -- previously a real, disclosed gap (the
    defense existed and was tested, but a blocked attempt left no signal
    anywhere)."""

    def test_non_https_scheme_is_audit_logged(self):
        from media_gen.download import download_media_bytes

        with (
            patch("media_gen.download.log_security_event") as mock_log,
            pytest.raises(MediaGenerationError),
        ):
            download_media_bytes("http://cdn.example/img.png")

        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[0] == "ssrf_blocked"
        assert kwargs["reason"] == "non_https_scheme"

    def test_no_hostname_is_audit_logged(self):
        from media_gen.download import download_media_bytes

        with (
            patch("media_gen.download.log_security_event") as mock_log,
            pytest.raises(MediaGenerationError),
        ):
            download_media_bytes("https:///img.png")

        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[0] == "ssrf_blocked"
        assert kwargs["reason"] == "no_hostname"

    def test_private_address_is_audit_logged(self):
        from media_gen.download import download_media_bytes

        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 0))]),
            patch("media_gen.download.log_security_event") as mock_log,
            pytest.raises(MediaGenerationError),
        ):
            download_media_bytes("https://malicious.example/img.png")

        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[0] == "ssrf_blocked"
        assert kwargs["reason"] == "private_or_reserved_address"
        assert kwargs["host"] == "malicious.example"
        assert kwargs["resolved_ip"] == "10.0.0.5"

    def test_unresolvable_host_is_not_logged_as_ssrf_blocked(self):
        """A DNS resolution failure never evaluated any address against the
        blocklist -- it must not be logged under the same event type as an
        actual SSRF rejection."""
        import socket as socket_module

        from media_gen.download import download_media_bytes

        with (
            patch("socket.getaddrinfo", side_effect=socket_module.gaierror("nope")),
            patch("media_gen.download.log_security_event") as mock_log,
            pytest.raises(MediaGenerationError),
        ):
            download_media_bytes("https://does-not-exist.invalid/img.png")

        mock_log.assert_not_called()

    def test_allowed_url_never_logs(self):
        from media_gen.download import _validate_download_url

        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("media_gen.download.log_security_event") as mock_log,
        ):
            _validate_download_url("https://cdn.example/img.png")

        mock_log.assert_not_called()


class TestDownloadMediaBytesRedirectHandling:
    """2026 Phase 3 security review: `_validate_download_url` on the
    *original* URL is moot if a redirect is then followed blindly -- these
    confirm every redirect hop is independently re-validated, not just the
    first URL."""

    def test_redirect_to_public_address_is_followed(self):
        from media_gen.download import download_media_bytes

        redirect_response = _MockStreamResponse(
            302, {"Location": "https://cdn2.example/img.png"}, b""
        )
        final_response = _MockStreamResponse(200, {"Content-Type": "image/png"}, b"abc")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", side_effect=[redirect_response, final_response]),
        ):
            data, content_type = download_media_bytes("https://cdn.example/img.png")
        assert data == b"abc"
        assert content_type == "image/png"

    def test_redirect_to_private_address_is_rejected(self):
        """The redirect target resolves to a private address -- must be
        caught exactly like a direct request to it would be, even though
        the original URL's own address was genuinely public."""
        from media_gen.download import download_media_bytes

        redirect_response = _MockStreamResponse(
            302, {"Location": "https://internal.example/secrets"}, b""
        )
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=[_PUBLIC_ADDRINFO, [(2, 1, 6, "", ("169.254.169.254", 0))]],
            ),
            patch("requests.get", return_value=redirect_response),
            pytest.raises(MediaGenerationError, match="private/internal address"),
        ):
            download_media_bytes("https://cdn.example/img.png")

    def test_redirect_to_non_https_is_rejected(self):
        from media_gen.download import download_media_bytes

        redirect_response = _MockStreamResponse(
            302, {"Location": "http://cdn2.example/img.png"}, b""
        )
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=redirect_response),
            pytest.raises(MediaGenerationError, match="non-HTTPS"),
        ):
            download_media_bytes("https://cdn.example/img.png")

    def test_redirect_with_no_location_header_is_rejected(self):
        from media_gen.download import download_media_bytes

        redirect_response = _MockStreamResponse(302, {}, b"")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=redirect_response),
            pytest.raises(MediaGenerationError, match="no Location header"),
        ):
            download_media_bytes("https://cdn.example/img.png")

    def test_relative_redirect_location_is_resolved_against_current_url(self):
        from media_gen.download import download_media_bytes

        redirect_response = _MockStreamResponse(302, {"Location": "/moved/img.png"}, b"")
        final_response = _MockStreamResponse(200, {"Content-Type": "image/png"}, b"abc")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", side_effect=[redirect_response, final_response]),
        ):
            data, _ = download_media_bytes("https://cdn.example/img.png")
        assert data == b"abc"

    def test_too_many_redirects_is_rejected(self):
        from media_gen.download import _MAX_REDIRECTS, download_media_bytes

        redirect_response = _MockStreamResponse(302, {"Location": "https://cdn.example/next"}, b"")
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_ADDRINFO),
            patch("requests.get", return_value=redirect_response),
            pytest.raises(MediaGenerationError, match=f"exceeded {_MAX_REDIRECTS}"),
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
