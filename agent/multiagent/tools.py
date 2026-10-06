"""The governed tool set for one supervised turn (Prompt 34).

Every tool is a thin binding over an existing function, registered in the
existing `agent.tools.registry.ToolRegistry`. Each binding gets its permission,
timeout and retry policy from there. The tenant, the model and the conversation
history are bound by closure when the registry is built for one turn. They are
never read from tool input, so an agent cannot choose them.

`caller_roles` is supplied by `ToolRegistry.execute` from the real caller, and
`ToolGateway` refuses any input that tries to set it. The `run_agent` binding
therefore always runs under the real caller's roles.

The module-level names (`run_agent`, `plan_subquestions`, ...) are looked up at
call time, so a test can replace one without touching the others.
"""

from __future__ import annotations

from typing import Any

from analytics.engine import compute_analytics_result
from analytics.forecasting import generate_forecast
from analytics.root_cause import investigate_root_cause
from retrieval.retriever import extract_governing_metrics, retrieve_business_context

from agent.analyst.planner import plan_subquestions
from agent.authz import Permission
from agent.graph import run_agent
from agent.state import ConversationExchange
from agent.tools.registry import ToolRegistry
from agent.tools.types import RetryPolicy, Tool, ToolCategory
from config.settings import Settings

TOOL_LLM_PLAN = "llm_plan"
TOOL_GOVERNED_SQL = "governed_sql"
TOOL_BUSINESS_CONTEXT = "business_context"
TOOL_ANALYTICS = "analytics_engine"
TOOL_FORECAST = "forecast_engine"
TOOL_ROOT_CAUSE = "root_cause_engine"


def build_supervisor_registry(
    *,
    settings: Settings,
    model: str | None,
    tenant_id: str | None,
    conversation_history: list[ConversationExchange],
) -> ToolRegistry:
    """Builds the per-turn registry. `model`, `tenant_id` and the history are
    fixed by closure here, not by any caller of a tool.

    Retry policy: `governed_sql` gets one attempt, because `run_agent` already
    runs its own bounded self-correction loop and a whole-call retry would
    multiply cost. `business_context` retries once on a transient failure. The
    engine tools are deterministic and get one attempt.
    """
    registry = ToolRegistry()
    timeout = settings.analyst_timeout_seconds

    def _sql(payload: dict[str, Any]) -> Any:
        return run_agent(
            question=payload["question"],
            conversation_history=list(conversation_history),
            enable_insight=False,
            caller_roles=tuple(payload["caller_roles"]),
            model=model,
            tenant_id=tenant_id,
            forecast_horizon=None,
        )

    def _plan(payload: dict[str, Any]) -> Any:
        texts, mode = plan_subquestions(payload["question"], settings, model, payload["max_items"])
        return {"texts": list(texts), "mode": mode}

    def _context(payload: dict[str, Any]) -> Any:
        result = retrieve_business_context(
            payload["question"], payload["database_id"], tuple(payload["caller_roles"]), settings
        )
        return {"metrics": extract_governing_metrics(result.items)}

    def _analytics(payload: dict[str, Any]) -> Any:
        return compute_analytics_result(payload["columns"], payload["rows"], settings).model_dump()

    def _forecast(payload: dict[str, Any]) -> Any:
        return generate_forecast(
            payload["points"], horizon=payload["horizon"], settings=settings
        ).model_dump()

    def _root_cause(payload: dict[str, Any]) -> Any:
        return investigate_root_cause(
            payload["current_rows"],
            payload["baseline_rows"],
            payload["columns"],
            payload["dimension_columns"],
            payload["value_column"],
            settings,
        ).model_dump()

    specs = [
        (
            TOOL_GOVERNED_SQL,
            "Run one question through the governed Text-to-SQL pipeline.",
            ToolCategory.READ,
            _sql,
            Permission.EXECUTE_SQL,
            RetryPolicy(max_attempts=1),
        ),
        (
            TOOL_LLM_PLAN,
            "Decompose one question into sub-questions (fail-open).",
            ToolCategory.READ,
            _plan,
            Permission.ASK,
            RetryPolicy(max_attempts=1),
        ),
        (
            TOOL_BUSINESS_CONTEXT,
            "Retrieve governed metric definitions for a question.",
            ToolCategory.READ,
            _context,
            Permission.ASK,
            RetryPolicy(max_attempts=2),
        ),
        (
            TOOL_ANALYTICS,
            "Deterministic statistics over an executed result.",
            ToolCategory.READ,
            _analytics,
            Permission.ASK,
            RetryPolicy(max_attempts=1),
        ),
        (
            TOOL_FORECAST,
            "Deterministic forecast over a historical series (an estimate).",
            ToolCategory.READ,
            _forecast,
            Permission.ASK,
            RetryPolicy(max_attempts=1),
        ),
        (
            TOOL_ROOT_CAUSE,
            "Deterministic contribution breakdown between two result sets.",
            ToolCategory.READ,
            _root_cause,
            Permission.ASK,
            RetryPolicy(max_attempts=1),
        ),
    ]
    for name, description, category, handler, permission, retry in specs:
        registry.register(
            Tool(
                name=name,
                description=description,
                category=category,
                handler=handler,
                permission=permission,
                timeout_seconds=timeout,
                retry_policy=retry,
            )
        )
    return registry
