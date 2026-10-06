"""Enforcement for the multi-agent supervisor (Prompt 34): tool gateway,
circuit breakers, and turn budgets.

The gateway is the only path from an agent to a tool. It enforces three
things in order:

1. The tool is on the calling agent's allowlist (`AgentSpec.allowed_tools`).
2. The tool input carries no identity override. `caller_roles` and `tenant_id`
   are set by the server, never by an agent or a model. `ToolRegistry.execute`
   uses `setdefault` for `caller_roles`, so a value in the input would override
   the roles the handler sees. The gateway refuses such input rather than rely
   on the registry.
3. The registry's own permission check passes for the real caller roles.

Circuit breakers and turn budgets are the supervisor's cost and fault controls.
They decide whether a call is attempted at all.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from agent.multiagent.contracts import AgentSpec
from agent.tools.registry import ToolRegistry
from agent.tools.types import ToolResult
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)

# Keys an agent or model may never set on a tool call. Identity is server-side.
_IDENTITY_KEYS = frozenset({"caller_roles", "tenant_id", "actor"})


class ToolAccessDenied(Exception):
    """An agent asked for a tool outside its allowlist, or tried to supply
    identity in tool input. The call is never executed."""


class ToolGateway:
    """Runs one agent's tool calls, enforcing that agent's spec.

    One gateway per supervised turn. It holds the real caller's roles and
    actor, so no caller of `call` can supply its own.
    """

    def __init__(
        self, registry: ToolRegistry, caller_roles: tuple[str, ...], actor: str | None
    ) -> None:
        self._registry = registry
        self._caller_roles = tuple(caller_roles)
        self._actor = actor

    def call(self, spec: AgentSpec, tool_name: str, input_data: dict[str, Any]) -> ToolResult:
        """Runs `tool_name` on behalf of `spec`'s agent.

        Raises:
            ToolAccessDenied: the tool is not allowlisted for this agent, or
                the input tries to set identity.
            ToolPermissionError: the real caller lacks the tool's permission
                (raised by the registry itself).
        """
        if tool_name not in spec.allowed_tools:
            log_security_event(
                "agent_tool_denied",
                "warning",
                "An agent requested a tool outside its allowlist; the call was refused.",
                agent=spec.name.value,
                tool=tool_name,
            )
            raise ToolAccessDenied(f"Agent {spec.name.value!r} may not call tool {tool_name!r}.")
        forbidden = sorted(_IDENTITY_KEYS.intersection(input_data))
        if forbidden:
            log_security_event(
                "agent_identity_override_denied",
                "warning",
                "An agent tried to supply identity in tool input; the call was refused.",
                agent=spec.name.value,
                tool=tool_name,
                keys=forbidden,
            )
            raise ToolAccessDenied(f"Identity fields may not be supplied to tools: {forbidden}.")
        return self._registry.execute(
            tool_name,
            dict(input_data),
            caller_roles=self._caller_roles,
            actor=self._actor,
        )


@dataclass
class CircuitBreaker:
    """Stops calling an agent after repeated failures, then probes it again.

    Closed: calls allowed. After `failure_threshold` consecutive failures it
    opens and refuses calls. Once `cooldown_seconds` have passed it allows one
    probe (half-open). A success closes it. A failed probe reopens it.
    """

    name: str
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    _failures: int = 0
    _opened_at: float | None = None
    _probing: bool = False

    def allow(self, now: float) -> bool:
        if self._opened_at is None:
            return True
        if now - self._opened_at >= self.cooldown_seconds and not self._probing:
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self, now: float) -> None:
        self._failures += 1
        if self._probing or self._failures >= self.failure_threshold:
            self._opened_at = now
            self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._probing else "open"


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_breaker(
    name: str, failure_threshold: int = 3, cooldown_seconds: float = 60.0
) -> CircuitBreaker:
    """The process-wide breaker for one agent, created on first use with the
    thresholds the caller passes (from `Settings`). Process-local, like the
    other rate limiters in this codebase: a multi-worker deployment has one
    independent breaker per worker."""
    with _breakers_lock:
        breaker = _breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            )
            _breakers[name] = breaker
        return breaker


def reset_breakers() -> None:
    """Clears every breaker. Tests use this for isolation."""
    with _breakers_lock:
        _breakers.clear()


@dataclass
class TurnBudget:
    """Hard caps on one supervised turn: agent invocations and cost units.

    `charge` is checked before every agent call. It returns the reason the
    call is refused, or None when the call may proceed.
    """

    max_agent_calls: int
    max_cost_units: int
    calls: int = 0
    cost: int = 0
    refusals: list[str] = field(default_factory=list)

    def can_charge(self, units: int) -> str | None:
        if self.calls >= self.max_agent_calls:
            return "agent_call_budget_exhausted"
        if self.cost + units > self.max_cost_units:
            return "cost_budget_exhausted"
        return None

    def charge(self, units: int) -> None:
        self.calls += 1
        self.cost += units
