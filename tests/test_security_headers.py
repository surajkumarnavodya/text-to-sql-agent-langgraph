"""Unit tests for api/main.py's `_add_security_headers` middleware -- 2026
Phase 3 security review. Fully mocked at the HTTP layer via FastAPI's
`TestClient`, hitting `/health` (no auth/DB/LLM dependency) as the simplest
route to observe response headers on.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _settings_with(**overrides: object) -> Settings:
    """A real `Settings` instance with `overrides` applied on top of
    whatever the process's current settings already are -- mirrors the
    override pattern used throughout this test suite
    (`tests/test_write_privilege_check.py`'s own `_settings` helper)."""
    base = api_main.get_settings()
    return Settings(**{**base.__dict__, **overrides})


class TestSecurityHeadersPresentByDefault:
    def test_x_content_type_options(self, client):
        response = client.get("/health")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        response = client.get("/health")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_referrer_policy(self, client):
        response = client.get("/health")
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_permissions_policy_allows_microphone_but_denies_others(self, client):
        response = client.get("/health")
        policy = response.headers.get("Permissions-Policy", "")
        assert "microphone=(self)" in policy
        assert "camera=()" in policy
        assert "geolocation=()" in policy

    def test_strict_transport_security(self, client):
        response = client.get("/health")
        hsts = response.headers.get("Strict-Transport-Security", "")
        assert "max-age=" in hsts
        assert "includeSubDomains" in hsts

    def test_content_security_policy_default(self, client):
        response = client.get("/health")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        # No unsafe-eval/unsafe-inline on script-src specifically -- that's
        # the directive that actually matters for XSS defense.
        assert "script-src 'self' 'unsafe-inline'" not in csp
        assert "script-src 'self' 'unsafe-eval'" not in csp

    def test_frame_src_is_self_only_when_oidc_not_configured(self, client):
        response = client.get("/health")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "frame-src 'self';" in csp

    def test_frame_src_includes_oidc_issuer_origin_when_configured(self, client):
        """2026 Phase 3: frontend/src/lib/auth.ts's OIDC silent-renew loads
        the identity provider in a hidden iframe -- without this,
        frame-src would inherit default-src 'self' and silently block
        every silent-renew attempt."""
        settings = _settings_with(
            oidc_issuer="https://my-tenant.auth0.com/", oidc_audience="my-api"
        )
        with patch("api.main.get_settings", return_value=settings):
            response = client.get("/health")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "frame-src 'self' https://my-tenant.auth0.com;" in csp


class TestSecurityHeadersConfigurable:
    def test_disabled_via_settings_omits_all_headers(self, client):
        with patch("api.main.get_settings", return_value=_settings_with(enable_security_headers=False)):
            response = client.get("/health")
        assert "Content-Security-Policy" not in response.headers
        assert "X-Frame-Options" not in response.headers
        assert "Strict-Transport-Security" not in response.headers

    def test_empty_csp_override_omits_only_csp(self, client):
        with patch("api.main.get_settings", return_value=_settings_with(content_security_policy="")):
            response = client.get("/health")
        assert "Content-Security-Policy" not in response.headers
        # The other headers are independent of the CSP override.
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_custom_csp_override_is_used_verbatim(self, client):
        custom = "default-src 'none'"
        with patch("api.main.get_settings", return_value=_settings_with(content_security_policy=custom)):
            response = client.get("/health")
        assert response.headers.get("Content-Security-Policy") == custom

    def test_route_supplied_header_is_not_overridden(self):
        """api/documents.py's PDF download route sets its own
        X-Content-Type-Options -- the middleware's use of setdefault must
        never clobber a header a route handler already set. Exercises
        `_add_security_headers` directly against a fake `call_next` rather
        than a real route, so this doesn't need a configured RAG store.
        No existing test in this suite uses async def / pytest-anyio's
        marker, so this drives the coroutine with a plain `asyncio.run`
        instead of introducing that pattern for one test."""
        import asyncio

        from fastapi import Response

        from api.main import _add_security_headers

        async def fake_call_next(request):
            return Response(headers={"X-Content-Type-Options": "custom-value"})

        async def run():
            return await _add_security_headers(request=None, call_next=fake_call_next)

        response = asyncio.run(run())
        assert response.headers["X-Content-Type-Options"] == "custom-value"


class TestCorsWildcardRejected:
    def test_wildcard_origin_is_rejected_at_settings_construction(self):
        from config.settings import ConfigurationError

        with pytest.raises(ConfigurationError, match="CORS_ALLOWED_ORIGINS"):
            _settings_with(cors_allowed_origins=("*",))

    def test_wildcard_among_other_origins_is_still_rejected(self):
        from config.settings import ConfigurationError

        with pytest.raises(ConfigurationError, match="CORS_ALLOWED_ORIGINS"):
            _settings_with(cors_allowed_origins=("http://localhost:5173", "*"))

    def test_specific_origins_are_accepted(self):
        settings = _settings_with(cors_allowed_origins=("http://localhost:5173",))
        assert settings.cors_allowed_origins == ("http://localhost:5173",)

    def test_empty_origins_is_accepted(self):
        settings = _settings_with(cors_allowed_origins=())
        assert settings.cors_allowed_origins == ()
