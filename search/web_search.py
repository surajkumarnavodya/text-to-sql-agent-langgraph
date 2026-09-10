"""Live web search, provider-configurable -- shaped like `db/connection.py`'s
`SUPPORTED_DB_TYPES` pattern (`WEB_SEARCH_PROVIDER` selects the implementation,
swapping providers is a `.env` change, not a code change) rather than a
single hardcoded vendor.

Only Tavily is implemented today (the provider signed off on for this
project); `SUPPORTED_SEARCH_PROVIDERS` is where a second provider (Bing,
SerpAPI, ...) would register its own call function -- `web_search()` itself
never needs to change.

Every result is wrapped in `WebResult` before it ever reaches a prompt --
`title`/`url`/`snippet`/`retrieved_at`, nothing else -- and the RAG/
orchestrator layer above this (`agent/orchestrator/nodes.py`) is responsible
for framing this content as untrusted, external, live data, never as
something from the company's own systems (same "data, not instructions"
principle as ingested PDF content -- see `rag/graph.py`'s generate-node
system prompt for the sibling case).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from config.settings import ConfigurationError, Settings, get_settings

logger = logging.getLogger(__name__)

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WebResult:
    """One search result -- the only shape that ever reaches a prompt."""

    title: str
    url: str
    snippet: str
    retrieved_at: str


class WebSearchNotConfiguredError(ConfigurationError):
    """Raised when web search is enabled but no API key is set for the configured provider."""


def _tavily_search(query: str, settings: Settings) -> list[WebResult]:
    if not settings.web_search_api_key:
        raise WebSearchNotConfiguredError(
            "ENABLE_WEB_SEARCH is on but WEB_SEARCH_API_KEY is not set in .env. "
            "Get a key at https://tavily.com and set WEB_SEARCH_API_KEY -- see "
            ".env.example's web search section."
        )
    response = httpx.post(
        _TAVILY_ENDPOINT,
        json={
            "api_key": str(settings.web_search_api_key),
            "query": query,
            "max_results": settings.web_search_max_results,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    retrieved_at = datetime.datetime.now(datetime.UTC).isoformat()
    return [
        WebResult(
            title=item.get("title") or item.get("url", ""),
            url=item.get("url", ""),
            snippet=item.get("content", ""),
            retrieved_at=retrieved_at,
        )
        for item in payload.get("results", [])
    ]


# Provider name -> call function. Every entry must accept exactly
# (query: str, settings: Settings) and return list[WebResult].
SUPPORTED_SEARCH_PROVIDERS: dict[str, Callable[[str, Settings], list[WebResult]]] = {
    "tavily": _tavily_search,
}


def web_search(query: str, settings: Settings | None = None) -> list[WebResult]:
    """Runs a live web search via the configured provider.

    Raises:
        ConfigurationError: if `WEB_SEARCH_PROVIDER` isn't one of
            `SUPPORTED_SEARCH_PROVIDERS`.
        WebSearchNotConfiguredError: if the provider's required API key isn't set.
        httpx.HTTPError: on a network/API failure -- callers (the
            orchestrator's web_search node) handle this the same way
            `execute_sql_node` handles a `SQLAlchemyError`: classify, log,
            degrade gracefully rather than crash the whole run.
    """
    settings = settings or get_settings()
    provider = settings.web_search_provider
    call = SUPPORTED_SEARCH_PROVIDERS.get(provider)
    if call is None:
        raise ConfigurationError(
            f"Unsupported WEB_SEARCH_PROVIDER={provider!r}. Supported values: "
            f"{', '.join(sorted(SUPPORTED_SEARCH_PROVIDERS))}."
        )
    results = call(query, settings)
    logger.info("[web_search] provider=%s query=%r results=%d", provider, query, len(results))
    return results
