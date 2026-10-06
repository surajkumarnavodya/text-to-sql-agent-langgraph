"""Validation and conflict resolution for specialist outputs (Prompt 34).

Two deterministic gates run over every agent output before anything reaches
the report:

1. `validate_outputs` drops claims that name an undispatched task, claim to
   come from a different agent, or cite evidence the supervisor never issued
   (invented evidence). It also caps each claim's truth level at its agent's
   ceiling.
2. `resolve_conflicts` handles claims that answer the same question with
   different values. The rule is fixed and never depends on which agent ran
   last:
   - Two or more DATABASE_FACT claims disagree: withhold all of them and
     record the conflict. Two observed values cannot both be right, so neither
     is shown.
   - A DATABASE_FACT claim and an AI_INFERENCE claim disagree: keep the fact,
     drop the inference, and record that it was superseded.
   - Inference claims disagree with each other and no fact decides it: withhold
     all of them and record the conflict.
   - Agreeing claims are kept as they are.

Nothing here averages, picks a winner by agent order, or upgrades a level.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from agent.multiagent.contracts import AgentName, AgentOutput, AgentSpec, Claim, cap_truth_level
from agent.provenance import DataTruthLevel


@dataclass(frozen=True)
class Conflict:
    """One unresolved or superseded disagreement, for the trace and report.

    Records the agents and the question key only, never the values, so a
    conflict description cannot itself leak a restricted result.
    """

    task_id: str
    key: str
    agents: tuple[str, ...]
    outcome: str  # "withheld" | "superseded"


def validate_outputs(
    outputs: list[AgentOutput],
    dispatched_task_ids: set[str],
    evidence_index: set[str],
    specs: dict[AgentName, AgentSpec],
) -> tuple[list[Claim], list[str]]:
    """Returns the claims that passed every check, and a violation message for
    each claim that did not.

    Violation messages name the agent and the rule broken, never the claim's
    text, so a malicious claim cannot put its own content into the logs or
    the report.
    """
    accepted: list[Claim] = []
    violations: list[str] = []
    for output in outputs:
        if output.status != "ok":
            continue
        spec = specs[output.agent]
        for claim in output.claims:
            if claim.agent != output.agent:
                violations.append(
                    f"{output.agent.value}: claim attributed to another agent was dropped"
                )
                continue
            if claim.task_id not in dispatched_task_ids:
                violations.append(
                    f"{output.agent.value}: claim for an undispatched task was dropped"
                )
                continue
            unknown = [ev for ev in claim.grounded_in if ev not in evidence_index]
            if unknown:
                violations.append(
                    f"{output.agent.value}: claim cited evidence the supervisor did not issue; dropped"
                )
                continue
            capped = cap_truth_level(claim.truth_level, spec.max_truth_level)
            if capped is not claim.truth_level:
                violations.append(
                    f"{output.agent.value}: claim truth level lowered to {capped.value} (agent ceiling)"
                )
                claim = replace(claim, truth_level=capped)
            accepted.append(claim)
    return accepted, violations


def _normalize(value: Any) -> Any:
    """A comparable form of a claim's value. Floats are rounded so two agents
    computing the same number through different arithmetic still agree."""
    if isinstance(value, float):
        return round(value, 9)
    return value


def resolve_conflicts(claims: list[Claim]) -> tuple[list[Claim], list[Conflict]]:
    """Applies the fixed resolution rule to claims that share a question.

    Only claims with a non-empty `key` and a non-None `value` are compared.
    Claims grouped by (task_id, key) answer the same question.
    """
    groups: dict[tuple[str, str], list[Claim]] = defaultdict(list)
    passthrough: list[Claim] = []
    for claim in claims:
        if claim.key and claim.value is not None:
            groups[(claim.task_id, claim.key)].append(claim)
        else:
            passthrough.append(claim)

    accepted: list[Claim] = list(passthrough)
    conflicts: list[Conflict] = []
    for (task_id, key), group in groups.items():
        values = {_normalize(c.value) for c in group}
        if len(values) <= 1:
            accepted.extend(group)
            continue
        facts = [c for c in group if c.truth_level == DataTruthLevel.DATABASE_FACT]
        fact_values = {_normalize(c.value) for c in facts}
        agents = tuple(sorted({c.agent.value for c in group}))
        if len(fact_values) > 1:
            conflicts.append(Conflict(task_id=task_id, key=key, agents=agents, outcome="withheld"))
            continue
        if facts:
            accepted.extend(facts)
            conflicts.append(
                Conflict(task_id=task_id, key=key, agents=agents, outcome="superseded")
            )
            continue
        conflicts.append(Conflict(task_id=task_id, key=key, agents=agents, outcome="withheld"))
    return accepted, conflicts
