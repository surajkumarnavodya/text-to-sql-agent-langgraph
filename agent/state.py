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

from agent.insight import ResultSummary
from db.query_cost import CostEstimate

AgentStatus = Literal[
    "pending",
    "sanitizing_input",
    "classifying_followup",
    "retrieving_schema",
    "generating",
    "validating",
    "estimating_cost",
    "executing",
    "succeeded",
    "failed",
    "needs_clarification",
    "rejected",
    "rate_limited",
]

# Why a question never reached generation at all -- either the input gate
# (`agent.input_guard.check_input`) rejected it before any LLM call, or the
# model itself refused via `agent.llm_client.OFF_TOPIC_SENTINEL` (the
# defense-in-depth backstop for anything the gate's cheaper pre-filter
# missed). Both land on the same "rejected" status/reason space so the UI
# has one place to look, regardless of which layer caught it.
RejectionReason = Literal["too_long", "empty", "injection_detected", "off_topic"]


class TableSchema(TypedDict):
    """One retrieved table's DDL plus how relevant it was to the question."""

    table_name: str
    ddl: str
    similarity_score: float


class ConversationExchange(TypedDict):
    """One prior turn's resolved shape, for follow-up reference resolution.

    Deliberately holds *structure*, not result data: the question text, the
    SQL that was generated for it, and which tables that SQL ended up using
    -- never result rows. This is what `agent.followup.classify_followup`
    and `generate_sql_node`'s follow-up prompt block are allowed to see; it
    keeps the reference-resolution prompt bounded regardless of how large
    the actual result set was (see CLAUDE.md's constraint on this).

    Built by the caller (`ui/session_history.py`) from the same session
    query history that powers the UI's History panel -- one source of truth
    for "what was asked and what happened," not a parallel state.
    """

    question: str
    sql: str | None
    tables: list[str]
    status: str  # "succeeded" | "failed" | "needs_clarification"


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

    # Set by sanitize_input_node -- None/"passed" means the question is
    # clean and may proceed. On rejection, status becomes "rejected" and
    # rejection_message holds the standardized, non-technical text shown to
    # the user (see agent.input_guard._MESSAGES) -- never the raw reason or
    # which pattern matched, per CLAUDE.md's "don't confirm what was
    # detected" rule. `question` itself is overwritten with the normalized
    # (NFKC + confusables-folded + control-stripped) text on a pass, so
    # every downstream node operates on sanitized text.
    rejection_reason: RejectionReason | None
    rejection_message: str | None

    # Set by generate_sql_node when the process-wide LLM-call rate limiter
    # (agent.rate_limit) denies a generation attempt -- including a retry
    # attempt, not just the first one. Distinct from "rejected": this is a
    # temporary, systemic load condition, not a judgment about the question
    # itself, so it gets its own status/message rather than reusing
    # rejection_reason/rejection_message.
    rate_limit_message: str | None

    # Input, set once by the caller (see agent.graph.run_agent) from the
    # UI's session query history, capped to the last few exchanges -- never
    # mutated by a node during a single run, just read by
    # classify_followup_node / retrieve_schema_node / generate_sql_node.
    conversation_history: list[ConversationExchange]

    # Set by classify_followup_node
    followup_classification: Literal["standalone", "followup", "ambiguous"] | None
    # The specific prior exchange a "followup" classification was resolved
    # against (the most recent one in conversation_history) -- surfaced in
    # the UI as "Following up on: ..." so a wrong interpretation is visible
    # before the user runs anything.
    followup_resolved_against: ConversationExchange | None
    # Set only when status becomes "needs_clarification" -- a plain-language
    # explanation of why the agent couldn't tell what was being asked,
    # mirroring failure_explanation's role for the "failed" status.
    clarification_message: str | None

    # Set by retrieve_schema on its first pass through (see that node's
    # docstring) via embeddings.retriever.select_database -- which
    # configured database (Settings.databases[i].name) this question was
    # auto-routed to. Read, never re-selected, on the one retry path that
    # re-enters retrieve_schema (execute_sql's "missing_reference" retry):
    # a retry must keep targeting the same database, not silently jump to
    # a different one mid-attempt. validate_sql/estimate_query_cost/
    # execute_sql all resolve their dialect/engine from this via
    # db.connection.get_connection(settings, state["selected_database"]).
    selected_database: str | None
    # Set by retrieve_schema
    schema_tables: list[TableSchema]
    schema_context_text: str

    # Set by generate_sql
    sql: str | None

    # Set by validate_sql -- table names the generated SQL referenced that
    # were never part of the retrieved schema context for this attempt (see
    # agent.sql_validator.find_unexpected_table_references). Detection/
    # logging signal only, not a new gate -- see that function's docstring.
    # Empty list is the overwhelmingly common case.
    schema_anomaly_tables: list[str]

    # Set by validate_sql
    validation_error: str | None

    # Set by estimate_query_cost_node -- the non-executing EXPLAIN/SHOWPLAN
    # estimate for the validated SQL (see db.query_cost), or None if
    # estimation was disabled, unsupported for this DB_TYPE, or failed open
    # (timed out / errored -- see that module's docstring on why this must
    # never block a legitimate query). Logged at debug level regardless of
    # severity, so "what does normal look like" can be tuned from real data
    # rather than guessed.
    cost_estimate: CostEstimate | None
    # Set only when cost_estimate.severity == "moderate" -- a UI-facing
    # "this may take a moment" notice shown before/during execution. A
    # "high" severity estimate never reaches execute_sql at all (see
    # route_after_cost_estimate): it's routed back to generate_sql as a
    # retryable error instead, the same as any other correctable mistake.
    cost_notice: str | None

    # Set by execute_sql
    execution_error: str | None
    result_columns: list[str] | None
    result_rows: list[tuple] | None
    row_count: int | None

    # Set by execute_sql, only on a *successful* multi-table-join execution
    # that returned zero rows -- detection-only, never a new gate (same
    # philosophy as schema_anomaly_tables above): a legitimate zero-row
    # answer to a multi-table question is common, but this shape is also
    # the observable symptom of a join that matched unrelated surrogate-key
    # columns (see agent.sql_validator.references_multiple_tables and
    # agent.llm_client._system_prompt's join-correctness rules). None means
    # either the result had rows, only one table was involved, or execution
    # didn't succeed at all.
    low_confidence_notice: str | None

    # Input, set once by the caller -- whether generate_insight_node should
    # even attempt an insight. True (the default) is normally low-risk and
    # high-value; the UI exposes this as a toggle so it can be turned off
    # per-question without touching anything else in the pipeline.
    enable_insight: bool

    # Set by generate_insight_node, only after execute_sql_node succeeds.
    # None means "no insight" -- disabled via enable_insight, the result was
    # a redundant single-value shape (see agent.insight.should_skip_insight),
    # the LLM call itself failed, or the generated text failed the
    # groundedness check (see agent.insight.is_insight_grounded) and was
    # dropped rather than shown. Never influences sql/result_rows/row_count
    # above -- purely a narrative layer rendered alongside them.
    insight: str | None
    # The exact ResultSummary the insight (if any) was generated from --
    # kept on state so scripts/run_eval.py's grounding checks reuse the
    # same summary the node itself graded against, rather than recomputing
    # it and risking the two definitions of "grounded" drifting apart.
    insight_summary: ResultSummary | None

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
