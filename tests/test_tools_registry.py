"""Unit tests for `agent.tools.registry.ToolRegistry` -- fully isolated
from the real source implementations (fake handlers only). See
`tests/test_tools_definitions.py` for tests that confirm the concrete
`build_default_registry()` tools wire through to the real underlying
functions.
"""

from __future__ import annotations

import time

import pytest

import security.audit_log as audit_log
from agent.authz import Permission
from agent.tools.registry import ToolRegistry
from agent.tools.types import (
    RetryPolicy,
    Tool,
    ToolCategory,
    ToolNotFoundError,
    ToolPermissionError,
)


def _make_tool(**overrides: object) -> Tool:
    defaults: dict[str, object] = {
        "name": "echo",
        "description": "Echoes its input back.",
        "category": ToolCategory.READ,
        "handler": lambda input_data: input_data,
        "permission": None,
        "timeout_seconds": 5.0,
        "retry_policy": RetryPolicy(),
    }
    defaults.update(overrides)
    return Tool(**defaults)  # type: ignore[arg-type]


class TestRegistration:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        tool = _make_tool()
        registry.register(tool)
        assert registry.get("echo") is tool

    def test_duplicate_registration_raises(self) -> None:
        registry = ToolRegistry()
        registry.register(_make_tool())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_make_tool())

    def test_get_unknown_raises_not_found(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError):
            registry.get("does-not-exist")

    def test_list_tools_sorted_and_filterable(self) -> None:
        registry = ToolRegistry()
        registry.register(_make_tool(name="zebra", category=ToolCategory.WRITE))
        registry.register(_make_tool(name="alpha", category=ToolCategory.READ))
        assert [t.name for t in registry.list_tools()] == ["alpha", "zebra"]
        assert [t.name for t in registry.list_tools(ToolCategory.WRITE)] == ["zebra"]
        assert [t.name for t in registry.list_tools(ToolCategory.READ)] == ["alpha"]


class TestPermissionEnforcement:
    def test_no_permission_required_always_allowed(self) -> None:
        registry = ToolRegistry()
        registry.register(_make_tool(permission=None))
        result = registry.execute("echo", {"x": 1}, caller_roles=())
        assert result.success is True
        assert result.output == {"x": 1, "caller_roles": ()}

    def test_denied_when_role_lacks_permission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[tuple[str, str, str, dict[str, object]]] = []
        monkeypatch.setattr(
            audit_log,
            "log_security_event",
            lambda event_type, severity, detail, **ctx: events.append(
                (event_type, severity, detail, ctx)
            ),
        )
        # registry.py imports log_security_event by name, so patch it there too.
        import agent.tools.registry as registry_module

        monkeypatch.setattr(
            registry_module,
            "log_security_event",
            lambda event_type, severity, detail, **ctx: events.append(
                (event_type, severity, detail, ctx)
            ),
        )

        registry = ToolRegistry()
        registry.register(_make_tool(permission=Permission.EXECUTE_SQL))

        with pytest.raises(ToolPermissionError):
            registry.execute("echo", {}, caller_roles=("viewer",))

        assert events, "expected a tool_permission_denied audit event"
        event_type, severity, _detail, ctx = events[0]
        assert event_type == "tool_permission_denied"
        assert severity == "warning"
        assert ctx["tool"] == "echo"
        assert ctx["required_permission"] == Permission.EXECUTE_SQL.value

    def test_allowed_when_role_grants_permission(self) -> None:
        registry = ToolRegistry()
        registry.register(_make_tool(permission=Permission.EXECUTE_SQL))
        result = registry.execute("echo", {}, caller_roles=("user",))
        assert result.success is True

    def test_caller_roles_forwarded_into_handler_input(self) -> None:
        registry = ToolRegistry()
        registry.register(_make_tool())
        result = registry.execute("echo", {"question": "hi"}, caller_roles=("user", "analyst"))
        assert result.output == {"question": "hi", "caller_roles": ("user", "analyst")}

    def test_explicit_caller_roles_in_input_data_wins(self) -> None:
        registry = ToolRegistry()
        registry.register(_make_tool())
        result = registry.execute("echo", {"caller_roles": ("override",)}, caller_roles=("user",))
        assert result.output == {"caller_roles": ("override",)}


class TestExecutionOutcomes:
    def test_successful_execution_logs_audit_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[tuple[str, str, dict[str, object]]] = []
        import agent.tools.registry as registry_module

        monkeypatch.setattr(
            registry_module,
            "log_security_event",
            lambda event_type, severity, detail, **ctx: events.append((event_type, severity, ctx)),
        )

        registry = ToolRegistry()
        registry.register(_make_tool())
        result = registry.execute("echo", {"q": "hi"})

        assert result.success is True
        assert result.output == {"q": "hi", "caller_roles": ()}
        assert result.attempts == 1
        assert events[-1][0] == "tool_executed"
        assert events[-1][2]["tool"] == "echo"

    def test_handler_exception_returns_failed_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.tools.registry as registry_module

        monkeypatch.setattr(registry_module, "log_security_event", lambda *a, **k: None)

        def _boom(_input_data: dict[str, object]) -> None:
            raise RuntimeError("handler exploded")

        registry = ToolRegistry()
        registry.register(_make_tool(handler=_boom))
        result = registry.execute("echo", {})

        assert result.success is False
        assert result.error is not None
        assert "handler exploded" in result.error
        assert result.attempts == 1

    def test_timeout_returns_failed_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.tools.registry as registry_module

        monkeypatch.setattr(registry_module, "log_security_event", lambda *a, **k: None)

        def _slow(_input_data: dict[str, object]) -> None:
            time.sleep(0.5)

        registry = ToolRegistry()
        registry.register(_make_tool(handler=_slow, timeout_seconds=0.05))
        result = registry.execute("echo", {})

        assert result.success is False
        assert result.error is not None
        assert "timeout" in result.error.lower()

    def test_retry_policy_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.tools.registry as registry_module

        monkeypatch.setattr(registry_module, "log_security_event", lambda *a, **k: None)

        call_count = {"n": 0}

        def _flaky(_input_data: dict[str, object]) -> str:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("not yet")
            return "ok"

        registry = ToolRegistry()
        registry.register(
            _make_tool(handler=_flaky, retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0))
        )
        result = registry.execute("echo", {})

        assert result.success is True
        assert result.output == "ok"
        assert result.attempts == 3

    def test_retry_policy_exhausted_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.tools.registry as registry_module

        monkeypatch.setattr(registry_module, "log_security_event", lambda *a, **k: None)

        def _always_fails(_input_data: dict[str, object]) -> None:
            raise RuntimeError("nope")

        registry = ToolRegistry()
        registry.register(
            _make_tool(
                handler=_always_fails,
                retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0),
            )
        )
        result = registry.execute("echo", {})

        assert result.success is False
        assert result.attempts == 2


class TestRetryPolicyValidation:
    def test_max_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)

    def test_backoff_seconds_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError, match="backoff_seconds"):
            RetryPolicy(backoff_seconds=-1.0)
