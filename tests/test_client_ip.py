"""Unit tests for `security/client_ip.py` -- the opt-in, exact-hop-count
trusted-proxy client-IP resolver added by the enterprise scalability/
security assessment (2026-09-27). See that module's own docstring for the
XFF-spoofing rationale this locks in.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from starlette.datastructures import Headers
from starlette.requests import Request

from config.settings import Settings
from security.client_ip import resolve_client_ip
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="x",
    embedding_model_name="x",
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


def _fake_request(host: str | None, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    client = SimpleNamespace(host=host) if host is not None else None
    return cast(Request, SimpleNamespace(client=client, headers=Headers(raw=headers or [])))


def _xff(value: str) -> list[tuple[bytes, bytes]]:
    return [(b"x-forwarded-for", value.encode())]


class TestDefaultBehaviorUnchanged:
    """trusted_proxy_count=0 (the default) must be byte-for-byte the old
    `request.client.host` behavior -- the whole point is zero behavior
    change for every deployment that doesn't opt in."""

    def test_returns_direct_peer(self):
        request = _fake_request("203.0.113.7")
        assert resolve_client_ip(request, _BASE_SETTINGS) == "203.0.113.7"

    def test_ignores_x_forwarded_for_even_when_present(self):
        request = _fake_request("10.0.0.1", _xff("9.9.9.9"))
        assert resolve_client_ip(request, _BASE_SETTINGS) == "10.0.0.1"

    def test_missing_client_returns_unknown(self):
        request = _fake_request(None)
        assert resolve_client_ip(request, _BASE_SETTINGS) == "unknown"


class TestTrustedProxyCountOne:
    def test_reads_the_single_forwarded_entry(self):
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request("10.0.0.1", _xff("203.0.113.9"))
        assert resolve_client_ip(request, settings) == "203.0.113.9"

    def test_falls_back_to_direct_peer_when_header_absent(self):
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request("10.0.0.1")
        assert resolve_client_ip(request, settings) == "10.0.0.1"

    def test_falls_back_to_direct_peer_when_header_is_blank(self):
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request("10.0.0.1", _xff(""))
        assert resolve_client_ip(request, settings) == "10.0.0.1"

    def test_strips_whitespace_around_entries(self):
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request("10.0.0.1", _xff("  203.0.113.9  "))
        assert resolve_client_ip(request, settings) == "203.0.113.9"


class TestTrustedProxyCountTwo:
    def test_reads_the_second_from_right_entry(self):
        """Client -> Proxy1 -> Proxy2 -> App: XFF as seen by the app is
        "client, proxy1" (Proxy2's own address is the direct TCP peer, not
        in the header at all) -- with 2 trusted hops, the real client is
        the leftmost/first entry, i.e. the 2nd-from-the-right of a 2-entry
        list."""
        settings = _settings(trusted_proxy_count=2)
        request = _fake_request("10.0.0.2", _xff("203.0.113.9, 10.0.0.1"))
        assert resolve_client_ip(request, settings) == "203.0.113.9"

    def test_falls_back_when_chain_is_shorter_than_the_trusted_count(self):
        """A misconfigured/incomplete proxy chain (only 1 hop present but 2
        are configured as trusted) must fail closed to the direct peer,
        never trust a header entry with no corresponding verified hop."""
        settings = _settings(trusted_proxy_count=2)
        request = _fake_request("10.0.0.1", _xff("203.0.113.9"))
        assert resolve_client_ip(request, settings) == "10.0.0.1"


class TestSpoofingResistance:
    """The core security property: only entries appended by hops we
    actually trust are ever read -- anything a client could have prepended
    before ever reaching the trust boundary is ignored."""

    def test_client_prepended_extra_hops_beyond_the_trusted_count_are_ignored(self):
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request("10.0.0.1", _xff("9.9.9.9, 8.8.8.8, 203.0.113.9"))
        assert resolve_client_ip(request, settings) == "203.0.113.9"

    def test_two_different_real_clients_behind_one_trusted_lb_are_distinguished(self):
        """The actual production bug this fixes: without this, both of
        these would resolve to the LB's own single IP and share one
        rate-limit bucket."""
        settings = _settings(trusted_proxy_count=1)
        request_a = _fake_request("10.0.0.1", _xff("203.0.113.9"))
        request_b = _fake_request("10.0.0.1", _xff("198.51.100.4"))
        assert resolve_client_ip(request_a, settings) != resolve_client_ip(request_b, settings)

    def test_identical_spoofed_prefix_from_different_real_clients_does_not_pool_them(self):
        settings = _settings(trusted_proxy_count=1)
        same_spoofed_prefix = "10.0.0.1, "
        request_a = _fake_request("10.0.0.1", _xff(same_spoofed_prefix + "203.0.113.9"))
        request_b = _fake_request("10.0.0.1", _xff(same_spoofed_prefix + "198.51.100.4"))
        assert resolve_client_ip(request_a, settings) != resolve_client_ip(request_b, settings)
