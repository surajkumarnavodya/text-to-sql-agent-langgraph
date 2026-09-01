"""Benchmark dataset and evaluation-result data model.

Two families of types live here:

1. **Dataset schema** (`BenchmarkCase`, `FollowUpCase`, `FollowUpTurn`) --
   what a benchmark question looks like on disk (`eval/benchmark/*.yaml`),
   loaded by `eval.dataset_loader`.
2. **Result schema** (`CaseRunResult`, `BenchmarkReport`) -- what actually
   running a case against the live agent produces, and how a full run's
   results are aggregated. `eval.runner` produces `CaseRunResult`s;
   `eval.evaluators` fills in their verdict fields; `eval.metrics` reduces a
   list of them into a `BenchmarkReport`.

Design note on "expected result": the dataset schema deliberately does NOT
require every case to hand-specify literal expected rows. The primary
correctness signal (`eval.evaluators.evaluate_result_set`) executes
`expected_sql` against the live database once per case to derive a *gold
result set*, then compares the agent's actual result set against that --
real execution-accuracy evaluation (the same principle Spider/BIRD-style
Text-to-SQL benchmarks use), not a hand-maintained literal that can drift
out of sync with the real schema. `expected_result` exists only as an
optional, supplementary, hand-authored sanity check (e.g. "this must be
exactly zero rows") for cases where that's clearer or where no gold SQL
makes sense (most adversarial cases have no `expected_sql` at all -- they
should never reach execution).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Difficulty = Literal["easy", "medium", "hard", "real_world", "adversarial"]

SecurityClassification = Literal["benign", "adversarial"]

# What the agent is expected to do with this question -- distinct from
# whether its *answer* is correct (that's `expected_result`/`expected_sql`).
# "succeed" is the default for ordinary accuracy cases; the rest describe a
# terminal AgentState["status"] plus, where relevant, a specific reason.
ExpectedBehavior = Literal[
    "succeed",
    "needs_clarification",
    "reject_injection",
    "reject_off_topic",
    "reject_empty",
    "reject_too_long",
    "fail_high_cost",
    "fail_safely",
]

# The 20 subcategories named in the benchmark request, grouped by the
# difficulty tier they normally appear under (a category can appear under
# more than one tier -- e.g. a join can be easy or hard depending on depth).
# Kept as a plain set (not a Literal) so a new category can be added to a
# dataset file without a code change; `eval.dataset_loader` warns, not
# fails, on an unrecognized one (see its docstring).
KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        # Easy
        "simple_filtering",
        "sorting",
        "aggregation",
        # Medium
        "joins",
        "date_filtering",
        "multi_table_analysis",
        "group_by_having",
        # Hard
        "nested_queries",
        "ctes",
        "window_functions",
        "complex_joins",
        "conditional_aggregation",
        # Real-world
        "business_questions",
        "ambiguous_wording",
        "incomplete_questions",
        "follow_up",
        # Adversarial
        "prompt_injection",
        "malicious_sql",
        "schema_manipulation",
        "unauthorized_data",
        # Cross-cutting -- not one of the 20 explicitly-named subcategories,
        # but each is a real, distinct metric the benchmark request itself
        # asks for (#13 NULL handling) or a dimension this framework
        # preserves from the legacy eval_questions.yaml (cost estimation),
        # so a handful of cases are deliberately tagged with these to give
        # those metrics real data to compute over.
        "null_handling",
        "cost_estimation",
    }
)


@dataclass(frozen=True)
class ExpectedResultSpec:
    """Optional, hand-authored supplementary result check.

    Never the primary correctness signal (see module docstring) -- used
    only when a case has no `expected_sql` (most adversarial cases) or when
    a simple, explicit assertion is clearer than deriving one from gold SQL
    (e.g. "must be exactly zero rows").
    """

    row_count: int | None = None
    columns: tuple[str, ...] | None = None
    sample_rows: tuple[tuple, ...] | None = None


@dataclass(frozen=True)
class BenchmarkCase:
    """One standalone benchmark question.

    Every field the task's dataset-structure request named is represented
    here directly: `question`, `database`, `expected_tables`,
    `expected_columns`, `expected_sql`, `expected_result`, `difficulty`,
    `category`, `security_classification`, `alternative_sql`,
    `expected_behavior`, `notes`.
    """

    id: str
    question: str
    database: str
    difficulty: Difficulty
    category: str
    security_classification: SecurityClassification = "benign"
    expected_tables: tuple[str, ...] = ()
    expected_columns: tuple[str, ...] = ()
    expected_sql: str | None = None
    alternative_sql: tuple[str, ...] = ()
    expected_result: ExpectedResultSpec | None = None
    expected_behavior: ExpectedBehavior = "succeed"
    expect_rejection_reason: str | None = None
    expect_cost_severity: str | None = None
    order_matters: bool = False
    min_rows: int | None = None
    max_rows: int | None = None
    expect_readable_result: bool = False
    expect_grounded_insight: bool = False
    notes: str = ""


@dataclass(frozen=True)
class FollowUpTurn:
    """One turn of a `FollowUpCase` -- the same shape as `BenchmarkCase`
    minus the fields that only make sense once per conversation (id,
    database, difficulty, category), plus `expect_followup`."""

    question: str
    expected_tables: tuple[str, ...] = ()
    expected_columns: tuple[str, ...] = ()
    expected_sql: str | None = None
    alternative_sql: tuple[str, ...] = ()
    expected_result: ExpectedResultSpec | None = None
    expected_behavior: ExpectedBehavior = "succeed"
    expect_followup: bool = False
    order_matters: bool = False
    min_rows: int | None = None
    max_rows: int | None = None
    expect_readable_result: bool = False
    expect_grounded_insight: bool = False


@dataclass(frozen=True)
class FollowUpCase:
    """A real multi-turn conversation, run turn by turn through the same
    `build_conversation_history`/`run_agent` path the UI uses -- see
    `eval/runner.py`."""

    id: str
    database: str
    turns: tuple[FollowUpTurn, ...]
    difficulty: Difficulty = "real_world"
    category: str = "follow_up"
    notes: str = ""


@dataclass(frozen=True)
class BenchmarkDataset:
    """Everything loaded from `eval/benchmark/*.yaml` for one run."""

    standalone_cases: tuple[BenchmarkCase, ...] = ()
    followup_cases: tuple[FollowUpCase, ...] = ()


# ---------------------------------------------------------------------------
# Result schema -- what running a case actually produces
# ---------------------------------------------------------------------------


@dataclass
class CaseRunResult:
    """Raw signal captured from running ONE case (or one turn) through the
    live agent -- the single source every metric in `eval/metrics.py` is
    computed from, and what `eval/evaluators.py` fills verdict fields into.

    Deliberately a single flat record per attempt-unit (one standalone case,
    or one turn of a follow-up sequence) rather than one record per metric
    category -- every metric this framework reports is a reduction over a
    list of these, so keeping one raw source avoids the 26 metrics silently
    drifting apart from what was actually observed.
    """

    case_id: str
    question: str
    difficulty: str
    category: str
    security_classification: str
    turn_index: int | None = None  # None for standalone cases

    # Raw agent outcome
    final_status: str = "unknown"
    retry_count: int = 0
    attempt_history: list[dict] = field(default_factory=list)
    generated_sql: str | None = None
    rejection_reason: str | None = None
    followup_classification: str | None = None
    failure_explanation: str | None = None

    # Retrieval
    retrieved_tables: list[str] = field(default_factory=list)
    # Copy of the source case's `expected_tables` -- carried onto the result
    # (rather than requiring metrics.py to look the case back up) so
    # `eval.metrics.compute_metrics`'s `relevant_table_precision` can be
    # computed from `CaseRunResult`s alone.
    expected_tables_hint: tuple[str, ...] = ()

    # Actual execution result
    result_columns: list[str] | None = None
    result_rows: list[tuple] | None = None
    row_count: int | None = None

    # Timing / cost / complexity -- real numbers, never estimated after the
    # fact (see eval/runner.py for how each is captured)
    wall_time_seconds: float = 0.0
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    llm_call_count: int = 0
    cost_estimate_severity: str | None = None
    cost_estimate_rows: float | None = None
    complexity_score: int | None = None
    # Ollama's own reported token counts (see agent/llm_client.py's
    # `_log_ollama_timing`), captured by `eval.runner` via a temporary log
    # handler on the `agent.llm_client` logger -- "where available" per the
    # benchmark request, since this is the only channel that currently
    # exposes them (AgentState itself doesn't carry token counts). None if
    # the run's log capture found no usable timing line for this case.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    # Gold comparison (populated by eval.evaluators, using expected_sql
    # executed live -- see that module)
    gold_columns: list[str] | None = None
    gold_rows: list[tuple] | None = None
    gold_source: str | None = (
        None  # "expected_sql" | "alternative_sql[N]" | "expected_result" | None
    )

    # Verdicts -- None means "not applicable to this case", not "unknown"
    execution_correct: bool | None = None
    result_set_correct: bool | None = None
    sql_exact_match: bool | None = None
    retrieval_recall: float | None = None
    column_recall: float | None = None
    structure_checks: dict[str, bool] = field(default_factory=dict)
    security_correct: bool | None = None
    overall_pass: bool = False
    failure_category: str | None = None
    error_detail: str | None = None


@dataclass
class BenchmarkReport:
    """Aggregated output of a full benchmark run -- what `eval/reporting.py`
    renders and `eval/regression.py` compares against a stored baseline."""

    run_id: str
    timestamp: str
    model: str
    database: str
    total_cases: int
    metrics: dict[str, float]
    per_category: dict[str, dict[str, float]]
    per_difficulty: dict[str, dict[str, float]]
    results: list[CaseRunResult]

    @property
    def failures(self) -> list[CaseRunResult]:
        return [r for r in self.results if not r.overall_pass]
