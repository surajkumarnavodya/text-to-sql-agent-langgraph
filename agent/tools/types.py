"""Generic tool abstraction (MCP-shaped): a uniform way to describe and
govern every "source" this application can call -- SQL, document/policy
RAG, web search, media search, media generation -- without changing how
any of them actually work internally.

This is additive infrastructure, not a replacement for
`agent/orchestrator/nodes.py`'s existing hardcoded per-source nodes, which
stay exactly as they are (see that module's own docstring for why: each
node calls its own module directly, deliberately, for inspectability). The
`Tool` objects in `agent/tools/definitions.py` wrap those same real
functions (`agent.graph.run_agent`, `rag.graph.run_rag`,
`search.web_search.web_search`, `media.search.search_media`,
`agent.orchestrator.nodes.execute_generation`) so a *new* consumer -- a
future research planner that needs to choose and invoke a tool
dynamically, or an external MCP client -- has one governed entry point
with a consistent name/description/schema/permission/timeout/retry/audit
contract, instead of five different call shapes scattered across five
modules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.authz import Permission


class ToolCategory(str, Enum):
    """READ tools only retrieve information; WRITE tools create/change
    something (spend money, persist state, call a paid external API).

    Master-prompt requirement: "Clearly separate READ operations from
    WRITE operations. Sensitive external actions must require explicit
    authorization." `ToolRegistry.execute` does not itself grant that
    authorization -- each WRITE tool's own handler (today, only
    `media_generation`, wrapping `execute_generation`) already enforces
    its own human-approval gate upstream in `generation_node` before this
    tool is ever reached. The category is what lets a caller (or an audit
    query over `security.audit_log`'s `tool_executed` events) distinguish
    the two classes at a glance.
    """

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts a tool call gets and how long to wait between them.

    `max_attempts=1` (the default) means "no retry" -- most of the tools
    this module wraps already have their own internal retry/self-correction
    loop (the SQL pipeline's adaptively-widened retry budget, the RAG
    subgraph's bounded rewrite/retry), and re-retrying the *whole* call on
    top of that would just multiply latency, not improve reliability.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be >= 0")


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one `ToolRegistry.execute` call."""

    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    duration_seconds: float = 0.0
    attempts: int = 0


class ToolError(Exception):
    """Base class for every error this package raises directly.

    Not raised for a wrapped handler's own failure -- that is caught and
    turned into a failed `ToolResult` instead (see `ToolRegistry.execute`),
    matching this codebase's existing "a source failure must not crash the
    whole run" posture (`agent/orchestrator/nodes.py`'s own `except
    Exception` handling at each node).
    """


class ToolNotFoundError(ToolError):
    """Raised by `ToolRegistry.get`/`execute` for an unregistered tool name."""


class ToolPermissionError(ToolError):
    """Raised by `ToolRegistry.execute` when the caller's roles don't grant
    the tool's required permission.

    Fail-closed: raised, not swallowed into a failed `ToolResult`, so a
    caller can't mistake "denied" for "the tool itself failed" -- mirrors
    `agent.authz`'s own fail-closed posture on an unrecognized role.
    """


class ToolTimeoutError(ToolError):
    """Raised internally when a tool's handler exceeds `Tool.timeout_seconds`.

    Never propagates out of `ToolRegistry.execute` itself -- it is caught
    there and turned into a failed `ToolResult` on the final retry attempt,
    the same as any other handler failure, since a timeout is an ordinary,
    expected failure mode for an external call, not an authorization or
    configuration error.
    """


@dataclass(frozen=True)
class Tool:
    """One governed capability.

    `handler` takes a single `dict[str, Any]` (the tool's input) and
    returns any JSON-ish value -- deliberately loose rather than a
    dedicated Pydantic input/output model for every tool, since the real
    functions this wraps already have their own well-typed signatures
    (`agent.graph.run_agent`, `rag.graph.run_rag`, ...); each handler in
    `definitions.py` is a thin adapter mapping the dict shape to that
    function's actual parameters. `input_schema`/`output_schema` (optional
    model classes) let a caller validate a dict-shaped input/output when
    useful (e.g. a future MCP client), without forcing every internal
    caller to construct a model just to invoke a tool it already knows the
    shape of -- none of the six tools in `definitions.py` set these today.

    `permission=None` means no permission check is performed -- reserved
    for a genuinely unrestricted tool; every tool registered in
    `definitions.py` sets a real `Permission`.
    """

    name: str
    description: str
    category: ToolCategory
    handler: Callable[[dict[str, Any]], Any]
    permission: Permission | None = None
    timeout_seconds: float = 30.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    input_schema: type[Any] | None = None
    output_schema: type[Any] | None = None
