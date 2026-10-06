"""The multi-agent supervisor (Prompt 34): one governed turn across specialists.

Routing is a fixed, deterministic policy. No model chooses which agent runs
next, and no agent talks to another directly. Agents return typed outputs to
the supervisor, which decides what runs next.

    governance(request) -> planner -> semantic
        -> for each sql_data task:  sql_data -> governance(rows) -> analytics -> forecast*
        -> root_cause* (explicit comparison pairs only)
        -> recommendation
        -> validate -> resolve conflicts -> report

    * runs only when its precondition holds (see each agent's handler).

Why a supervisor and not a free agent mesh: most specialists here are pure
functions over data that already exists. Only the planner calls a model. A
supervisor keeps routing inspectable and makes every budget and refusal an
explicit, testable value. The LangGraph analyst (`agent.analyst.graph`) is
unchanged and is the fallback this module is compared against in
`tests/test_multiagent_benchmark.py`.

Per turn, the supervisor enforces, in order, before any agent call:
- the wall-clock deadline (`analyst_timeout_seconds`),
- the turn's call and cost budget (`agent.multiagent.policy.TurnBudget`),
- the agent's circuit breaker (`agent.multiagent.policy.get_breaker`).

A refusal by any of these is recorded in the trace and reported as an open
item. It never becomes a silent gap.

Outputs are checked after the fact. Claims must cite evidence the supervisor
issued, must come from the agent that made them, and are capped at that agent's
truth level (`agent.multiagent.resolution`). Conflicting claims are resolved
by a fixed rule, not by agent order.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent.input_guard import check_input, rejection_message
from agent.multiagent.agents import (
    HANDLERS,
    SPECS,
    AgentContext,
    AgentTask,
    Handler,
    Workspace,
)
from agent.multiagent.contracts import AgentName, AgentOutput, Claim
from agent.multiagent.policy import (
    ToolAccessDenied,
    ToolGateway,
    TurnBudget,
    get_breaker,
)
from agent.multiagent.resolution import Conflict, resolve_conflicts, validate_outputs
from agent.multiagent.tools import build_supervisor_registry
from agent.provenance import DataTruthLevel
from agent.state import ConversationExchange
from agent.tools.types import ToolPermissionError
from config.settings import Settings, get_settings
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)

# Specialists whose failure degrades the answer without invalidating it: the
# planner falls back to the single question, and semantic definitions are
# enrichment. A failure here is reported as an open item, not a partial turn.
_ENRICHMENT_AGENTS = frozenset({AgentName.PLANNER, AgentName.SEMANTIC})

# Governance decisions that stop the turn before any data is touched.
_REFUSED_DECISIONS = frozenset({"refuse", "require_authorization"})


@dataclass(frozen=True)
class RootCausePair:
    """An explicit comparison the caller asks for. The planner never creates
    one: a model proposing a comparison is not authority to run it."""

    current_task: str
    baseline_task: str
    dimension_columns: tuple[str, ...]
    value_column: str


@dataclass
class SupervisedResult:
    status: str
    stop_reason: str | None
    subquestions: list[dict]
    claims: list[Claim]
    conflicts: list[Conflict]
    violations: list[str]
    open_items: list[str]
    trace: list[dict]
    evidence: list[dict]
    report_markdown: str
    usage: dict[str, Any] = field(default_factory=dict)


def _database_id(settings: Settings, tenant_id: str | None) -> str | None:
    """The database the semantic lookup reads definitions for: the first one
    this tenant may use. None when none is configured."""
    databases = settings.databases_for_tenant(tenant_id)
    return databases[0].name if databases else None


def run_supervised_analysis(
    question: str,
    *,
    caller_roles: tuple[str, ...],
    caller_subject: str | None = None,
    tenant_id: str | None = None,
    model: str | None = None,
    conversation_history: list[ConversationExchange] | None = None,
    root_cause_pairs: Sequence[RootCausePair] = (),
    handlers: dict[AgentName, Handler] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SupervisedResult:
    """Runs one supervised turn. The caller has authenticated and authorized
    the request. `handlers` overrides specialists for tests, such as a
    malicious or failing one; production passes nothing.
    """
    settings = get_settings()
    active_handlers = {**HANDLERS, **(handlers or {})}
    budget = TurnBudget(
        max_agent_calls=settings.multiagent_max_agent_calls,
        max_cost_units=settings.multiagent_max_cost_units,
    )
    started = clock()
    registry = build_supervisor_registry(
        settings=settings,
        model=model,
        tenant_id=tenant_id,
        conversation_history=list(conversation_history or []),
    )
    gateway = ToolGateway(registry, tuple(caller_roles), caller_subject)
    workspace = Workspace()
    outputs: list[AgentOutput] = []
    trace: list[dict] = []
    dispatched: set[str] = set()
    blocked: list[str] = []
    stop: str | None = None
    open_items: list[str] = []

    def dispatch(task: AgentTask) -> AgentOutput | None:
        """Runs one agent under every supervisor control. Returns None when the
        turn has already stopped. Otherwise returns the agent's output, or a
        recorded refusal."""
        nonlocal stop
        if stop is not None:
            return None
        dispatched.add(task.id)
        spec = SPECS[task.agent]

        if clock() - started >= settings.analyst_timeout_seconds:
            stop = "timeout"
            return _record(
                task,
                AgentOutput(
                    agent=task.agent, task_id=task.subject, status="skipped", detail="timeout"
                ),
            )
        refusal = budget.can_charge(spec.cost_units)
        if refusal:
            stop = refusal
            blocked.append(task.id)
            return _record(
                task,
                AgentOutput(
                    agent=task.agent, task_id=task.subject, status="skipped", detail=refusal
                ),
            )
        # Breakers are scoped to the tenant. A breaker shared across tenants would
        # let one tenant's bad data (which can make an engine raise) switch that
        # agent off for every other tenant: a cross-tenant denial of service.
        breaker = get_breaker(
            f"{tenant_id or 'anonymous'}:{task.agent.value}",
            failure_threshold=settings.multiagent_circuit_failure_threshold,
            cooldown_seconds=settings.multiagent_circuit_cooldown_seconds,
        )
        if not breaker.allow(clock()):
            blocked.append(task.id)
            return _record(
                task,
                AgentOutput(
                    agent=task.agent,
                    task_id=task.subject,
                    status="skipped",
                    detail="circuit open",
                    open_items=(
                        f"{task.agent.value} is temporarily unavailable; its step was skipped.",
                    ),
                ),
            )
        budget.charge(spec.cost_units)
        ctx = AgentContext(
            spec=spec,
            gateway=gateway,
            settings=settings,
            workspace=workspace,
            caller_roles=tuple(caller_roles),
        )
        try:
            output = active_handlers[task.agent](ctx, task)
        except (ToolAccessDenied, ToolPermissionError) as exc:
            # A policy refusal is not an agent fault, so the breaker is not touched.
            logger.warning("Supervisor refused a tool call from %s: %s", task.agent.value, exc)
            return _record(
                task,
                AgentOutput(
                    agent=task.agent,
                    task_id=task.subject,
                    status="denied",
                    detail="tool not permitted",
                ),
            )
        except Exception:
            # Full detail goes to the log only. The output carries a fixed message.
            logger.exception("Specialist %s raised", task.agent.value)
            output = AgentOutput(
                agent=task.agent, task_id=task.subject, status="failed", detail="internal error"
            )
        if output.status == "failed":
            breaker.record_failure(clock())
        else:
            breaker.record_success()
        return _record(task, output)

    def _record(task: AgentTask, output: AgentOutput) -> AgentOutput:
        outputs.append(output)
        trace.append(
            {
                "seq": len(trace) + 1,
                "agent": task.agent.value,
                "task_id": task.id,
                "status": output.status,
                "cost_units": SPECS[task.agent].cost_units if output.status != "skipped" else 0,
                "detail": output.detail,
            }
        )
        return output

    # 1. Request gate: no cost, and a refusal ends the turn before any data.
    guard = check_input(question, settings.max_question_length)
    if not guard.passed:
        return _finish(
            question=question,
            status="rejected",
            stop=None,
            subquestions=[],
            outputs=outputs,
            trace=trace,
            workspace=workspace,
            open_items=[rejection_message(guard.reason or "policy_refused", None)],
            budget=budget,
            dispatched=dispatched,
        )
    question = guard.cleaned_question
    gov = dispatch(
        AgentTask(id="G0", agent=AgentName.GOVERNANCE, text=question, params={"mode": "request"})
    )
    if gov is not None and gov.status == "ok" and gov.data.get("decision") in _REFUSED_DECISIONS:
        return _finish(
            question=question,
            status="refused",
            stop=None,
            subquestions=[],
            outputs=outputs,
            trace=trace,
            workspace=workspace,
            open_items=[gov.data.get("message") or "This question cannot be answered."],
            budget=budget,
            dispatched=dispatched,
        )

    # 2. Plan. A failed planner falls back to the single question.
    plan_out = dispatch(
        AgentTask(
            id="P0",
            agent=AgentName.PLANNER,
            text=question,
            params={"max_items": settings.analyst_max_subqueries},
        )
    )
    texts = [question]
    if plan_out is not None and plan_out.status == "ok":
        texts = list(plan_out.data.get("texts") or [question])
    subtasks: list[AgentTask] = []
    dropped = 0
    for text in texts:
        item = check_input(text, settings.max_question_length)
        if not item.passed:
            dropped += 1
            continue
        subtasks.append(
            AgentTask(
                id=f"Q{len(subtasks) + 1}", agent=AgentName.SQL_DATA, text=item.cleaned_question
            )
        )
    if dropped:
        open_items.append(f"{dropped} planned step(s) were dropped by the input checks.")
    if not subtasks:
        return _finish(
            question=question,
            status="rejected",
            stop=None,
            subquestions=[],
            outputs=outputs,
            trace=trace,
            workspace=workspace,
            open_items=open_items + ["No usable sub-question could be formed."],
            budget=budget,
            dispatched=dispatched,
        )

    # 3. Semantic definitions, once per turn, for the first database this tenant may use.
    database_id = _database_id(settings, tenant_id)
    if database_id is not None:
        dispatch(
            AgentTask(
                id="S0",
                agent=AgentName.SEMANTIC,
                text=question,
                params={"database_id": database_id},
            )
        )

    # 4. Each data task, then its governed analysis chain.
    for task in subtasks:
        sql_out = dispatch(task)
        if sql_out is None or sql_out.status != "ok" or task.id not in workspace.slots:
            continue
        dispatch(
            AgentTask(
                id=f"GR_{task.id}",
                agent=AgentName.GOVERNANCE,
                params={"mode": "rows", "data_task": task.id},
            )
        )
        dispatch(
            AgentTask(id=f"A_{task.id}", agent=AgentName.ANALYTICS, params={"data_task": task.id})
        )
        dispatch(
            AgentTask(id=f"F_{task.id}", agent=AgentName.FORECAST, params={"data_task": task.id})
        )

    # 5. Root cause, only for explicit comparison pairs the caller asked for.
    for index, pair in enumerate(root_cause_pairs, start=1):
        dispatch(
            AgentTask(
                id=f"R{index}",
                agent=AgentName.ROOT_CAUSE,
                params={
                    "current_task": pair.current_task,
                    "baseline_task": pair.baseline_task,
                    "dimension_columns": list(pair.dimension_columns),
                    "value_column": pair.value_column,
                },
            )
        )

    # 6. Recommendations, over whatever the data tasks produced.
    if workspace.slots:
        dispatch(AgentTask(id="REC", agent=AgentName.RECOMMENDATION))

    return _finish(
        question=question,
        status=None,
        stop=stop,
        subquestions=[{"id": t.id, "text": t.text} for t in subtasks],
        outputs=outputs,
        trace=trace,
        workspace=workspace,
        open_items=open_items,
        budget=budget,
        dispatched=dispatched,
        blocked=blocked,
        started=started,
        clock=clock,
    )


def _finish(
    *,
    question: str,
    status: str | None,
    stop: str | None,
    subquestions: list[dict],
    outputs: list[AgentOutput],
    trace: list[dict],
    workspace: Workspace,
    open_items: list[str],
    budget: TurnBudget,
    dispatched: set[str],
    blocked: list[str] | None = None,
    started: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SupervisedResult:
    """Validates outputs, resolves conflicts, decides the status and renders
    the report. Shared by every exit path, so a refused, rejected or stopped
    turn is reported with the same structure as a complete one."""
    accepted, violations = validate_outputs(
        outputs,
        dispatched,
        workspace.evidence_ids,
        SPECS,
    )
    claims, conflicts = resolve_conflicts(accepted)
    for violation in violations:
        log_security_event("agent_output_rejected", "warning", violation)
    open_items = list(open_items)
    for output in outputs:
        open_items.extend(output.open_items)
        if output.agent in _ENRICHMENT_AGENTS and output.status in {"failed", "denied"}:
            # Fail-open enrichment: the turn continued without it, and the report says so.
            open_items.append(
                f"{output.agent.value} was unavailable; the analysis continued without it."
            )
    open_items.extend(f"Rejected agent output: {v}" for v in violations)
    for conflict in conflicts:
        open_items.append(
            f"Conflicting values for {conflict.key} on step {conflict.task_id} were {conflict.outcome}; "
            f"agents: {', '.join(conflict.agents)}."
        )
    if stop:
        open_items.append(f"The turn stopped early: {stop.replace('_', ' ')}.")

    if status is None:
        status = _status_for(outputs, workspace, conflicts, violations, stop, blocked or [])

    usage = {
        "agent_calls": budget.calls,
        "cost_units": budget.cost,
        "elapsed_seconds": round((clock() - started), 3) if started is not None else 0,
    }
    report = _render_report(question, status, claims, open_items, trace, subquestions, usage)
    return SupervisedResult(
        status=status,
        stop_reason=stop,
        subquestions=subquestions,
        claims=claims,
        conflicts=conflicts,
        violations=violations,
        open_items=open_items,
        trace=trace,
        evidence=list(workspace.evidence),
        report_markdown=report,
        usage=usage,
    )


def _status_for(
    outputs: list[AgentOutput],
    workspace: Workspace,
    conflicts: list[Conflict],
    violations: list[str],
    stop: str | None,
    blocked: list[str],
) -> str:
    """Same shape as the analyst's status rules: a data-producing turn is
    "succeeded" only when every step finished cleanly. Anything else is
    "partial", and a turn with no usable data is "insufficient_data" or
    "failed"."""
    data_outputs = [o for o in outputs if o.agent == AgentName.SQL_DATA]
    sub_statuses = {o.data.get("sub_status") for o in data_outputs if o.status == "ok"}
    failed = any(
        o.status in {"failed", "denied"} and o.agent not in _ENRICHMENT_AGENTS for o in outputs
    )
    if not workspace.evidence:
        if "needs_clarification" in sub_statuses:
            return "needs_clarification"
        if data_outputs and all(o.status in {"failed", "denied"} for o in data_outputs):
            return "failed"
        return "partial" if stop or blocked else "insufficient_data"
    if stop or blocked or failed or conflicts or violations:
        return "partial"
    return "succeeded"


_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("Observed (database facts)", "database"),
    ("Governed metric definitions (confirmed business truth)", "metric"),
    ("Projections and attributions (AI estimates, not observed fact)", "estimate"),
    ("Recommendations (AI estimates)", "recommendation"),
)


def _section_of(claim: Claim) -> str:
    if claim.truth_level == DataTruthLevel.DATABASE_FACT:
        return "database"
    if claim.truth_level == DataTruthLevel.CONFIRMED_BUSINESS_TRUTH:
        return "metric"
    if claim.agent == AgentName.RECOMMENDATION:
        return "recommendation"
    return "estimate"


def _render_report(
    question: str,
    status: str,
    claims: list[Claim],
    open_items: list[str],
    trace: list[dict],
    subquestions: list[dict],
    usage: dict,
) -> str:
    """The report, from typed claims and fixed headings. Each section names its
    truth level, so an estimate is never presented as an observed value."""
    lines = [f"# Supervised analysis: {question}", "", f"**Status:** {status}", ""]
    lines.append(
        f"Agent calls: {usage['agent_calls']}, cost units: {usage['cost_units']}, "
        f"elapsed: {usage['elapsed_seconds']}s."
    )
    for heading, key in _SECTION_ORDER:
        section = [c for c in claims if _section_of(c) == key]
        lines += ["", f"## {heading}"]
        lines += [f"- [{c.task_id}] {c.text}" for c in section] or ["- None."]
    lines += ["", "## Open items"]
    lines += [f"- {item}" for item in open_items] or ["- None."]
    lines += ["", "## Agent trace"]
    lines += [
        f"- {t['seq']}. {t['agent']} ({t['task_id']}): {t['status']}"
        + (f" -- {t['detail']}" if t["detail"] else "")
        for t in trace
    ]
    return "\n".join(lines)
