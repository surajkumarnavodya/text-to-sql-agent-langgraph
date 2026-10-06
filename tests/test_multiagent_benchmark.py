"""Regression benchmark: the Prompt 33 analyst versus the Prompt 34 supervisor.

Asserts the orchestration properties that should hold by construction: both
flows run the same SQL and planner calls for the same question, both report the
same observed rows, and the supervisor adds only deterministic specialist work.
Timing is recorded by `eval.multiagent_benchmark` but never asserted here.
"""

from __future__ import annotations

import pytest

from eval.multiagent_benchmark import SCENARIOS, run_benchmark
from tests.test_data_analyst_agent import _SETTINGS


@pytest.fixture(scope="module")
def rows() -> dict[tuple[str, str], dict]:
    result = run_benchmark(_SETTINGS)
    return {(r["scenario"], r["flow"].split(" ")[0]): r for r in result}


def _pair(rows, scenario: str) -> tuple[dict, dict]:
    return rows[(scenario, "analyst")], rows[(scenario, "supervisor")]


def test_every_scenario_runs_through_both_flows(rows):
    assert len(rows) == 2 * len(SCENARIOS)


@pytest.mark.parametrize("scenario", [s.name for s in SCENARIOS])
def test_both_flows_make_the_same_sql_and_planner_calls(rows, scenario):
    analyst, supervisor = _pair(rows, scenario)

    assert analyst["sql_calls"] == supervisor["sql_calls"]
    assert analyst["llm_calls"] == supervisor["llm_calls"] == 1


@pytest.mark.parametrize("scenario", ["single_lookup", "two_step_question", "planner_unavailable"])
def test_both_flows_report_the_same_observed_data(rows, scenario):
    analyst, supervisor = _pair(rows, scenario)

    assert analyst["observed_claims"] == supervisor["observed_claims"] > 0


def test_a_failing_step_is_partial_in_both_flows(rows):
    analyst, supervisor = _pair(rows, "one_step_fails")

    assert analyst["status"] == supervisor["status"] == "partial"
    assert analyst["sql_calls"] == supervisor["sql_calls"] == 2


def test_the_analyst_has_no_specialist_overhead_but_the_supervisor_does(rows):
    for scenario in (s.name for s in SCENARIOS):
        analyst, supervisor = _pair(rows, scenario)
        assert analyst["specialist_calls"] == 0
        assert supervisor["specialist_calls"] > 0


def test_an_unreachable_planner_degrades_both_flows_to_one_question(rows):
    analyst, supervisor = _pair(rows, "planner_unavailable")

    assert analyst["sql_calls"] == supervisor["sql_calls"] == 1
    assert analyst["status"] == supervisor["status"] == "succeeded"
