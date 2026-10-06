"""The specialist agents (Prompt 34): one spec and one handler each.

Each handler wraps an existing, already-tested function. It adds no new SQL,
no new statistics and no new authorization logic. Every tool call goes through
`ToolGateway`, and every claim it returns is checked by
`agent.multiagent.resolution` before it can reach the report.

Specialists:
- planner (llm): splits the question. Its output is text, which is re-checked
  and then routed. It can name no tool and no agent.
- governance (control): the request gate, and row redaction before any engine
  sees the rows. A deterministic control, not an LLM.
- semantic (retrieval): governed metric definitions (CONFIRMED_BUSINESS_TRUTH).
- sql_data (data): the only agent that touches the database, and only through
  `run_agent`. It is the sole source of DATABASE_FACT row counts.
- analytics (engine): statistics over the governed rows. Reads, never recomputes SQL.
- forecast (engine): projections. Always AI_INFERENCE, never observed fact.
- root_cause (engine): contribution attribution. Always AI_INFERENCE.
- recommendation (engine): dedupes the sub-runs' recommendations and links
  them to evidence. Always AI_INFERENCE.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from governance.request_policy import evaluate_request
from governance.result_policy import govern_rows_for_caller

from agent.analyst.evidence import build_outcome
from agent.analyst.graph import recommend_node
from agent.analyst.state import AnalystState
from agent.multiagent.contracts import AgentName, AgentOutput, AgentSpec, Claim
from agent.multiagent.policy import ToolGateway
from agent.multiagent.tools import (
    TOOL_ANALYTICS,
    TOOL_BUSINESS_CONTEXT,
    TOOL_FORECAST,
    TOOL_GOVERNED_SQL,
    TOOL_LLM_PLAN,
    TOOL_ROOT_CAUSE,
)
from agent.provenance import DataTruthLevel
from agent.tools.types import ToolResult
from config.settings import Settings

SPECS: dict[AgentName, AgentSpec] = {
    AgentName.PLANNER: AgentSpec(
        name=AgentName.PLANNER,
        kind="llm",
        allowed_tools=frozenset({TOOL_LLM_PLAN}),
        max_truth_level=DataTruthLevel.AI_INFERENCE,
        cost_units=3,
    ),
    AgentName.GOVERNANCE: AgentSpec(
        name=AgentName.GOVERNANCE,
        kind="control",
        allowed_tools=frozenset(),
        max_truth_level=DataTruthLevel.AI_INFERENCE,
        cost_units=0,
    ),
    AgentName.SEMANTIC: AgentSpec(
        name=AgentName.SEMANTIC,
        kind="retrieval",
        allowed_tools=frozenset({TOOL_BUSINESS_CONTEXT}),
        max_truth_level=DataTruthLevel.CONFIRMED_BUSINESS_TRUTH,
        cost_units=2,
        max_attempts=2,
    ),
    AgentName.SQL_DATA: AgentSpec(
        name=AgentName.SQL_DATA,
        kind="data",
        allowed_tools=frozenset({TOOL_GOVERNED_SQL}),
        max_truth_level=DataTruthLevel.DATABASE_FACT,
        cost_units=5,
    ),
    AgentName.ANALYTICS: AgentSpec(
        name=AgentName.ANALYTICS,
        kind="engine",
        allowed_tools=frozenset({TOOL_ANALYTICS}),
        max_truth_level=DataTruthLevel.DATABASE_FACT,
        cost_units=1,
    ),
    AgentName.FORECAST: AgentSpec(
        name=AgentName.FORECAST,
        kind="engine",
        allowed_tools=frozenset({TOOL_FORECAST}),
        max_truth_level=DataTruthLevel.AI_INFERENCE,
        cost_units=1,
    ),
    AgentName.ROOT_CAUSE: AgentSpec(
        name=AgentName.ROOT_CAUSE,
        kind="engine",
        allowed_tools=frozenset({TOOL_ROOT_CAUSE}),
        max_truth_level=DataTruthLevel.AI_INFERENCE,
        cost_units=1,
    ),
    AgentName.RECOMMENDATION: AgentSpec(
        name=AgentName.RECOMMENDATION,
        kind="engine",
        allowed_tools=frozenset(),
        max_truth_level=DataTruthLevel.AI_INFERENCE,
        cost_units=0,
    ),
}


@dataclass
class DataSlot:
    """One sql_data task's result, held in memory for this turn only. Raw
    rows never leave the supervisor. They are not returned to the caller,
    logged or put in the trace."""

    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    governed_rows: list[Any] | None = None
    analytics: dict[str, Any] | None = None
    analytical_intent: str | None = None
    recommendations: list[dict] = field(default_factory=list)
    row_evidence_id: str | None = None


@dataclass
class Workspace:
    """Everything the agents of one turn share: the evidence index the
    supervisor issues, and the per-data-task slots. The evidence index is what
    `validate_outputs` checks citations against."""

    evidence: list[dict] = field(default_factory=list)
    evidence_ids: set[str] = field(default_factory=set)
    slots: dict[str, DataSlot] = field(default_factory=dict)
    next_index: int = 1

    def issue(self, items: list[dict]) -> None:
        for item in items:
            self.evidence.append(item)
            self.evidence_ids.add(item["id"])
        self.next_index += len(items)


@dataclass(frozen=True)
class AgentTask:
    """One unit of work for a specialist. `params` is supervisor-built and
    typed by the handler that reads it. `data_task` names the sql_data task
    whose result this task works on, so a claim is attributed to that task."""

    id: str
    agent: AgentName
    text: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return str(self.params.get("data_task", self.id))


@dataclass
class AgentContext:
    """One agent invocation's view of the turn. `spec` is the invoking agent's
    own spec, bound by the supervisor, so `call` only ever checks tools against
    the agent actually running. A handler cannot name another agent's spec."""

    spec: AgentSpec
    gateway: ToolGateway
    settings: Settings
    workspace: Workspace
    caller_roles: tuple[str, ...]

    def call(self, tool_name: str, payload: dict[str, Any]) -> ToolResult:
        return self.gateway.call(self.spec, tool_name, payload)


Handler = Callable[[AgentContext, AgentTask], AgentOutput]


def _failed(task: AgentTask, detail: str) -> AgentOutput:
    return AgentOutput(agent=task.agent, task_id=task.subject, status="failed", detail=detail)


def _skipped(task: AgentTask, detail: str, open_item: str | None = None) -> AgentOutput:
    return AgentOutput(
        agent=task.agent,
        task_id=task.subject,
        status="skipped",
        detail=detail,
        open_items=(open_item,) if open_item else (),
    )


def planner_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    result = ctx.call(
        TOOL_LLM_PLAN,
        {"question": task.text, "max_items": task.params["max_items"]},
    )
    if not result.success:
        return _failed(task, "planner tool failed")
    return AgentOutput(
        agent=task.agent,
        task_id=task.id,
        status="ok",
        data=dict(result.output),
        detail=f"planning mode {result.output['mode']}",
    )


def governance_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    """Two modes. "request" decides whether a question may be answered at all.
    "rows" redacts the rows a data task produced before any engine sees them.
    Both run locally and deterministically."""
    if task.params["mode"] == "request":
        verdict = evaluate_request(task.text, ctx.caller_roles)
        return AgentOutput(
            agent=task.agent,
            task_id=task.id,
            status="ok",
            data={
                "decision": str(verdict.decision.value),
                "message": verdict.message,
                "category": verdict.category,
            },
        )
    slot = ctx.workspace.slots[task.params["data_task"]]
    slot.governed_rows = govern_rows_for_caller(slot.columns, slot.rows, ctx.caller_roles)
    changed = slot.governed_rows != slot.rows
    return AgentOutput(
        agent=task.agent,
        task_id=task.subject,
        status="ok",
        data={"changed": changed},
        detail="rows redacted" if changed else "no redaction needed",
    )


def semantic_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    result = ctx.call(
        TOOL_BUSINESS_CONTEXT,
        {"question": task.text, "database_id": task.params["database_id"]},
    )
    if not result.success:
        return _failed(task, "semantic tool failed")
    claims = tuple(
        Claim(
            agent=task.agent,
            task_id=task.id,
            text=f"Governed metric: {metric.get('name', 'unnamed')}",
            truth_level=DataTruthLevel.CONFIRMED_BUSINESS_TRUTH,
            key=f"metric:{metric.get('name', '')}",
            value=metric.get("name"),
        )
        for metric in result.output["metrics"]
    )
    return AgentOutput(agent=task.agent, task_id=task.id, status="ok", claims=claims)


def sql_data_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    result = ctx.call(TOOL_GOVERNED_SQL, {"question": task.text})
    if not result.success:
        return _failed(task, "the query step failed")
    final = result.output
    outcome = build_outcome(final, task.id, ctx.workspace.next_index, task.text)
    ctx.workspace.issue(list(outcome["evidence"]))
    row_evidence = next((e["id"] for e in outcome["evidence"] if e["kind"] == "row_count"), None)
    ctx.workspace.slots[task.id] = DataSlot(
        columns=list(final.get("result_columns") or []),
        rows=list(final.get("result_rows") or []),
        analytical_intent=_intent_value(final.get("analytical_intent")),
        recommendations=list(final.get("recommendations") or []),
        row_evidence_id=row_evidence,
    )
    claims = tuple(
        Claim(
            agent=task.agent,
            task_id=task.id,
            text=item["claim"],
            truth_level=DataTruthLevel.DATABASE_FACT,
            key="row_count",
            value=item["row_count"],
            grounded_in=(item["id"],),
        )
        for item in outcome["evidence"]
        if item["kind"] == "row_count"
    )
    return AgentOutput(
        agent=task.agent,
        task_id=task.id,
        status="ok",
        claims=claims,
        open_items=tuple(outcome["open_items"]),
        data={"sub_status": outcome["status"]},
        detail=outcome["detail"],
    )


def analytics_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    slot = ctx.workspace.slots[task.params["data_task"]]
    if slot.governed_rows is None or not slot.columns:
        return _skipped(task, "no governed rows to analyse")
    result = ctx.call(
        TOOL_ANALYTICS,
        {"columns": slot.columns, "rows": slot.governed_rows},
    )
    if not result.success:
        return _failed(task, "analytics tool failed")
    slot.analytics = result.output
    subject = task.subject
    claims: list[Claim] = [
        Claim(
            agent=task.agent,
            task_id=subject,
            text=f"The result has {result.output['row_count']} row(s).",
            truth_level=DataTruthLevel.DATABASE_FACT,
            key="row_count",
            value=result.output["row_count"],
            grounded_in=(slot.row_evidence_id,) if slot.row_evidence_id else (),
        )
    ]
    for index, finding in enumerate(result.output.get("findings") or []):
        claim_obj = finding.get("claim") or {}
        claims.append(
            Claim(
                agent=task.agent,
                task_id=subject,
                text=str(claim_obj.get("value", "")),
                truth_level=DataTruthLevel(str(_enum_value(claim_obj.get("level")))),
                key=f"finding:{index}",
                grounded_in=(slot.row_evidence_id,) if slot.row_evidence_id else (),
            )
        )
    return AgentOutput(agent=task.agent, task_id=subject, status="ok", claims=tuple(claims))


def forecast_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    slot = ctx.workspace.slots[task.params["data_task"]]
    if slot.analytical_intent != "forecast" or slot.analytics is None:
        return _skipped(task, "forecast not requested for this result")
    points = _growth_points(slot.analytics)
    if len(points) < 2:
        return _skipped(
            task, "no time series to project", "Forecast skipped: no usable time series."
        )
    result = ctx.call(
        TOOL_FORECAST,
        {"points": points, "horizon": ctx.settings.forecast_default_horizon},
    )
    if not result.success:
        return _failed(task, "forecast tool failed")
    output = result.output
    subject = task.subject
    if output["status"] != "ok":
        reasons = "; ".join(output.get("rejection_reasons") or ["insufficient data"])
        return AgentOutput(
            agent=task.agent,
            task_id=subject,
            status="ok",
            open_items=(f"Forecast not produced: {reasons}.",),
        )
    claims = tuple(
        Claim(
            agent=task.agent,
            task_id=subject,
            text=f"Projected {point['period']}: {point['forecast']} (estimate, not observed)",
            truth_level=DataTruthLevel.AI_INFERENCE,
            key=f"forecast:{point['period']}",
            value=point["forecast"],
            grounded_in=(slot.row_evidence_id,) if slot.row_evidence_id else (),
        )
        for point in output["points"]
    )
    return AgentOutput(agent=task.agent, task_id=subject, status="ok", claims=claims)


def root_cause_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    current = ctx.workspace.slots[task.params["current_task"]]
    baseline = ctx.workspace.slots[task.params["baseline_task"]]
    if current.governed_rows is None or baseline.governed_rows is None:
        return _skipped(task, "a comparison input is missing")
    if current.columns != baseline.columns:
        return _skipped(
            task,
            "comparison result shapes differ",
            "Root cause not attempted: result shapes differ.",
        )
    result = ctx.call(
        TOOL_ROOT_CAUSE,
        {
            "current_rows": current.governed_rows,
            "baseline_rows": baseline.governed_rows,
            "columns": current.columns,
            "dimension_columns": task.params["dimension_columns"],
            "value_column": task.params["value_column"],
        },
    )
    if not result.success:
        return _failed(task, "root cause tool failed")
    output = result.output
    if not output["has_sufficient_evidence"]:
        return AgentOutput(
            agent=task.agent,
            task_id=task.id,
            status="ok",
            open_items=("No root cause stated: the evidence was not sufficient.",),
        )
    grounded = tuple(e for e in (current.row_evidence_id, baseline.row_evidence_id) if e)
    claims = tuple(
        Claim(
            agent=task.agent,
            task_id=task.id,
            text=(
                f"{contributor['label']} accounts for about "
                f"{contributor['contribution_percent']:.1f}% of the change (attribution estimate)"
            ),
            truth_level=DataTruthLevel.AI_INFERENCE,
            key=f"contributor:{contributor['label']}",
            value=contributor["contribution_percent"],
            grounded_in=grounded,
        )
        for contributor in output["contributors"]
    )
    return AgentOutput(agent=task.agent, task_id=task.id, status="ok", claims=claims)


def recommendation_handler(ctx: AgentContext, task: AgentTask) -> AgentOutput:
    """Reuses the analyst's own recommend node, unchanged, over this turn's
    evidence and candidates. Nothing is recomputed here."""
    candidates = [
        {"subquestion_id": subject, "recommendation": rec}
        for subject, slot in ctx.workspace.slots.items()
        for rec in slot.recommendations
    ]
    if not candidates:
        return AgentOutput(agent=task.agent, task_id=task.id, status="ok")
    state = cast(
        AnalystState,
        {
            "evidence": ctx.workspace.evidence,
            "recommendation_candidates": candidates,
            "trace": [],
        },
    )
    update = recommend_node(state)
    claims = tuple(
        Claim(
            agent=task.agent,
            task_id=rec["subquestion_id"],
            text=rec["claim"],
            truth_level=DataTruthLevel.AI_INFERENCE,
            key=f"recommendation:{rec['id']}",
            grounded_in=tuple(rec["evidence_ids"]),
        )
        for rec in update["recommendations"]
    )
    return AgentOutput(agent=task.agent, task_id=task.id, status="ok", claims=claims)


HANDLERS: dict[AgentName, Handler] = {
    AgentName.PLANNER: planner_handler,
    AgentName.GOVERNANCE: governance_handler,
    AgentName.SEMANTIC: semantic_handler,
    AgentName.SQL_DATA: sql_data_handler,
    AgentName.ANALYTICS: analytics_handler,
    AgentName.FORECAST: forecast_handler,
    AgentName.ROOT_CAUSE: root_cause_handler,
    AgentName.RECOMMENDATION: recommendation_handler,
}


def _intent_value(intent: Any) -> str | None:
    """The classified intent as a plain string, or None when there is none."""
    if intent is None:
        return None
    value = intent.get("intent") if isinstance(intent, dict) else intent
    return None if value is None else str(getattr(value, "value", value))


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _growth_points(analytics: dict[str, Any]) -> list[tuple[str, float]]:
    """The period/value series from the analytics result's growth finding.
    Empty when the result had no time series."""
    for finding in analytics.get("findings") or []:
        growth = finding.get("growth")
        if growth:
            return [(str(p["period"]), float(p["value"])) for p in growth.get("points") or []]
    return []
