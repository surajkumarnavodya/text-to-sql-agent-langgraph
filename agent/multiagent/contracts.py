"""Typed contracts for the multi-agent supervisor (Prompt 34).

Every specialist agent is described by an `AgentSpec` and returns an
`AgentOutput`. The spec is the agent's entire authority: which governed tools
it may call, how many cost units one call charges, and the highest truth level
it may ever assert. The supervisor enforces all three. An agent cannot widen
its own spec from inside its handler.

Plain frozen dataclasses rather than pydantic: these are internal values that
never cross an HTTP boundary directly. The API layer maps them to its own
response models (`api/multiagent_schemas.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.provenance import DataTruthLevel


class AgentName(str, Enum):
    """The closed set of specialists. A plan can only name one of these."""

    PLANNER = "planner"
    GOVERNANCE = "governance"
    SEMANTIC = "semantic"
    SQL_DATA = "sql_data"
    ANALYTICS = "analytics"
    FORECAST = "forecast"
    ROOT_CAUSE = "root_cause"
    RECOMMENDATION = "recommendation"


# Truth levels ordered from least to most trusted. A claim's level may be
# lowered to an agent's ceiling but never raised above it.
_TRUTH_RANK: dict[DataTruthLevel, int] = {
    DataTruthLevel.AI_INFERENCE: 0,
    DataTruthLevel.CONFIRMED_BUSINESS_TRUTH: 1,
    DataTruthLevel.DATABASE_FACT: 2,
}


def cap_truth_level(level: DataTruthLevel, ceiling: DataTruthLevel) -> DataTruthLevel:
    """Returns `level` lowered to `ceiling` if it exceeds it, otherwise unchanged.

    An agent cannot assert a stronger claim than its spec allows. An
    inference-only agent that claims `DATABASE_FACT` is downgraded to its
    ceiling, and the downgrade is recorded by the supervisor, not hidden.
    """
    if _TRUTH_RANK[level] <= _TRUTH_RANK[ceiling]:
        return level
    return ceiling


@dataclass(frozen=True)
class AgentSpec:
    """One specialist's fixed authority.

    Attributes:
        name: Which specialist this is.
        kind: "llm" (calls a model), "data" (may run governed SQL), "engine"
            (pure deterministic computation), or "control" (governance).
        allowed_tools: The only tool names this agent may invoke through the
            supervisor. Empty means it may invoke none.
        max_truth_level: The strongest truth level this agent may assert.
        cost_units: What one invocation charges against the turn's cost budget.
        max_attempts: Retries the tool layer may make for this agent's tool
            calls. The SQL tool stays at 1, because `run_agent` already runs
            its own bounded self-correction loop, and retrying the whole call
            would multiply cost.
    """

    name: AgentName
    kind: str
    allowed_tools: frozenset[str]
    max_truth_level: DataTruthLevel
    cost_units: int
    max_attempts: int = 1


@dataclass(frozen=True)
class Claim:
    """One assertion an agent makes, with its provenance.

    `grounded_in` lists the evidence ids this claim rests on. Every id must
    already exist in the supervisor's evidence index, or the claim is dropped
    as invented. `key` groups claims that answer the same question, so
    conflicts can be detected (for example two agents both stating the row
    count of one sub-question).
    """

    agent: AgentName
    task_id: str
    text: str
    truth_level: DataTruthLevel
    key: str
    value: Any = None
    grounded_in: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentOutput:
    """One agent invocation's result.

    `status` is "ok", "failed" (the handler or its tool raised or timed out),
    "skipped" (a circuit breaker or budget refused the call, or a precondition
    was not met), or "denied" (a tool or policy check refused the call). Only
    "ok" outputs contribute claims. `data` is supervisor-internal and never
    returned to the caller.
    """

    agent: AgentName
    task_id: str
    status: str
    claims: tuple[Claim, ...] = ()
    open_items: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    cost_units: int = 0
    detail: str = ""
