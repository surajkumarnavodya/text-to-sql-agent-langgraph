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
    "reviewing",
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


class GoldenExample(TypedDict):
    """One retrieved human-approved (question, SQL) pair from the golden
    dataset (see `embeddings.golden_examples`) -- same shape/spirit as
    `TableSchema` above (a retrieved, scored search result), not the raw
    storage record."""

    question: str
    sql: str
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
        outcome: One of "succeeded", "safety_violation", "empty",
            "parse_error", "nested_aggregate", "restricted_column",
            "high_cost", "syntax_error", "missing_reference",
            "aggregate_nesting", "plan_not_satisfied", "timeout",
            "unknown_error", "schema_retrieval_error", "llm_error",
            "off_topic", "rate_limited".
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

    # Input, set once by the caller (see agent.graph.run_agent /
    # agent.orchestrator.graph.run_orchestrated) from the authenticated
    # caller's `security.oidc.AuthIdentity.roles` (api/auth.py) -- empty
    # tuple for an unauthenticated/"none"-auth-mode caller or a caller
    # script that doesn't pass one (e.g. eval/runner.py), which resolves to
    # the same "no elevated permissions" outcome `agent.authz`'s default-
    # deny role map already gives an empty role set. Read by
    # `validate_sql_node` (restricted-column gate) and, on the
    # orchestrator's `OrchestratorState` superset, by
    # `agent.orchestrator.nodes`'s per-source authorization check (2026
    # Phase 2 security review) -- never mutated by a node, just read.
    caller_roles: tuple[str, ...]

    # Input, set once by the caller alongside caller_roles -- the
    # authenticated caller's `security.oidc.AuthIdentity.subject`. `None`
    # for the "none"/"static_token" auth modes (no real per-user identity
    # exists to key anything on -- see api/auth.py) or for a caller that
    # never passed one. 2026 Phase 2 security review: used by
    # `agent.orchestrator.nodes.router_node` to scope the session-level
    # expensive-source cost ceiling to a real, authenticated identity when
    # one exists, instead of only the client-supplied, unauthenticated
    # `OrchestratorState["session_id"]` (trivially resettable by minting a
    # fresh one -- see that field's own docstring).
    caller_subject: str | None

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

    # Input, set once by the caller (`agent.graph.run_agent`) via
    # `agent.model_registry.validate_model_selection` -- the Ollama model
    # name to use for every LLM call this run makes (generation, planning,
    # review, insight). Always a validated, allowed model name by the time
    # it lands here (an invalid caller-supplied model never reaches the
    # graph at all -- see `api/main.py`'s `/ask` handler, which validates
    # before ever calling `run_agent`), never a raw, unchecked string.
    # Deliberately request-scoped, exactly like `selected_database` above --
    # never a process-global -- so one caller's model choice can never leak
    # into a concurrent request for a different one. `agent.nodes` reads
    # this (never re-selects it) and passes it straight through to
    # `agent.llm_client`'s `model=` parameter on every call; `None` there
    # means "use `Settings.ollama_model`," the pre-existing default
    # behavior, so a caller that never sets this field (every caller before
    # this feature existed, and any current caller that omits
    # `AskRequest.model`) sees zero change. Deliberately never changes
    # per-attempt: a retry keeps using the same model the first attempt did,
    # for the same reason a retry keeps targeting the same
    # `selected_database` -- switching models mid-question would make the
    # error-feedback retry loop's "learn from the last mistake" premise
    # incoherent. Every LLM call for one question -- generation, the
    # up-front plan, the plan-conformance review, and the post-execution
    # insight -- intentionally uses this same one model, never a mix; see
    # `agent.llm_client.generate_insight_from_llm`'s own docstring for why.
    selected_model: str | None

    # Input, set once by the caller (`agent.graph.run_agent` /
    # `agent.orchestrator.graph.run_orchestrated`) via
    # `security.tenancy.resolve_tenant_id_for_identity` -- which tenant the
    # authenticated caller belongs to. `None` for an anonymous/no-identity
    # caller (mirrors `caller_subject` above). Always
    # `security.tenancy.DEFAULT_TENANT_ID` today, since this codebase is
    # explicitly single-tenant by design (see `security/tenancy.py`'s own
    # module docstring for the full decision record) -- introduced here as
    # plumbing only, ahead of any real enforcement, so a future prompt that
    # adds genuine multi-tenant checks doesn't need another `AgentState`
    # migration to get a tenant id to check against. Deliberately
    # request-scoped, exactly like `selected_database`/`selected_model`
    # above -- never a process-global -- so one caller's tenant can never
    # leak into a concurrent request for a different one. Read, never
    # re-resolved, by any node that comes to depend on it later; today,
    # nothing reads it -- the only real enforcement of tenant scoping in
    # this codebase remains `identity.share_policy.authorize_share_action`,
    # which resolves its own tenant id independently via
    # `security.tenancy.resolve_actor_tenant_id` and does not read this
    # field.
    tenant_id: str | None

    # Set by retrieve_schema
    schema_tables: list[TableSchema]
    schema_context_text: str

    # Set by retrieve_golden_examples_node, which runs immediately after
    # retrieve_schema (and before plan_query) -- the best-matching
    # human-approved (question, SQL) pairs for this database's golden
    # dataset (see embeddings.golden_examples), or None if the feature is
    # disabled (Settings.enable_golden_examples), the store is empty/
    # unreachable (fails open, same as plan_query_node), or nothing cleared
    # Settings.golden_examples_min_similarity. Reused unchanged across every
    # generate_sql retry for this question, exactly like query_plan below
    # (only recomputed if retrieve_schema itself reruns) -- injected into
    # generate_sql's prompt via agent.llm_client._build_golden_examples_block.
    golden_examples: list[GoldenExample] | None

    # Set by retrieve_business_context_node, which runs between
    # retrieve_golden_examples and plan_query (see agent/graph.py) -- the
    # ranked business-context chunks (retrieval.models.ScoredChunk.to_context_dict()
    # shape) retrieval.retriever.retrieve_business_context found for this
    # question: table/column/relationship descriptions, glossary terms,
    # metric definitions, curated SQL examples, and documentation snippets.
    # Always a plain list of dicts, never the pydantic Chunk/ScoredChunk
    # objects themselves -- same "TypedDict state holds plain dicts, not
    # model instances" convention TableSchema/AttemptRecord already follow.
    # Empty list (not None) when retrieval found nothing, was disabled, or
    # failed -- see retrieval_warnings below for *why* it's empty. Reused
    # unchanged across every generate_sql retry for this question (only
    # recomputed if retrieve_schema itself reruns), exactly like
    # golden_examples/query_plan above -- injected into generate_sql's
    # prompt via agent.llm_client._build_business_context_block.
    retrieved_context: list[dict]
    # The exact text retrieval.retriever.retrieve_business_context embedded
    # as its query -- normally just `question` itself, kept separately on
    # state (rather than re-read from `question`) so a future caller can
    # tell "this is literally what was searched for" apart from the
    # original question text without assuming the two always match.
    retrieval_query: str | None
    # chunk_id of every chunk that made it into retrieved_context, in the
    # same order -- for traceability (e.g. an admin UI or eval script
    # tying a generation back to exactly which knowledge-base entries
    # informed it) independent of parsing retrieved_context itself.
    retrieval_sources: list[str]
    # Non-empty whenever business-context retrieval degraded for any reason
    # (disabled, empty/missing collection, embedding failure, vector-store
    # failure, or nothing cleared the similarity threshold) -- see
    # retrieval.retriever.retrieve_business_context's docstring for the
    # full fallback contract. Never a reason status becomes "failed":
    # retrieval is an accuracy aid, exactly like golden_examples/query_plan.
    retrieval_warnings: list[str]
    # Observability-only counters from the retrieval call (candidate/final
    # chunk counts, per-type breakdown, duration_ms) -- never read by any
    # node's own logic, purely for logging/debugging a retrieval decision,
    # the same role agent.state.DatabaseSelection.scores_by_db plays for
    # multi-database routing.
    retrieval_metadata: dict

    # Set by plan_query_node, which runs between retrieve_schema and
    # generate_sql -- an ordered list of concrete steps the model judged
    # necessary to answer the question (grouping, metrics, filters, whether
    # a top-N-per-group ranking or a period-over-period comparison is
    # needed), or None if planning was skipped. Skipped whenever
    # Settings.enable_query_planning is False, or the question matched no
    # agent.complexity signal (see plan_query_node's docstring) -- the
    # overwhelmingly common case, so this is None for most questions.
    # Reused unchanged across every generate_sql retry for this question
    # (only recomputed if retrieve_schema itself reruns, on a
    # missing_reference retry) -- injected into generate_sql's prompt
    # (agent.llm_client._build_plan_block) and checked against the
    # generated SQL by review_sql_node below.
    query_plan: list[str] | None

    # Set by review_sql_node, only when query_plan is not None (nothing to
    # check against otherwise) -- whether the LLM judged the just-generated
    # SQL to implement every plan step. None means review didn't run.
    plan_review_passed: bool | None
    # The reviewer's critique when plan_review_passed is False -- fed back
    # into the next generate_sql attempt the same way any other retryable
    # error is (error_history/last_error_category). None when review passed
    # or didn't run.
    plan_review_feedback: str | None

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

    # Set once by the caller (agent.graph.run_agent), via
    # agent.complexity.compute_max_retries -- this question's effective
    # retry budget, which may exceed Settings.max_retries if the question
    # text matched one or more complexity signals (top-N-per-group phrasing,
    # growth/period comparisons, several metrics at once). Every retry-vs-
    # give-up check (validate_sql/estimate_query_cost/execute_sql) reads
    # this instead of Settings.max_retries directly, falling back to it if
    # unset (e.g. a test constructing AgentState by hand).
    max_retries: int
    # The signals agent.complexity.detect_complexity_signals matched for
    # this question (empty list if none) -- carried on state purely for
    # observability (logged once by run_agent), never read by any node's
    # own logic.
    complexity_signals: list[str]

    # Full per-attempt timeline (see AttemptRecord) -- one entry per
    # concluded attempt, for the UI's "Retry timeline" expander and for
    # inspecting/testing the retry loop's behavior directly.
    attempt_history: Annotated[list[AttemptRecord], operator.add]

    # Category of the most recent failure -- a agent.sql_validator.ViolationType
    # value ("empty" | "parse_error" | "nested_aggregate" | "restricted_column"
    # | "high_cost" | "safety_violation") or an
    # agent.error_classification.ExecutionErrorCategory value ("syntax" |
    # "missing_reference" | "aggregate_nesting" | "timeout" | "unknown"), or
    # None. Overwritten each attempt (not accumulated) -- generate_sql_node
    # reads it to give the next LLM call a more targeted retry hint
    # (agent.llm_client._ERROR_CATEGORY_HINTS) than a generic "fix it."
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
