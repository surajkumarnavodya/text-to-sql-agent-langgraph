"""Benchmark: the Prompt 33 LangGraph analyst versus the Prompt 34 supervisor.

Runs both flows over the same scripted scenarios with the same fake LLM and
the same fake governed SQL, so every difference is in orchestration, not model
quality. Counted per scenario:

- `sql_calls`: governed SQL executions (the expensive part)
- `llm_calls`: planner LLM calls
- `specialist_calls`: supervisor specialist invocations that did not skip
  (the analyst has no equivalent, so this is 0 for it)
- `observed_claims`: DATABASE_FACT row-count claims, to check both flows
  report the same observed data
- `elapsed_ms`: wall time, reported only. Not asserted, since it is noisy.

This is deterministic orchestration accounting. It does NOT measure answer
quality on a real model or database. That needs the live evaluation harness
(see 33_AI_DATA_ANALYST_AGENT_CONTRACT.md, next-prompt section).

Run with `python -m eval.multiagent_benchmark` (needs a valid `.env`, for
Settings only; nothing is sent to a model or database).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from agent.analyst import graph as analyst_graph
from agent.analyst.budget import AnalystBudget
from agent.multiagent import supervisor as supervisor_module
from agent.multiagent import tools as tools_module
from agent.multiagent.policy import reset_breakers
from config.settings import Settings

_REVENUE_COLUMNS = ["region", "total"]
_REVENUE_ROWS = [("North", 10), ("South", 20), ("East", 5)]
_MONTHLY_COLUMNS = ["month", "revenue"]
_MONTHLY_ROWS = [
    ("2023-01", 10),
    ("2023-02", 12),
    ("2023-03", 14),
    ("2023-04", 16),
    ("2023-05", 18),
    ("2023-06", 20),
]


def _final(columns: list[str], rows: list[tuple], intent: dict | None = None) -> dict:
    return {
        "status": "succeeded",
        "sql": "SELECT 1",
        "row_count": len(rows),
        "result_columns": columns,
        "result_rows": rows,
        "analytical_result": None,
        "analytical_intent": intent,
        "recommendations": [],
    }


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str
    planner_items: list[str] | None  # None -> the planner call fails
    sql_results: dict[str, Any] = field(
        default_factory=dict
    )  # question -> final state or Exception
    default_sql: dict | None = None


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="single_lookup",
        question="Revenue by region?",
        planner_items=["Revenue by region?"],
        default_sql=_final(_REVENUE_COLUMNS, _REVENUE_ROWS),
    ),
    Scenario(
        name="two_step_question",
        question="How is revenue trending by region?",
        planner_items=["Revenue by region?", "Revenue by month?"],
        default_sql=_final(_REVENUE_COLUMNS, _REVENUE_ROWS),
    ),
    Scenario(
        name="forecast_intent",
        question="Forecast monthly revenue",
        planner_items=["Monthly revenue?"],
        default_sql=_final(_MONTHLY_COLUMNS, _MONTHLY_ROWS, intent={"intent": "forecast"}),
    ),
    Scenario(
        name="planner_unavailable",
        question="Total orders last month?",
        planner_items=None,
        default_sql=_final(_REVENUE_COLUMNS, _REVENUE_ROWS),
    ),
    Scenario(
        name="one_step_fails",
        question="Revenue by region and month?",
        planner_items=["Revenue by region?", "Revenue by month?"],
        sql_results={"Revenue by month?": RuntimeError("upstream query timeout")},
        default_sql=_final(_REVENUE_COLUMNS, _REVENUE_ROWS),
    ),
)


class _FakeSql:
    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        self.calls = 0

    def __call__(self, question: str, **_kwargs: Any) -> dict:
        self.calls += 1
        outcome = self._scenario.sql_results.get(question, self._scenario.default_sql)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeLlm:
    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        self.calls = 0

    def __call__(self, **_kwargs: Any) -> str:
        self.calls += 1
        if self._scenario.planner_items is None:
            raise RuntimeError("planner backend unreachable")
        return json.dumps({"subquestions": self._scenario.planner_items})


def _analyst_flow(scenario: Scenario, settings: Settings) -> dict:
    sql = _FakeSql(scenario)
    llm = _FakeLlm(scenario)
    budget = AnalystBudget(
        max_steps=settings.analyst_max_steps,
        max_subqueries=settings.analyst_max_subqueries,
        max_llm_calls=settings.analyst_max_llm_calls,
        max_followups=settings.analyst_max_followups,
        timeout_seconds=settings.analyst_timeout_seconds,
    )
    with (
        mock.patch.object(analyst_graph, "get_settings", lambda: settings),
        mock.patch.object(analyst_graph, "run_agent", sql),
        mock.patch("agent.analyst.planner.call_ollama", llm),
    ):
        started = time.perf_counter()
        state = analyst_graph.run_analysis(
            scenario.question, budget=budget, caller_roles=("analyst",), tenant_id="tenant-a"
        )
        elapsed = (time.perf_counter() - started) * 1000
    observed = sum(1 for e in state.get("evidence", []) if e["kind"] == "row_count")
    return {
        "flow": "analyst (LangGraph)",
        "status": state.get("status"),
        "sql_calls": sql.calls,
        "llm_calls": llm.calls,
        "specialist_calls": 0,
        "observed_claims": observed,
        "elapsed_ms": round(elapsed, 2),
    }


def _supervisor_flow(scenario: Scenario, settings: Settings) -> dict:
    sql = _FakeSql(scenario)
    llm = _FakeLlm(scenario)
    reset_breakers()
    with (
        mock.patch.object(supervisor_module, "get_settings", lambda: settings),
        mock.patch.object(analyst_graph, "get_settings", lambda: settings),
        mock.patch.object(supervisor_module, "_database_id", lambda *_a: "default"),
        mock.patch.object(tools_module, "run_agent", sql),
        mock.patch.object(
            tools_module,
            "retrieve_business_context",
            lambda *a, **k: type("R", (), {"items": []})(),
        ),
        mock.patch("agent.analyst.planner.call_ollama", llm),
    ):
        started = time.perf_counter()
        result = supervisor_module.run_supervised_analysis(
            scenario.question, caller_roles=("analyst",), tenant_id="tenant-a"
        )
        elapsed = (time.perf_counter() - started) * 1000
    reset_breakers()
    specialist = sum(
        1
        for t in result.trace
        if t["agent"] not in {"planner", "sql_data"} and t["status"] != "skipped"
    )
    observed = sum(1 for c in result.claims if c.key == "row_count" and c.agent.value == "sql_data")
    return {
        "flow": "supervisor (multi-agent)",
        "status": result.status,
        "sql_calls": sql.calls,
        "llm_calls": llm.calls,
        "specialist_calls": specialist,
        "observed_claims": observed,
        "elapsed_ms": round(elapsed, 2),
    }


def run_benchmark(settings: Settings) -> list[dict]:
    """Runs every scenario through both flows. One row per scenario per flow."""
    rows: list[dict] = []
    for scenario in SCENARIOS:
        for runner in (_analyst_flow, _supervisor_flow):
            row = runner(scenario, settings)
            row["scenario"] = scenario.name
            rows.append(row)
    return rows


def format_table(rows: list[dict]) -> str:
    header = f"{'scenario':<22}{'flow':<26}{'status':<20}{'sql':>4}{'llm':>5}{'spec':>6}{'obs':>5}{'ms':>10}"
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row['scenario']:<22}{row['flow']:<26}{str(row['status']):<20}"
            f"{row['sql_calls']:>4}{row['llm_calls']:>5}{row['specialist_calls']:>6}"
            f"{row['observed_claims']:>5}{row['elapsed_ms']:>10.2f}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    from config.settings import get_settings

    print(format_table(run_benchmark(get_settings())))
