"""`ToolRegistry`: the single governed entry point for invoking a `Tool`.

Enforces, uniformly, for any registered tool:
  - a permission check (fail closed) before the handler ever runs, using
    the exact same `agent.authz.has_role_permission` call
    `agent/orchestrator/nodes.py::router_node` already uses for its own
    per-source authorization gate
  - a hard per-attempt timeout, thread-based (mirroring
    `db.execution._execute_with_timeout`'s own daemon-thread-join pattern
    -- the only cross-platform way to bound an arbitrary Python call's
    wall-clock time, since not every handler here does I/O a driver-level
    `SET statement_timeout` could intercept)
  - the tool's configured retry policy
  - one structured `security.audit_log` event per outcome (denied /
    succeeded / failed-on-every-attempt)
  - a `ToolResult`, never a raised exception from a handler's own failure
    (only `ToolNotFoundError`/`ToolPermissionError` propagate out of
    `execute` -- see their own docstrings in `agent/tools/types.py`)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from agent.authz import has_role_permission
from agent.tools.types import (
    Tool,
    ToolCategory,
    ToolNotFoundError,
    ToolPermissionError,
    ToolResult,
)
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)


def _run_with_timeout(handler: Any, input_data: dict[str, Any], timeout_seconds: float) -> Any:
    """Runs `handler(input_data)` on a daemon worker thread, raising
    `TimeoutError` if it hasn't finished within `timeout_seconds`.

    Mirrors `db.execution._execute_with_timeout`'s own join-based pattern.
    Unlike that function there is no connection to force-close here, so a
    handler that ignores the timeout keeps running in the background on
    its daemon thread (it will not block process exit) even though the
    caller has already moved on with a failed `ToolResult`. This is an
    accepted limitation, identical in shape to Python's own lack of a
    portable "kill this thread" primitive -- every wrapped tool's own real
    work (an HTTP call, an LLM call, a DB query) already has its own inner
    timeout enforced closer to the actual I/O (the SQL pipeline's own
    `db.execution` timeout, `httpx` client timeouts for web search), so
    this is a backstop, not the only layer.
    """
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["value"] = handler(input_data)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        raise TimeoutError(f"Handler exceeded {timeout_seconds}s.")
    if "error" in error:
        raise error["error"]
    return result["value"]


class ToolRegistry:
    """A name -> `Tool` map plus a governed `execute`.

    Not a process-lifetime singleton itself -- `agent.tools.definitions
    .build_default_registry()` builds a fresh one each call, mirroring
    `agent.orchestrator.graph.build_orchestrator_graph`'s own
    cheap-to-rebuild posture; a caller that wants one process-lifetime
    instance can wrap that call in its own `functools.cache`, the same
    pattern `config.settings.get_settings` already establishes elsewhere
    in this codebase.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"A tool named {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"No tool registered as {name!r}.") from None

    def list_tools(self, category: ToolCategory | None = None) -> list[Tool]:
        tools = list(self._tools.values())
        if category is not None:
            tools = [t for t in tools if t.category == category]
        return sorted(tools, key=lambda t: t.name)

    def execute(
        self,
        name: str,
        input_data: dict[str, Any],
        *,
        caller_roles: tuple[str, ...] = (),
        actor: str | None = None,
    ) -> ToolResult:
        """Runs `name`'s handler against `input_data`, governed as described
        in this module's docstring.

        Args:
            caller_roles: The invoking identity's roles
                (`security.oidc.AuthIdentity.roles`) -- checked against
                `tool.permission` exactly the way `agent.orchestrator.nodes
                .router_node` already checks `_SOURCE_PERMISSIONS`,
                fail-closed on an empty/unrecognized role set.
            actor: An optional human-readable identifier for the caller,
                for the audit log only -- never used for authorization.

        Raises:
            ToolNotFoundError: `name` isn't registered.
            ToolPermissionError: `caller_roles` doesn't grant `tool.permission`.
        """
        tool = self.get(name)

        if tool.permission is not None and not has_role_permission(caller_roles, tool.permission):
            log_security_event(
                "tool_permission_denied",
                "warning",
                "A tool invocation was denied because the caller's role(s) do not "
                "grant the required permission.",
                tool=name,
                required_permission=tool.permission.value,
                caller_roles=list(caller_roles),
                actor=actor,
            )
            raise ToolPermissionError(
                f"Caller roles {caller_roles!r} do not grant {tool.permission.value!r}, "
                f"required by tool {name!r}."
            )

        # Forward the already-authorized caller_roles into the handler's own
        # input dict (unless the caller explicitly overrode it there) so a
        # handler that needs to pass roles on to the wrapped function (e.g.
        # `run_agent`/`run_rag`'s own `caller_roles` parameter -- see
        # `agent/tools/definitions.py`) doesn't require the caller to
        # specify the same roles twice, once for the permission check above
        # and again inside `input_data`.
        call_input = dict(input_data)
        call_input.setdefault("caller_roles", caller_roles)

        attempts = 0
        last_error: str | None = None
        overall_start = time.monotonic()
        while attempts < tool.retry_policy.max_attempts:
            attempts += 1
            try:
                output = _run_with_timeout(tool.handler, call_input, tool.timeout_seconds)
            except TimeoutError:
                last_error = (
                    f"Tool {name!r} exceeded its {tool.timeout_seconds}s timeout "
                    f"(attempt {attempts}/{tool.retry_policy.max_attempts})."
                )
                logger.warning("[tools] %s", last_error)
            except Exception as exc:  # noqa: BLE001 - a handler failure must not crash the caller
                last_error = str(exc)
                logger.warning(
                    "[tools] tool=%s attempt=%d/%d failed: %s",
                    name,
                    attempts,
                    tool.retry_policy.max_attempts,
                    last_error,
                )
            else:
                duration = time.monotonic() - overall_start
                log_security_event(
                    "tool_executed",
                    "info",
                    "A tool invocation completed successfully.",
                    tool=name,
                    category=tool.category.value,
                    attempts=attempts,
                    duration_seconds=round(duration, 3),
                    actor=actor,
                )
                return ToolResult(
                    tool_name=name,
                    success=True,
                    output=output,
                    duration_seconds=duration,
                    attempts=attempts,
                )

            if attempts < tool.retry_policy.max_attempts and tool.retry_policy.backoff_seconds > 0:
                time.sleep(tool.retry_policy.backoff_seconds)

        duration = time.monotonic() - overall_start
        log_security_event(
            "tool_execution_failed",
            "warning",
            "A tool invocation failed on every retry attempt.",
            tool=name,
            category=tool.category.value,
            attempts=attempts,
            duration_seconds=round(duration, 3),
            error=last_error,
            actor=actor,
        )
        return ToolResult(
            tool_name=name,
            success=False,
            error=last_error,
            duration_seconds=duration,
            attempts=attempts,
        )
