"""Standalone entry point: stage-by-stage timing profile of the agent pipeline.

Requires a live DB connection + Ollama, like scripts/run_eval.py -- not part
of the pytest suite, never run by CI. Runs a fixed set of questions spanning
easy to hard, and prints a breakdown of where time actually goes: schema
retrieval, prompt assembly, the LLM call itself (further split into model
load / prompt processing / token generation via Ollama's own reported
timings -- see agent/llm_client.py), validation, DB execution, and an
approximation of UI-side result formatting.

This exists to answer "which stage is the bottleneck" with data instead of
assumption before spending effort optimizing any one layer.

Usage (from repo root, with the venv activated):

    python scripts\\profile_pipeline.py
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from agent.graph import run_agent  # noqa: E402
from agent.state import AgentState  # noqa: E402
from config.settings import configure_logging, get_settings  # noqa: E402
from db.connection import get_read_only_engine  # noqa: E402
from db.schema_introspection import introspect_schema  # noqa: E402
from ui.column_formatting import (
    format_column_label,
    get_display_columns,
    get_key_column_names,
)  # noqa: E402

# Spans easy -> hard, per the profiling request: a trivial single-table
# count, a 2-table join, a 3-table join, a 3+ table hierarchy join, and a
# question shaped to invite a window function.
QUESTIONS: list[tuple[str, str]] = [
    ("easy: count", "How many customers are there?"),
    ("easy: aggregate", "What is the total sales amount?"),
    ("2-table join", "Show total sales by product name"),
    ("3-table join", "Show total sales by customer country"),
    ("3+ table join", "Show total sales by product category"),
    ("hard: window fn", "For each product category, show the top 3 products by total sales"),
]

_STAGES = ["retrieve_schema", "generate_sql", "validate_sql", "execute_sql"]


def _time_result_formatting(columns: list[str], rows: list[tuple], key_columns: set[str]) -> float:
    """Approximates the UI-side (non-agent) cost: DataFrame + column formatting + a chart.

    This is the Python-only slice of what ui/app.py does after execute_sql
    returns -- it does NOT include actual browser rendering or Streamlit's
    own diffing/websocket cost, which can't be measured from a headless
    script. Included because the profiling request explicitly names
    "result formatting/rendering" as a stage to account for.
    """
    start = time.perf_counter()
    df = pd.DataFrame(rows, columns=columns)
    display_columns, _ = get_display_columns(list(df.columns), key_columns)
    _ = df[display_columns].rename(columns=format_column_label)
    return (time.perf_counter() - start) * 1000


def main() -> None:
    configure_logging()
    settings = get_settings()
    engine = get_read_only_engine(settings)
    discovered_tables = introspect_schema(engine, schema=settings.db_schema)
    key_columns = get_key_column_names(discovered_tables)

    per_question_rows: list[dict] = []
    all_stage_durations: dict[str, list[float]] = {stage: [] for stage in _STAGES}
    all_stage_durations["prompt_wait_llm_orchestration"] = []  # see note below
    all_stage_durations["result_formatting"] = []

    for label, question in QUESTIONS:
        wall_start = time.perf_counter()
        state: AgentState = run_agent(question)
        wall_ms = (time.perf_counter() - wall_start) * 1000

        stage_totals = dict.fromkeys(_STAGES, 0.0)
        for record in state.get("stage_timings", []):
            stage_totals[record["stage"]] = (
                stage_totals.get(record["stage"], 0.0) + record["duration_ms"]
            )
        # Whatever wall time isn't accounted for by the four node stages is
        # LangGraph's own orchestration overhead between nodes (routing,
        # state merging) -- typically tiny, but measured rather than assumed.
        accounted = sum(stage_totals.values())
        overhead_ms = max(wall_ms - accounted, 0.0)

        formatting_ms = 0.0
        if state.get("status") == "succeeded" and state.get("result_rows") is not None:
            formatting_ms = _time_result_formatting(
                state.get("result_columns") or [], state.get("result_rows") or [], key_columns
            )

        row = {
            "label": label,
            "question": question,
            "status": state.get("status"),
            "retries": state.get("retry_count", 0),
            "wall_ms": round(wall_ms, 1),
            **{stage: round(stage_totals[stage], 1) for stage in _STAGES},
            "orchestration_overhead_ms": round(overhead_ms, 1),
            "result_formatting_ms": round(formatting_ms, 1),
        }
        per_question_rows.append(row)

        for stage in _STAGES:
            all_stage_durations[stage].append(stage_totals[stage])
        all_stage_durations["prompt_wait_llm_orchestration"].append(overhead_ms)
        all_stage_durations["result_formatting"].append(formatting_ms)

        print(f"[{label}] {question!r}")
        print(f"  status={row['status']} retries={row['retries']} wall_ms={row['wall_ms']}")
        print(
            "  "
            + " ".join(f"{stage}={row[stage]}ms" for stage in _STAGES)
            + f" orchestration={row['orchestration_overhead_ms']}ms"
            + f" formatting={row['result_formatting_ms']}ms"
        )
        print()

    print("=" * 100)
    print("SUMMARY (across all questions, ms)")
    print("=" * 100)
    header = f"{'stage':<32}{'mean':>10}{'min':>10}{'max':>10}{'stdev':>10}"
    print(header)
    for stage, durations in all_stage_durations.items():
        mean = statistics.mean(durations) if durations else 0.0
        stdev = statistics.stdev(durations) if len(durations) > 1 else 0.0
        print(
            f"{stage:<32}{mean:>10.1f}{min(durations, default=0.0):>10.1f}"
            f"{max(durations, default=0.0):>10.1f}{stdev:>10.1f}"
        )

    total_wall = [row["wall_ms"] for row in per_question_rows]
    print()
    print(f"Average total wall time per question: {statistics.mean(total_wall):.1f} ms")

    df_out = pd.DataFrame(per_question_rows)
    print()
    print("Per-question breakdown:")
    print(df_out.to_string(index=False))


if __name__ == "__main__":
    main()
