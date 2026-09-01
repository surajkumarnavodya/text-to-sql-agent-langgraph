"""Runs a `BenchmarkDataset` against the live agent and produces `CaseRunResult`s.

Requires a live Ollama server and a live, configured database -- exactly
like `scripts/integration_test.py`/the old `scripts/run_eval.py`. Never
imported by anything in `tests/` (which stays fully mocked); this module's
own logic is what `scripts/run_benchmark.py` calls.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import Engine

from agent.graph import run_agent
from agent.state import AgentState
from config.settings import Settings, get_settings
from db.connection import get_read_only_engine, get_sqlglot_dialect
from eval.evaluators import (
    classify_failure,
    compute_complexity_score,
    compute_overall_pass,
    evaluate_column_selection,
    evaluate_result_set,
    evaluate_retrieval,
    evaluate_security,
    evaluate_sql_exact_match,
    evaluate_sql_structure,
)
from eval.schema import BenchmarkCase, BenchmarkDataset, CaseRunResult, FollowUpTurn
from observability.llm_timing_capture import capture_llm_timings
from ui.session_history import QueryHistoryEntry, build_conversation_history, new_history_entry

logger = logging.getLogger(__name__)


def _llm_call_count(state: AgentState) -> int:
    """Total real LLM calls made for this run: one per attempt (generation)
    plus one more if an insight was attempted (see `generate_insight_node`
    -- it's skipped, not called, for a redundant single-value result, so
    this only counts it when the state shows an insight or a summary was
    actually produced)."""
    attempts = len(state.get("attempt_history") or [])
    insight_call = 1 if state.get("insight_summary") is not None else 0
    return attempts + insight_call


def _stage_timings_ms(state: AgentState) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in state.get("stage_timings") or []:
        totals[record["stage"]] = totals.get(record["stage"], 0.0) + record["duration_ms"]
    return totals


def _run_one_turn(
    question: str,
    conversation_history: list,
    enable_insight: bool,
) -> tuple[AgentState, float, int, int, bool]:
    """Runs one `run_agent()` call with token capture + wall-clock timing.

    Returns:
        (final_state, wall_time_seconds, prompt_tokens, completion_tokens).
        Token counts are 0 if no LLM call was made at all (e.g. the
        question was rejected before generation) -- distinct from "not
        available," which is represented as None at the `CaseRunResult`
        level by the caller when `saw_any` is False.
    """
    start = time.perf_counter()
    with capture_llm_timings() as capture:
        state = run_agent(question, conversation_history, enable_insight=enable_insight)
    wall_time = time.perf_counter() - start
    return (
        state,
        wall_time,
        capture.prompt_tokens_total,
        capture.completion_tokens_total,
        capture.saw_any_tokens,
    )


def _build_case_run_result(
    case_id: str,
    question: str,
    difficulty: str,
    category: str,
    security_classification: str,
    turn_index: int | None,
    expected_tables: tuple[str, ...],
    state: AgentState,
    wall_time: float,
    prompt_tokens: int,
    completion_tokens: int,
    saw_tokens: bool,
    dialect: str | None,
) -> CaseRunResult:
    sql = state.get("sql")
    cost_estimate = state.get("cost_estimate")
    run = CaseRunResult(
        case_id=case_id,
        question=question,
        difficulty=difficulty,
        category=category,
        security_classification=security_classification,
        turn_index=turn_index,
        final_status=state.get("status", "unknown"),
        retry_count=state.get("retry_count", 0),
        attempt_history=[dict(a) for a in state.get("attempt_history") or []],
        generated_sql=sql,
        rejection_reason=state.get("rejection_reason"),
        followup_classification=state.get("followup_classification"),
        failure_explanation=state.get("failure_explanation"),
        retrieved_tables=[t["table_name"] for t in state.get("schema_tables") or []],
        expected_tables_hint=expected_tables,
        result_columns=state.get("result_columns"),
        result_rows=state.get("result_rows"),
        row_count=state.get("row_count"),
        wall_time_seconds=wall_time,
        stage_timings_ms=_stage_timings_ms(state),
        llm_call_count=_llm_call_count(state),
        cost_estimate_severity=cost_estimate.severity if cost_estimate else None,
        cost_estimate_rows=cost_estimate.estimated_rows if cost_estimate else None,
        complexity_score=compute_complexity_score(sql, dialect),
        prompt_tokens=prompt_tokens if saw_tokens else None,
        completion_tokens=completion_tokens if saw_tokens else None,
    )
    return run


def _grade(
    run: CaseRunResult, case: BenchmarkCase | FollowUpTurn, engine: Engine, dialect: str | None
) -> None:
    """Fills every verdict field on `run` in place, using `case`'s
    expectations -- the single point where evaluators.py's functions are
    wired together for one case (shared by standalone and follow-up-turn
    grading, so a follow-up turn is graded by exactly the same logic as a
    standalone question -- see `agent.nodes`'s own "a follow-up gets no
    exemption" philosophy, mirrored here)."""
    if case.expected_behavior == "succeed":
        evaluate_result_set(run, case, engine)
        run.sql_exact_match = evaluate_sql_exact_match(run.generated_sql, case, dialect)
        run.retrieval_recall = evaluate_retrieval(run.retrieved_tables, case.expected_tables)
        run.column_recall = evaluate_column_selection(
            run.generated_sql, case.expected_columns, dialect
        )
        run.structure_checks = evaluate_sql_structure(
            run.generated_sql, case, run.category, dialect
        )
    else:
        run.security_correct = evaluate_security(run, case)

    run.overall_pass = compute_overall_pass(run, case)
    if not run.overall_pass:
        run.failure_category, run.error_detail = classify_failure(run, case)


def run_standalone_case(
    case: BenchmarkCase, engine: Engine, settings: Settings, dialect: str | None
) -> CaseRunResult:
    """Runs and grades one standalone `BenchmarkCase`."""
    state, wall_time, prompt_tok, completion_tok, saw_tok = _run_one_turn(
        case.question, [], enable_insight=case.expect_grounded_insight
    )
    run = _build_case_run_result(
        case.id,
        case.question,
        case.difficulty,
        case.category,
        case.security_classification,
        None,
        case.expected_tables,
        state,
        wall_time,
        prompt_tok,
        completion_tok,
        saw_tok,
        dialect,
    )
    _grade(run, case, engine, dialect)
    return run


def run_followup_case(
    case, engine: Engine, settings: Settings, dialect: str | None
) -> list[CaseRunResult]:
    """Runs and grades one multi-turn `FollowUpCase`, turn by turn, through
    the real `build_conversation_history`/`run_agent` path -- identical to
    how `ui/app.py` and the legacy `scripts/run_eval.py` exercise follow-ups."""
    history: list[QueryHistoryEntry] = []
    results: list[CaseRunResult] = []
    for turn_index, turn in enumerate(case.turns):
        conversation_history = build_conversation_history(history)
        state, wall_time, prompt_tok, completion_tok, saw_tok = _run_one_turn(
            turn.question, conversation_history, enable_insight=turn.expect_grounded_insight
        )
        run = _build_case_run_result(
            case.id,
            turn.question,
            case.difficulty,
            case.category,
            "benign",
            turn_index,
            turn.expected_tables,
            state,
            wall_time,
            prompt_tok,
            completion_tok,
            saw_tok,
            dialect,
        )
        _grade(run, turn, engine, dialect)
        results.append(run)
        history.append(new_history_entry(turn.question, state))
    return results


def run_benchmark(
    dataset: BenchmarkDataset,
    limit: int | None = None,
    categories: set[str] | None = None,
    difficulties: set[str] | None = None,
    progress_callback=None,
) -> list[CaseRunResult]:
    """Runs every applicable case in `dataset` against the live agent.

    Args:
        dataset: Loaded via `eval.dataset_loader.load_benchmark()`.
        limit: If set, stop after this many standalone cases (follow-up
            sequences are counted as one unit toward the limit, all their
            turns run together) -- for a deliberately sized subset run when
            the full dataset would take too long (each case is a real LLM
            round trip; see `scripts/run_benchmark.py --help`).
        categories: If set, only run cases whose `category` is in this set.
        difficulties: If set, only run cases whose `difficulty` is in this set.
        progress_callback: Optional `Callable[[int, int, CaseRunResult], None]`
            invoked after each case/turn (index, total, result) -- lets a
            CLI print progress without this function knowing about stdout.

    Returns:
        One `CaseRunResult` per standalone case and per follow-up turn, in
        run order.
    """
    settings = get_settings()
    engine = get_read_only_engine(settings)
    dialect = get_sqlglot_dialect(settings.db_type)

    standalone = [
        c
        for c in dataset.standalone_cases
        if (categories is None or c.category in categories)
        and (difficulties is None or c.difficulty in difficulties)
    ]
    followups = [
        c
        for c in dataset.followup_cases
        if (categories is None or c.category in categories)
        and (difficulties is None or c.difficulty in difficulties)
    ]
    if limit is not None:
        standalone = standalone[:limit]
        remaining = max(limit - len(standalone), 0)
        followups = followups[:remaining]

    total_units = len(standalone) + len(followups)
    results: list[CaseRunResult] = []
    unit_index = 0

    for case in standalone:
        unit_index += 1
        logger.info("[benchmark] (%d/%d) %s: %r", unit_index, total_units, case.id, case.question)
        run = run_standalone_case(case, engine, settings, dialect)
        results.append(run)
        if progress_callback:
            progress_callback(unit_index, total_units, run)

    for followup_case in followups:
        unit_index += 1
        logger.info(
            "[benchmark] (%d/%d) %s: %d-turn follow-up sequence",
            unit_index,
            total_units,
            followup_case.id,
            len(followup_case.turns),
        )
        turn_results = run_followup_case(followup_case, engine, settings, dialect)
        results.extend(turn_results)
        if progress_callback:
            for turn_result in turn_results:
                progress_callback(unit_index, total_units, turn_result)

    return results
