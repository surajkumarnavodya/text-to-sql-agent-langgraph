"""Fetches a generated asset's bytes from the provider's CDN, once.

`generation_node` calls this immediately after IMA reports a generation as
complete, so the raw provider URL is only ever dereferenced server-side --
it's never stored in `AgentState`, the API response, or shown to the user
(see `media_gen/cache.py` and `agent.orchestrator.state
.MediaGenerationResult.media_id`).

SECURITY (SSRF): `url` here is not a value this app constructed -- it's
taken verbatim from IMA's own API response body (`media.get("url")` in
`image.py`/`video.py`/`audio.py`). If that provider account, its DNS, or a
response in transit were ever compromised, an attacker-controlled URL could
otherwise point this server-side fetch at internal infrastructure (a cloud
metadata endpoint, an internal admin panel, `localhost`) -- the response
would then be cached and served back to the requesting client via
`GET /media/{media_id}`, effectively proxying internal content out through
what looks like a generated image. `_validate_download_url` below closes
this: HTTPS-only, and the resolved address must not fall in a private/
loopback/link-local/reserved range. This is a resolve-then-connect check,
not a fully rebinding-proof pinned-IP fetch (the OS could theoretically
re-resolve a different address between the check and `requests.get`) --
acceptable here since the realistic threat is a malicious *response URL*
from a compromised provider, not an active attacker racing DNS against
this one low-value internal call.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import requests

from media_gen.client import MediaGenerationError
from security.redaction import redact_secrets

_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# 2026 Phase 3 file-upload security review (finding G1): this app only ever
# generates image/video/audio -- a provider response claiming anything else
# (e.g. `text/html`) is either a bug or a compromised/malicious response,
# and `GET /media/{media_id}` (api/media.py) serves this value back to the
# browser verbatim as the HTTP response's Content-Type. Without this
# allowlist, an attacker-controlled `text/html` (or `image/svg+xml`, which
# browsers can render as script-executing markup) served under this app's
# own origin would be a stored-content-confusion/XSS vector -- the same
# class of risk `_validate_download_url` above already treats provider
# responses as untrusted for (SSRF), just applied to the declared content
# type instead of the URL. Falls back to the generic binary type (which
# browsers download rather than render inline) rather than rejecting the
# whole download outright, since the bytes themselves are still valid,
# already-size-capped media content worth keeping.
_ALLOWED_CONTENT_TYPE_PREFIXES = ("image/", "video/", "audio/")

# Private/loopback/link-local/reserved ranges a legitimate public CDN
# should never resolve to -- IPv4 and IPv6 alike. Deliberately broad (a
# false positive here just means a generation fails closed with a clean
# error, never a reason to loosen it for convenience).
_BLOCKED_IP_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "::/128",
        "64:ff9b::/96",
        "100::/64",
        "fc00::/7",
        "fe80::/10",
    )
)


def _validate_download_url(url: str) -> None:
    """Raises `MediaGenerationError` if `url` isn't safe to fetch
    server-side -- see this module's docstring for why this check exists
    at all."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise MediaGenerationError(
            f"Refusing to download generated media over non-HTTPS scheme: {parsed.scheme!r}"
        )
    hostname = parsed.hostname
    if not hostname:
        raise MediaGenerationError("Refusing to download generated media: URL has no hostname")

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise MediaGenerationError(f"Could not resolve media host {hostname!r}: {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if any(ip in network for network in _BLOCKED_IP_NETWORKS):
            raise MediaGenerationError(
                f"Refusing to download generated media from a private/internal "
                f"address ({ip}) -- possible SSRF attempt via a malicious provider response"
            )


_DEFAULT_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # matches Settings.media_max_file_mb's default
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def download_media_bytes(
    url: str, timeout: float = 30.0, max_bytes: int = _DEFAULT_MAX_DOWNLOAD_BYTES
) -> tuple[bytes, str]:
    """Downloads `url` and returns `(content, content_type)`.

    `content_type` is read from the response's own `Content-Type` header
    (falling back to a generic binary type if absent or if it doesn't match
    `_ALLOWED_CONTENT_TYPE_PREFIXES` -- see that constant's docstring) --
    IMA's CDN, not this app, is the source of truth for what kind of file
    it actually served, within that allowlist. Raises `MediaGenerationError`
    on any transport/HTTP failure,
    matching `media_gen.client.IMAClient._request`'s own error contract so
    callers only need to catch one exception type across the whole
    generate-then-download flow. `_validate_download_url` runs first --
    see this module's docstring for the SSRF risk it closes.

    2026 Phase 2 security review: streams the response and aborts once
    `max_bytes` is exceeded (checked both against a declared
    `Content-Length`, before reading any body at all, and against the
    actual bytes received while streaming, since a response can omit or
    lie about `Content-Length`) -- a response-size limit, matching this
    app's existing "never buffer an unbounded upload/download into memory"
    posture elsewhere (`api/documents.py::upload_document`,
    `api/voice.py::transcribe_audio`, both bounded-read for the same
    reason). Without this, a compromised or misbehaving provider response
    could exhaust this process's memory on a single generation result.
    `max_bytes` defaults to matching `Settings.media_max_file_mb`'s own
    default (200MB) -- the same cap this app already applies to a
    locally-ingested media file, reused here rather than inventing a
    second, independent limit for a conceptually identical "how big a
    media file may this app hold in memory at once" question.
    """
    _validate_download_url(url)

    try:
        response = requests.get(url, timeout=timeout, stream=True)
    except requests.RequestException as exc:
        raise MediaGenerationError(
            f"Network error downloading generated media: {redact_secrets(str(exc))}"
        ) from exc

    with response:
        if response.status_code >= 400:
            raise MediaGenerationError(
                f"HTTP error {response.status_code} downloading generated media from provider",
                status_code=response.status_code,
            )

        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            try:
                if int(declared_length) > max_bytes:
                    raise MediaGenerationError(
                        f"Refusing to download generated media: declared size "
                        f"{declared_length} bytes exceeds the {max_bytes}-byte limit."
                    )
            except ValueError:
                pass  # a malformed Content-Length is caught by the streaming check below anyway

        content_type = (
            response.headers.get("Content-Type", _DEFAULT_CONTENT_TYPE).split(";")[0].strip()
        )
        if not content_type.lower().startswith(_ALLOWED_CONTENT_TYPE_PREFIXES):
            content_type = _DEFAULT_CONTENT_TYPE

        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                total += len(chunk)
                if total > max_bytes:
                    raise MediaGenerationError(
                        f"Refusing to download generated media: exceeded the "
                        f"{max_bytes}-byte limit while streaming the response."
                    )
                chunks.append(chunk)
        except requests.RequestException as exc:
            raise MediaGenerationError(
                f"Network error downloading generated media: {redact_secrets(str(exc))}"
            ) from exc

    return b"".join(chunks), content_type or _DEFAULT_CONTENT_TYPE
