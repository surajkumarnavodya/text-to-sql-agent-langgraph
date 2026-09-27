"""Unit tests for media_gen/upload.py -- the IMA local-image-upload flow
(needed for image_to_image/AI-guided editing, added 2026-09-27).

Fully mocked -- no real network call. The mocked shapes here match the real,
verified contract this module's own docstring documents (fetched from
`github.com/imastuido/ima-all-ai`'s `scripts/ima_runtime/shared/
{inputs,client,config}.py` on 2026-09-27): a signed GET against
`imapi.liveme.com/api/rest/oss/getuploadtoken` returning `{"data": {"ful":
..., "fdl": ...}}`, then a plain PUT of the raw bytes to `ful`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from media_gen.client import MediaGenerationError
from media_gen.upload import upload_image_to_ima


def _mock_response(
    status_code: int = 200, json_data: dict | None = None, text: str = ""
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data or {}
    return response


class TestUploadImageToIma:
    def test_successful_upload_returns_the_download_url(self):
        token_response = _mock_response(
            200,
            {
                "data": {
                    "ful": "https://cdn.example.com/put-here",
                    "fdl": "https://cdn.example.com/final.png",
                }
            },
        )
        put_response = _mock_response(200)

        with (
            patch("requests.get", return_value=token_response) as mock_get,
            patch("requests.put", return_value=put_response) as mock_put,
        ):
            url = upload_image_to_ima(
                "real-api-key", b"\x89PNGfakebytes", "image/png", suffix="png"
            )

        assert url == "https://cdn.example.com/final.png"
        # Real bytes were PUT, not a filename/placeholder.
        assert mock_put.call_args.kwargs["data"] == b"\x89PNGfakebytes"
        assert mock_put.call_args.kwargs["headers"]["Content-Type"] == "image/png"
        # The real per-account api_key is sent, not the static app id/key alone.
        get_params = mock_get.call_args.kwargs["params"]
        assert get_params["appUid"] == "real-api-key"
        assert get_params["cmimToken"] == "real-api-key"
        assert get_params["fSuffix"] == "png"
        assert get_params["fContentType"] == "image/png"

    def test_uses_the_real_verified_im_host_and_static_app_credentials(self):
        token_response = _mock_response(
            200, {"data": {"ful": "https://x/put", "fdl": "https://x/dl"}}
        )
        put_response = _mock_response(200)
        with (
            patch("requests.get", return_value=token_response) as mock_get,
            patch("requests.put", return_value=put_response),
        ):
            upload_image_to_ima("k", b"bytes", "image/png", suffix="png")
        url = mock_get.call_args.args[0]
        assert url == "https://imapi.liveme.com/api/rest/oss/getuploadtoken"
        params = mock_get.call_args.kwargs["params"]
        assert params["appId"] == "webAgent"
        assert params["appKey"] == "32jdskjdk320eew"
        assert "sign" in params and "timestamp" in params and "nonce" in params

    def test_token_request_http_error_raises(self):
        with (
            patch("requests.get", return_value=_mock_response(401, text="unauthorized")),
            pytest.raises(MediaGenerationError, match="upload-token"),
        ):
            upload_image_to_ima("bad-key", b"bytes", "image/png", suffix="png")

    def test_token_response_missing_ful_or_fdl_raises(self):
        with (
            patch("requests.get", return_value=_mock_response(200, {"data": {"ful": "https://x"}})),
            pytest.raises(MediaGenerationError, match="ful/fdl"),
        ):
            upload_image_to_ima("k", b"bytes", "image/png", suffix="png")

    def test_token_request_network_error_raises(self):
        with (
            patch("requests.get", side_effect=requests.ConnectionError("dns failure")),
            pytest.raises(MediaGenerationError, match="Network error"),
        ):
            upload_image_to_ima("k", b"bytes", "image/png", suffix="png")

    def test_put_http_error_raises(self):
        token_response = _mock_response(
            200, {"data": {"ful": "https://x/put", "fdl": "https://x/dl"}}
        )
        with (
            patch("requests.get", return_value=token_response),
            patch("requests.put", return_value=_mock_response(500, text="server error")),
            pytest.raises(MediaGenerationError, match="image upload failed"),
        ):
            upload_image_to_ima("k", b"bytes", "image/png", suffix="png")

    def test_put_network_error_raises(self):
        token_response = _mock_response(
            200, {"data": {"ful": "https://x/put", "fdl": "https://x/dl"}}
        )
        with (
            patch("requests.get", return_value=token_response),
            patch("requests.put", side_effect=requests.Timeout("timed out")),
            pytest.raises(MediaGenerationError, match="Network error"),
        ):
            upload_image_to_ima("k", b"bytes", "image/png", suffix="png")

    def test_malformed_token_json_raises(self):
        response = _mock_response(200)
        response.json.side_effect = ValueError("not json")
        with (
            patch("requests.get", return_value=response),
            pytest.raises(MediaGenerationError, match="not valid JSON"),
        ):
            upload_image_to_ima("k", b"bytes", "image/png", suffix="png")
