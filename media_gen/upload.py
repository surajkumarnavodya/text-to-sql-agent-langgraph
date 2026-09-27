"""Uploads local image bytes to IMA's CDN and returns a public URL --
needed because `POST /open/v1/tasks/create`'s `src_img_url`/`input_images`
fields accept a URL, never raw bytes/base64 (see `media_gen/client.py`'s own
module docstring for how the rest of the IMA contract was verified the same
way). This is the one remaining piece of that contract `media_gen/client.py`
never needed before now, since every existing caller (`text_to_image`/
`text_to_video`/`text_to_speech`/`text_to_music`) is text-only.

VERIFIED against IMA's own real reference source (not guessed), the same
way `media_gen/client.py` was: fetched `scripts/ima_runtime/shared/
{inputs,client,config}.py` from `github.com/imastuido/ima-all-ai` (the
identical public skill-bundle repo `media_gen/client.py` cites) on
2026-09-27 and read the actual `prepare_image_url`/`request_upload_token`/
`upload_binary` implementations. That is the ground truth this module is
built from.

Real upload contract (confirmed, not assumed):
  1. `GET https://imapi.liveme.com/api/rest/oss/getuploadtoken` -- a
     *signed* request against a separate IM/CDN host (`imapi.liveme.com`,
     not `api.imastudio.com`), using a static, publicly-embedded
     `appId`/`appKey` pair (`"webAgent"`/`"32jdskjdk320eew"` -- these are
     not per-account secrets; they're literally hardcoded in the public,
     open-source reference client every caller of this skill bundle
     shares) plus a SHA1 signature over
     `f"{appId}|{appKey}|{timestamp}|{nonce}"`, alongside the real,
     per-account `api_key` (sent as both `appUid` and `cmimToken`). Query
     params: `appUid`, `appId`, `appKey`, `cmimToken`, `sign`, `timestamp`,
     `nonce`, `fService="privite"` [sic -- verified typo in the real API,
     not a bug introduced here], `fType="picture"`, `fSuffix` (file
     extension without the dot), `fContentType`. Response:
     `{"data": {"ful": "<PUT this URL>", "fdl": "<public download URL>"}}`.
  2. `PUT <ful>` with the raw image bytes and `Content-Type: <content_type>`.
  3. `<fdl>` is now a real, fetchable URL -- use it as `src_img_url`/
     `input_images` in a `tasks/create` payload.

Known simplification vs. the reference `ima_runtime` package (deliberate):
no retry/backoff on the upload-token request or the PUT itself -- a
transient failure surfaces as a clean `MediaGenerationError` instead,
matching this module's own caller (`media_gen.image_edit_provider`)'s
already-established "no create-time retry-with-degradation" precedent
documented in `media_gen/client.py`.
"""

from __future__ import annotations

import hashlib
import time
import uuid

import requests

from media_gen.client import MediaGenerationError
from security.redaction import redact_secrets

# Real, verified constants -- see this module's own docstring for the
# source. Not secrets: both are hardcoded, publicly-embedded values in the
# open-source `ima-all-ai` reference client every caller of this skill
# bundle shares, distinct from the real, per-account `IMA_API_KEY` secret.
_IMA_IM_BASE = "https://imapi.liveme.com"
_APP_ID = "webAgent"
_APP_KEY = "32jdskjdk320eew"

_UPLOAD_TOKEN_TIMEOUT_SECONDS = 15.0
_PUT_TIMEOUT_SECONDS = 60.0


def _generate_signature() -> tuple[str, str, str]:
    """SHA1 of `appId|appKey|timestamp|nonce`, matching `ima_runtime.shared
    .client._gen_sign()` exactly -- verified against the real reference
    source, not reverse-engineered from behavior."""
    nonce = uuid.uuid4().hex[:21]
    timestamp = str(int(time.time()))
    raw = f"{_APP_ID}|{_APP_KEY}|{timestamp}|{nonce}"
    signature = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest().upper()
    return signature, timestamp, nonce


def upload_image_to_ima(api_key: str, image_bytes: bytes, content_type: str, *, suffix: str) -> str:
    """Uploads `image_bytes` to IMA's CDN and returns the resulting public
    download URL (`fdl`) -- the one real, live-verified way to give an
    `image_to_image` task a source image (see this module's own docstring).

    Args:
        api_key: The real, per-account IMA API key (`Settings.ima_api_key
            .get_secret_value()`) -- sent as both `appUid` and `cmimToken`,
            matching the reference client's own request shape.
        image_bytes: Raw image bytes to upload.
        content_type: e.g. `"image/png"`.
        suffix: File extension without the leading dot (e.g. `"png"`).

    Raises:
        MediaGenerationError: on any network/HTTP failure, or a malformed
            token response missing `ful`/`fdl`.
    """
    signature, timestamp, nonce = _generate_signature()
    try:
        token_response = requests.get(
            f"{_IMA_IM_BASE}/api/rest/oss/getuploadtoken",
            params={
                "appUid": api_key,
                "appId": _APP_ID,
                "appKey": _APP_KEY,
                "cmimToken": api_key,
                "sign": signature,
                "timestamp": timestamp,
                "nonce": nonce,
                "fService": "privite",
                "fType": "picture",
                "fSuffix": suffix,
                "fContentType": content_type,
            },
            timeout=_UPLOAD_TOKEN_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise MediaGenerationError(
            f"Network error requesting an IMA upload token: {redact_secrets(str(exc))}"
        ) from exc

    if token_response.status_code >= 400:
        raise MediaGenerationError(
            f"IMA upload-token request failed with HTTP {token_response.status_code}: "
            f"{redact_secrets(token_response.text[:500])}",
            status_code=token_response.status_code,
        )

    try:
        token_data = (token_response.json() or {}).get("data") or {}
    except ValueError as exc:
        raise MediaGenerationError("IMA upload-token response was not valid JSON.") from exc

    upload_url = token_data.get("ful")
    download_url = token_data.get("fdl")
    if not upload_url or not download_url:
        raise MediaGenerationError(
            f"IMA upload-token response was missing ful/fdl: {redact_secrets(str(token_data))}"
        )

    try:
        put_response = requests.put(
            upload_url,
            data=image_bytes,
            headers={"Content-Type": content_type},
            timeout=_PUT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise MediaGenerationError(
            f"Network error uploading image bytes to IMA: {redact_secrets(str(exc))}"
        ) from exc

    if put_response.status_code >= 400:
        raise MediaGenerationError(
            f"IMA image upload failed with HTTP {put_response.status_code}: "
            f"{redact_secrets(put_response.text[:500])}",
            status_code=put_response.status_code,
        )

    return download_url
