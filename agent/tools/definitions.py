"""Concrete `Tool` registrations wrapping this application's real,
existing source implementations -- no reimplementation, no duplicated
logic, exactly the principle `agent/orchestrator/nodes.py`'s own module
docstring already states for its five hardcoded nodes. Each tool's handler
calls straight through to the same function `agent/orchestrator/nodes.py`
calls today:

    sql_query          -> agent.graph.run_agent
    document_search    -> rag.graph.run_rag(collection="documents")
    policy_search      -> rag.graph.run_rag(collection="policies")
    web_search         -> search.web_search.web_search
    media_search       -> media.search.search_media
    media_generation   -> agent.orchestrator.nodes.execute_generation

This module does NOT change how `agent/orchestrator/nodes.py` invokes any
of these today -- the orchestrator graph's own hardcoded nodes keep calling
these functions directly, unmodified, exactly as before this module
existed. What this module adds is a second, governed way to reach the same
six capabilities: one a future dynamic consumer (a research planner that
needs to choose and invoke a tool by name rather than following a fixed
graph edge, or an external MCP client) can use without needing to know any
of these modules' own distinct call shapes. See `docs/TOOLS.md` for the
full rationale and `agent/tools/registry.py` for what "governed" means
(permission check, timeout, retry, audit logging).

Timeout values below are conservative, hand-picked defaults (not yet
wired to `config.settings.Settings`, to keep this increment's surface area
small) -- see `docs/TOOLS.md`'s "Known limitations" for the follow-up.
"""

from __future__ import annotations

from typing import Any

from agent.authz import Permission
from agent.tools.registry import ToolRegistry
from agent.tools.types import RetryPolicy, Tool, ToolCategory

# Conservative, hand-picked per-tool timeouts. The SQL and RAG pipelines
# each have their own internal retry budgets and multiple LLM calls, so
# they get the most headroom; web/media search are single external calls
# and get less; media generation can involve a real provider-side
# image/video generation job, so it gets the most.
_SQL_TOOL_TIMEOUT_SECONDS = 60.0
_RAG_TOOL_TIMEOUT_SECONDS = 45.0
_WEB_SEARCH_TOOL_TIMEOUT_SECONDS = 20.0
_MEDIA_SEARCH_TOOL_TIMEOUT_SECONDS = 20.0
_MEDIA_GENERATION_TOOL_TIMEOUT_SECONDS = 120.0


def _sql_query_handler(input_data: dict[str, Any]) -> Any:
    from agent.graph import run_agent

    return run_agent(
        input_data["question"],
        input_data.get("conversation_history"),
        input_data.get("enable_insight", True),
        tuple(input_data.get("caller_roles", ())),
    )


def _make_rag_handler(collection: str) -> Any:
    def _handler(input_data: dict[str, Any]) -> Any:
        from rag.graph import run_rag

        return run_rag(
            input_data["question"],
            collection,  # type: ignore[arg-type]
            input_data.get("settings"),
            tuple(input_data.get("caller_roles", ())),
        )

    return _handler


def _web_search_handler(input_data: dict[str, Any]) -> Any:
    from search.web_search import web_search

    return web_search(input_data["question"], input_data.get("settings"))


def _media_search_handler(input_data: dict[str, Any]) -> Any:
    from config.settings import get_settings
    from media.search import search_media

    settings = input_data.get("settings") or get_settings()
    return search_media(
        input_data["question"],
        settings,
        input_data.get("media_type", "any"),
        input_data.get("top_k"),
    )


def _media_generation_handler(input_data: dict[str, Any]) -> Any:
    from agent.orchestrator.nodes import execute_generation
    from config.settings import get_settings

    settings = input_data.get("settings") or get_settings()
    return execute_generation(input_data["question"], input_data["kind"], settings)


def build_default_registry() -> ToolRegistry:
    """Builds a fresh `ToolRegistry` with all six of this application's
    real sources registered.

    Each handler resolves its own `Settings` at call time (via
    `input_data["settings"]` if the caller supplies one, else the cached
    `config.settings.get_settings()`/the wrapped function's own internal
    default) rather than pinning one at registry-build time -- the same
    "don't pin a stale settings snapshot" posture the orchestrator nodes
    already have by calling `get_settings()` themselves on every
    invocation.
    """
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="sql_query",
            description=(
                "Answer a question against the company's structured databases -- "
                "customer, sales/order, financial, or HR data -- by generating, "
                "validating, and executing read-only SQL."
            ),
            category=ToolCategory.READ,
            permission=Permission.EXECUTE_SQL,
            timeout_seconds=_SQL_TOOL_TIMEOUT_SECONDS,
            handler=_sql_query_handler,
        )
    )
    registry.register(
        Tool(
            name="document_search",
            description=(
                "Search general uploaded PDF documents (the default document "
                "collection, not the access-restricted policy one) for an answer."
            ),
            category=ToolCategory.READ,
            permission=Permission.DOCUMENTS_READ,
            timeout_seconds=_RAG_TOOL_TIMEOUT_SECONDS,
            handler=_make_rag_handler("documents"),
        )
    )
    registry.register(
        Tool(
            name="policy_search",
            description=(
                "Search the access-restricted policy document collection "
                "(compensation, disciplinary, or legal/compliance content only)."
            ),
            category=ToolCategory.READ,
            permission=Permission.POLICY_RAG_QUERY,
            timeout_seconds=_RAG_TOOL_TIMEOUT_SECONDS,
            handler=_make_rag_handler("policies"),
        )
    )
    registry.register(
        Tool(
            name="web_search",
            description="Search the live web for current or external information.",
            category=ToolCategory.READ,
            permission=Permission.WEB_SEARCH,
            timeout_seconds=_WEB_SEARCH_TOOL_TIMEOUT_SECONDS,
            handler=_web_search_handler,
        )
    )
    registry.register(
        Tool(
            name="media_search",
            description=(
                "Search a local, untagged image/video library by plain-English "
                "content description."
            ),
            category=ToolCategory.READ,
            permission=Permission.MEDIA_SEARCH,
            timeout_seconds=_MEDIA_SEARCH_TOOL_TIMEOUT_SECONDS,
            handler=_media_search_handler,
        )
    )
    registry.register(
        Tool(
            name="media_generation",
            description=(
                "Generate a brand-new image or video via a real, metered provider "
                "call. WRITE: spends real money, and -- upstream, in "
                "`generation_node` -- requires explicit human approval before "
                "this tool is ever invoked (see `Settings"
                ".require_generation_approval`)."
            ),
            category=ToolCategory.WRITE,
            permission=Permission.MEDIA_GENERATE,
            timeout_seconds=_MEDIA_GENERATION_TOOL_TIMEOUT_SECONDS,
            retry_policy=RetryPolicy(max_attempts=1),
            handler=_media_generation_handler,
        )
    )
    return registry
