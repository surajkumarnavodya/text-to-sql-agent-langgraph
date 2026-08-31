"""Shared state for the LangGraph Text-to-SQL agent.

Every node function takes an `AgentState` and returns a partial dict of
updates; LangGraph merges those updates into the running state between node
executions. `error_history` uses an `operator.add` reducer so that each
node's contribution is *appended* to the running list rather than
overwriting it -- this is what lets `generate_sql` see the full trail of
prior failures on a retry.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

AgentStatus = Literal[
    "pending",
    "retrieving_schema",
    "generating",
    "validating",
    "executing",
    "succeeded",
    "failed",
]


class TableSchema(TypedDict):
    """One retrieved table's DDL plus how relevant it was to the question."""

    table_name: str
    ddl: str
    similarity_score: float


class StageTiming(TypedDict):
    """One node call's wall-clock duration, for performance profiling.

    One entry per node *call* (not per attempt) -- a question that retries
    twice produces two `generate_sql`/`validate_sql`/`execute_sql` entries,
    which is the point: it lets a profiling script sum "total time spent in
    stage X" across the whole run, including retries, rather than only
    seeing the last attempt's cost.
    """

    stage: str  # "retrieve_schema" | "generate_sql" | "validate_sql" | "execute_sql"
    attempt: int
    duration_ms: float


class AttemptRecord(TypedDict):
    """One full generate -> validate -> execute cycle's outcome.

    Exactly one entry is appended per attempt, at the point that attempt's
    fate becomes known (a validate/execute failure, or a final execute
    success) -- not per node call -- so the list reads as a plain timeline:
    "Attempt 1: missing_reference (retrying). Attempt 2: succeeded."

    Attributes:
        attempt: 1-indexed attempt number.
        sql: The SQL text this attempt validated/executed (None if the
            attempt never got that far, e.g. the LLM call itself failed).
        outcome: One of "succeeded", "safety_violation", "parse_error",
            "syntax_error", "missing_reference", "timeout",
            "unknown_error", "schema_retrieval_error", "llm_error".
        error: The raw error message (validator or database), None on success.
        will_retry: Whether the graph will attempt another generation cycle.
    """

    attempt: int
    sql: str | None
    outcome: str
    error: str | None
    will_retry: bool


class AgentState(TypedDict, total=False):
    """Full state threaded through the LangGraph graph.

    All fields are optional (`total=False`) because each node only sets the
    fields it's responsible for; the graph accumulates state as it runs.
    """

    # Input
    question: str

    # Set by retrieve_schema
    schema_tables: list[TableSchema]
    schema_context_text: str

    # Set by generate_sql
    sql: str | None

    # Set by validate_sql
    validation_error: str | None

    # Set by execute_sql
    execution_error: str | None
    result_columns: list[str] | None
    result_rows: list[tuple] | None
    row_count: int | None

    # Retry bookkeeping, shared across validate_sql / execute_sql
    error_history: Annotated[list[str], operator.add]
    retry_count: int

    # Full per-attempt timeline (see AttemptRecord) -- one entry per
    # concluded attempt, for the UI's "Retry timeline" expander and for
    # inspecting/testing the retry loop's behavior directly.
    attempt_history: Annotated[list[AttemptRecord], operator.add]

    # Category of the most recent failure ("safety_violation" | "parse_error"
    # | "syntax_error" | "missing_reference" | "timeout" | "unknown_error" |
    # None). Overwritten each attempt (not accumulated) -- generate_sql_node
    # reads it to give the next LLM call a more targeted retry hint than a
    # generic "fix it."
    last_error_category: str | None

    # Set once, at the point status becomes "failed" -- a plain-language,
    # UI-ready summary of why the agent gave up (distinct from the raw
    # driver/validator error text in error_history).
    failure_explanation: str | None

    # Overall progress, useful for the UI to show a status indicator
    status: AgentStatus

    # Per-node-call wall-clock timings (see StageTiming) -- one entry per
    # node call, accumulated across retries. Powers performance profiling
    # (scripts/profile_pipeline.py) and, potentially, a live "which stage is
    # running now" progress indicator in the UI.
    stage_timings: Annotated[list[StageTiming], operator.add]
