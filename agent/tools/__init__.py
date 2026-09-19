"""Generic tool/MCP-shaped abstraction over this application's real
source implementations (SQL, document/policy RAG, web search, media
search, media generation).

See `agent/tools/registry.py` and `agent/tools/definitions.py`'s own
module docstrings for the full rationale; `docs/TOOLS.md` has the design
writeup and `docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md` has the broader
context this was scoped from.
"""

from __future__ import annotations

from agent.tools.definitions import build_default_registry
from agent.tools.registry import ToolRegistry
from agent.tools.types import (
    RetryPolicy,
    Tool,
    ToolCategory,
    ToolError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolResult,
    ToolTimeoutError,
)

__all__ = [
    "RetryPolicy",
    "Tool",
    "ToolCategory",
    "ToolError",
    "ToolNotFoundError",
    "ToolPermissionError",
    "ToolResult",
    "ToolTimeoutError",
    "ToolRegistry",
    "build_default_registry",
]
