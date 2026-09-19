"""Unit tests for search/web_search.py -- the Tavily request shape,
response parsing, and the SUPPORTED_SEARCH_PROVIDERS dispatch. `httpx.post`
is mocked throughout; no real Tavily API is ever contacted.

Named as the specific, previously-disclosed gap this closes: `CLAUDE.md`'s
"Known gaps / follow-ups" section states "`search/` still has no dedicated
`pytest` unit test file" -- `search/web_search.py` itself was previously
verified only by running it against a real Tavily instance during
development, not by a regression suite. This file is that suite.
"""

from __future__ import annotations

import pytest

from config.settings import ConfigurationError, Settings
from search.web_search import (
    SUPPORTED_SEARCH_PROVIDERS,
    WebResult,
    WebSearchNotConfiguredError,
    web_search,
)
from security.secrets import SecretStr


class _FakeResponse:
    """Mirrors `tests/test_moderation_provider.py`'s own `_FakeResponse`
    pattern -- the established convention in this codebase for mocking an
    `httpx.post(...).raise_for_status().json()` call chain."""

    def __init__(self, payload: dict, status_error: Exception | None = None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def json(self) -> dict:
        return self._payload


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "web_search_provider": "tavily",
        "web_search_api_key": SecretStr("fake-tavily-key"),
        "web_search_max_results": 5,
    }
    base.update(overrides)
    return Settings(**base)


class TestNotConfigured:
    def test_raises_when_api_key_missing(self):
        settings = _settings(web_search_api_key=None)

        with pytest.raises(WebSearchNotConfiguredError):
            web_search("what is the capital of France", settings)

    def test_error_message_names_the_env_var_and_where_to_get_a_key(self):
        settings = _settings(web_search_api_key=None)

        with pytest.raises(WebSearchNotConfiguredError, match="WEB_SEARCH_API_KEY"):
            web_search("anything", settings)


class TestUnsupportedProvider:
    def test_raises_configuration_error_for_an_unregistered_provider(self):
        settings = _settings(web_search_provider="bing")

        with pytest.raises(ConfigurationError, match="Unsupported WEB_SEARCH_PROVIDER"):
            web_search("anything", settings)

    def test_error_message_lists_the_actually_supported_providers(self):
        settings = _settings(web_search_provider="bing")

        with pytest.raises(ConfigurationError, match="tavily"):
            web_search("anything", settings)


class TestTavilyRequestConstruction:
    def test_sends_the_api_key_query_and_max_results(self, monkeypatch):
        captured = {}

        def _fake_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return _FakeResponse({"results": []})

        monkeypatch.setattr("search.web_search.httpx.post", _fake_post)
        settings = _settings(web_search_max_results=3)

        web_search("current interest rates", settings)

        assert captured["url"] == "https://api.tavily.com/search"
        assert captured["json"]["api_key"] == "fake-tavily-key"
        assert captured["json"]["query"] == "current interest rates"
        assert captured["json"]["max_results"] == 3

    def test_never_logs_or_sends_the_raw_secret_string_representation(self, monkeypatch):
        """The api_key sent must be the real value (`.get_secret_value()`),
        not SecretStr's own masked `str()` -- a regression here would
        silently send/log the literal string "**********" instead of a
        working key, exactly the failure mode `CLAUDE.md`'s "Pydantic-based
        configuration and validation" section warns about for every
        SecretStr field in this codebase."""
        captured = {}
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: captured.update(json) or _FakeResponse({"results": []}),
        )
        settings = _settings(web_search_api_key=SecretStr("sk-real-value-123"))

        web_search("anything", settings)

        assert captured["api_key"] == "sk-real-value-123"
        assert "*" not in captured["api_key"]

    def test_uses_advanced_search_depth(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: captured.update(json) or _FakeResponse({"results": []}),
        )

        web_search("anything", _settings())

        assert captured["search_depth"] == "advanced"


class TestTavilyResponseParsing:
    def test_maps_each_result_into_a_well_typed_webresult(self, monkeypatch):
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: _FakeResponse(
                {
                    "results": [
                        {
                            "title": "Example Article",
                            "url": "https://example.com/a",
                            "content": "Some snippet.",
                        },
                    ]
                }
            ),
        )

        results = web_search("anything", _settings())

        assert len(results) == 1
        result = results[0]
        assert isinstance(result, WebResult)
        assert result.title == "Example Article"
        assert result.url == "https://example.com/a"
        assert result.snippet == "Some snippet."
        assert result.retrieved_at  # a real ISO timestamp was stamped, not left blank

    def test_falls_back_to_the_url_as_title_when_title_is_missing(self, monkeypatch):
        """A defensive fallback worth locking in -- a Tavily result with no
        title must never surface as an empty/None title in the UI."""
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: _FakeResponse(
                {"results": [{"url": "https://example.com/untitled", "content": "..."}]}
            ),
        )

        results = web_search("anything", _settings())

        assert results[0].title == "https://example.com/untitled"

    def test_an_empty_results_list_produces_an_empty_list_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: _FakeResponse({"results": []}),
        )

        assert web_search("no matches for this", _settings()) == []

    def test_missing_results_key_entirely_is_treated_as_no_results(self, monkeypatch):
        """A malformed/unexpected Tavily response shape (no "results" key
        at all) must degrade to an empty list via `.get("results", [])`,
        never raise a KeyError up through this function."""
        monkeypatch.setattr(
            "search.web_search.httpx.post", lambda url, json, timeout: _FakeResponse({})
        )

        assert web_search("anything", _settings()) == []

    def test_an_http_error_status_propagates_to_the_caller(self, monkeypatch):
        """`raise_for_status()` failures (a 401/429/500 from Tavily) must
        reach the caller (`agent.orchestrator.nodes.web_search_node`) as a
        real `httpx.HTTPError`, per this function's own documented
        contract -- never silently swallowed here."""
        import httpx

        fake_request = httpx.Request("POST", "https://api.tavily.com/search")
        fake_response = httpx.Response(429, request=fake_request)
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: _FakeResponse(
                {},
                status_error=httpx.HTTPStatusError(
                    "429", request=fake_request, response=fake_response
                ),
            ),
        )

        with pytest.raises(httpx.HTTPStatusError):
            web_search("anything", _settings())


class TestProviderRegistry:
    def test_tavily_is_the_only_registered_provider_today(self):
        """Matches this codebase's `db.connection.SUPPORTED_DB_TYPES`
        pattern -- a regression here (an accidentally-removed or silently
        aliased provider) would break `WEB_SEARCH_PROVIDER=tavily`
        deployments without any test failing elsewhere."""
        assert set(SUPPORTED_SEARCH_PROVIDERS) == {"tavily"}
        assert callable(SUPPORTED_SEARCH_PROVIDERS["tavily"])

    def test_default_settings_uses_get_settings_when_none_is_passed(self, monkeypatch):
        """`web_search(query)` with no explicit settings must fall back to
        `config.settings.get_settings()`, the same convention every other
        settings-consuming function in this codebase follows."""
        settings = _settings()
        monkeypatch.setattr("search.web_search.get_settings", lambda: settings)
        monkeypatch.setattr(
            "search.web_search.httpx.post",
            lambda url, json, timeout: _FakeResponse({"results": []}),
        )

        # No explicit settings argument -- must not raise, must use the
        # monkeypatched get_settings() above.
        assert web_search("anything") == []
