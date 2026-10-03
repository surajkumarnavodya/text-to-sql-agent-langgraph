"""LangGraph node functions for the self-correcting Text-to-SQL agent.

Each function takes the current `AgentState` and returns a partial dict of
updates (LangGraph's convention) -- this is what makes the state
transitions "visible": every node's contract is exactly its input and
output state, independent of graph wiring (see `agent/graph.py`).

Flow: sanitize_input -> classify_followup -> retrieve_schema -> generate_sql
-> validate_sql -> estimate_query_cost -> execute_sql, with
validate_sql/estimate_query_cost/execute_sql routing back to generate_sql on
failure (see `route_after_validation` / `route_after_cost_estimate` /
`route_after_execution`), capped at `Settings.max_retries`. See
`agent/graph.py`'s module docstring for the full diagram, including the
terminal "rejected"/"needs_clarification"/"rate_limited" shapes.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from analytics.engine import compute_analytics_result
from analytics.forecasting import generate_forecast
from analytics.models import AnalyticsResult
from recommendation.engine import RecommendationInputs, generate_recommendations
from retrieval.retriever import extract_governing_metrics, retrieve_business_context
from sqlalchemy.exc import SQLAlchemyError

from agent.analytical_plan import AnalyticalPlan, render_plan_as_steps
from agent.authz import Permission, has_role_permission
from agent.error_classification import ExecutionErrorCategory, classify_execution_error
from agent.exceptions import (
    MalformedLLMOutputError,
    OffTopicQuestionError,
    OllamaUnavailableError,
    SchemaRetrievalError,
)
from agent.followup import classify_followup
from agent.input_guard import (
    NO_RELEVANT_DATA_MESSAGE,
    check_input,
    rejection_message,
    sanitize_conversation_history,
)
from agent.insight import is_insight_grounded, should_skip_insight, summarize_result
from agent.intent import AnalyticalIntentType, intent_implies_planning
from agent.llm_client import (
    generate_analytical_intent_from_llm,
    generate_analytical_plan_from_llm,
    generate_insight_from_llm,
    generate_query_plan_from_llm,
    generate_sql_from_llm,
    review_sql_against_metrics_from_llm,
    review_sql_against_plan_from_llm,
)
from agent.plan_validator import validate_plan
from agent.rate_limit import (
    LLM_CALL_LIMIT_MESSAGE,
    get_database_execution_limiter,
    get_llm_call_limiter,
)
from agent.sql_validator import (
    SAFETY_VIOLATION_TYPES,
    enforce_row_limit,
    find_restricted_column_references,
    find_unexpected_table_references,
    qualify_table_schema,
    references_multiple_tables,
    strip_row_limit,
    validate_sql,
)
from agent.state import (
    AgentState,
    AttemptRecord,
    ConversationExchange,
    GoldenExample,
    StageTiming,
    TableSchema,
)
from config.sensitive_columns import load_sensitive_columns
from config.settings import get_settings
from config.table_descriptions import apply_table_description, load_table_descriptions
from db.connection import get_connection, get_read_only_engine, get_sqlglot_dialect
from db.execution import execute_readonly_sql
from db.query_cost import MODERATE_COST_NOTICE, estimate_query_cost, high_cost_error_message
from db.query_store import QueryStoreFindings, get_cached_query_store_findings
from db.schema_introspection import extract_ddl_column_names
from embeddings.golden_examples import retrieve_golden_examples
from embeddings.retriever import retrieve_relevant_schema, select_database
from observability.metrics import get_default_metrics
from security.audit_log import log_security_event
from security.injection_patterns import INJECTION_PATTERNS
from security.redaction import redact_secrets
from security.tenancy import DEFAULT_TENANT_ID

# Re-exported for `tests/test_agent_nodes.py`'s
# `monkeypatch.setattr("agent.nodes.execute_readonly_sql", ...)` seam -- the
# actual implementation (and its own tests) lives in `db/execution.py`, a
# pure database-execution concern with no LangGraph/state dependency. Kept as
# a plain re-import (not re-implemented) so there is exactly one definition.
__all__ = ["execute_readonly_sql"]

logger = logging.getLogger(__name__)


def _query_fingerprint(sql: str | None) -> str | None:
    """A short, non-reversible fingerprint of `sql` for trace logs --
    `None` when there's no SQL yet for this stage.

    Prompt 23 (observability, evaluation & reliability): the trace log
    below (see `_timed_node`) needs to answer "did the SQL text change
    between attempt 1 and attempt 2" and "which stage produced which
    query" without ever putting SQL text -- or anything derived from it
    that could leak a literal value -- into a log line. A plain SHA-256
    hash (truncated for readability) gives a stable, comparable identifier
    with zero content leakage, which is a stronger guarantee than a
    masked-literal preview would be (see `db.query_store._mask_literals`'s
    own, different use case: a *human-readable* preview, which is never
    what a log line needs). Deliberately a free function, not a method on
    any class, since it has no state of its own.
    """
    if not sql:
        return None
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def _timed_node(stage: str) -> Callable[[Callable[[AgentState], dict[str, Any]]], Callable]:
    """Records a node's wall-clock duration into `stage_timings`, uniformly,
    and emits a structured `[trace]` log line for that same call.

    Applied as a decorator rather than hand-timing each node body: every
    node here has several early-return branches for different outcomes
    (safety violation, retryable failure, success, ...), and this measures
    the call the same way regardless of which branch it took, without the
    node's own logic needing to know timing exists. See
    `scripts/profile_pipeline.py` for how this data gets turned into a
    stage-by-stage breakdown.

    Prompt 23 (observability, evaluation & reliability) added the
    `[trace]` line below, additive to the pre-existing `[timing]` one
    (kept byte-for-byte unchanged -- `scripts/profile_pipeline.py` parses
    it) rather than folding new fields into it, so a tool that already
    greps for `[timing]` is unaffected. `correlation_id`/`tenant_id` are
    never included explicitly here -- both are already stamped onto
    *every* log line (this one included) by `security.audit_log
    .CorrelationIdLogFilter`/`TenantIdLogFilter`, attached once at the
    root logger (`config.settings.configure_logging`), so repeating them
    per call site would be redundant, not additive. Every other field is
    read from `state`/`result` by well-known, already-existing key names
    (`selected_model`, `selected_database`, `sql`, `last_error_category`,
    `row_count`) -- zero changes needed to any individual node's own
    return contract, the same "cross-cutting, no node needs to know this
    exists" principle `stage_timings` itself already established. Every
    field is optional and omitted (not rendered as a literal `"None"`)
    when not meaningful for this particular stage/outcome -- e.g. `model`
    is absent before `retrieve_schema` has even run, `result_size` is
    absent for every stage except a successful `execute_sql`.
    """

    def decorator(node_func: Callable[[AgentState], dict[str, Any]]) -> Callable:
        @functools.wraps(node_func)
        def wrapper(state: AgentState) -> dict[str, Any]:
            attempt_number = state.get("retry_count", 0) + 1
            start = time.perf_counter()
            result = node_func(state)
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "[timing] stage=%s attempt=%d duration_ms=%.1f", stage, attempt_number, duration_ms
            )
            # The merged, post-update view -- a pass-through node (e.g. a
            # disabled-flag no-op) may return a result dict with none of
            # these keys at all, in which case the value already present in
            # the *input* state (set by an earlier stage this run) is still
            # the right one to report, not a false "unknown."
            merged = {**state, **result}
            trace_fields: dict[str, Any] = {}
            status = merged.get("status")
            if status is not None:
                trace_fields["status"] = status
            error_category = merged.get("last_error_category")
            if error_category is not None:
                trace_fields["error_category"] = error_category
            model = merged.get("selected_model")
            if model is not None:
                trace_fields["model"] = model
            db_provider = merged.get("selected_database")
            if db_provider is not None:
                trace_fields["db_provider"] = db_provider
            query_fingerprint = _query_fingerprint(merged.get("sql"))
            if query_fingerprint is not None:
                trace_fields["query_fingerprint"] = query_fingerprint
            result_size = merged.get("row_count")
            if result_size is not None:
                trace_fields["result_size"] = result_size
            if trace_fields:
                rendered = " ".join(f"{key}={value!r}" for key, value in trace_fields.items())
                logger.info(
                    "[trace] stage=%s attempt=%d duration_ms=%.1f %s",
                    stage,
                    attempt_number,
                    duration_ms,
                    rendered,
                )
            timing: StageTiming = {
                "stage": stage,
                "attempt": attempt_number,
                "duration_ms": round(duration_ms, 2),
            }
            result = dict(result)
            result["stage_timings"] = [timing]
            return result

        return wrapper

    return decorator


def _give_up_explanation(state: AgentState, detailed_message: str) -> str:
    """Picks the final failure_explanation when the retry budget is exhausted.

    When every attempt's schema retrieval came up empty (`schema_tables` is
    whatever the *last* `retrieve_schema_node` call set, so this reflects
    the final attempt), the real, standardized "couldn't find data related
    to that" message (CLAUDE.md's Part 3 -- "existing behavior, keep as
    is") is more honest and more useful than a generic "gave up after N
    attempts" -- the model was never going to succeed without relevant
    tables to work with, no matter how many retries it got. Otherwise, the
    detailed technical message (as before) is what's shown.
    """
    if not state.get("schema_tables"):
        return NO_RELEVANT_DATA_MESSAGE
    return detailed_message


def _effective_max_retries(state: AgentState, settings) -> int:
    """Returns this question's retry budget: `state["max_retries"]` if set, else the global default.

    `agent.graph.run_agent` always sets `state["max_retries"]` up front (see
    `agent.complexity.compute_max_retries`) -- the fallback to
    `settings.max_retries` here only matters for a test constructing
    `AgentState` by hand without that field.
    """
    return state.get("max_retries", settings.max_retries)


def _selected_db_name(state: AgentState) -> str:
    """Returns `state["selected_database"]`, guaranteed non-None by this point.

    `retrieve_schema_node` always runs (and sets this) before
    `validate_sql`/`estimate_query_cost`/`execute_sql` -- see `agent/graph.py`
    -- so a None here is a precondition violation, not a real runtime case.
    """
    selected_database = state["selected_database"]
    if selected_database is None:
        # 2026 Phase 3 security review (bandit B101): an explicit raise, not
        # `assert` -- `assert` is stripped entirely under `python -O`,
        # which would turn this precondition violation into a confusing
        # downstream `None`-related error instead of a clear one here.
        raise RuntimeError("reached with no selected_database; retrieve_schema_node must run first")
    return selected_database


# Patterns for the invalid identifier named in a "missing reference" driver
# error, across the four supported engines -- deliberately separate from
# (and narrower than) `agent.error_classification`'s own broader keyword
# list, since these need to *capture* the actual name, not just detect the
# error's category. Order doesn't matter; only the first pattern that
# matches is used, per error text.
_INVALID_IDENTIFIER_PATTERNS = (
    re.compile(r"invalid column name\s+['\"`]([A-Za-z_][A-Za-z0-9_]*)['\"`]", re.IGNORECASE),
    re.compile(r"unknown column\s+['\"`]([A-Za-z_][A-Za-z0-9_]*)['\"`]", re.IGNORECASE),
    re.compile(r"no such column:?\s+['\"`]?([A-Za-z_][A-Za-z0-9_]*)['\"`]?", re.IGNORECASE),
    re.compile(r'column\s+"([A-Za-z_][A-Za-z0-9_]*)"\s+does not exist', re.IGNORECASE),
    re.compile(r'ora-00904:\s*(?:"[^"]+"\.)?"([A-Za-z_][A-Za-z0-9_]*)"', re.IGNORECASE),
)

# A generated SQL identifier this short is never worth fuzzy-suggesting a
# correction for -- too many unrelated real columns would coincidentally
# score above the cutoff (e.g. "id"), producing a confidently wrong "did
# you mean."
_MIN_SUGGESTION_LENGTH = 4


# Re-bound to the shared implementation in `db.schema_introspection` (Prompt
# 12, `12_ANALYTICAL_PLANNING_CONTRACT.md`) -- `agent.plan_validator` now
# needs the identical DDL-column-name extraction `_suggest_correct_column`
# below already relied on, so the one implementation moved there rather than
# being duplicated. Kept under this module's original private name (not a
# plain `from ... import extract_ddl_column_names` at call sites) so the
# diff below stays minimal and `_suggest_correct_column`'s own docstring
# references keep working unchanged.
_extract_column_names = extract_ddl_column_names


def _suggest_correct_column(
    error_text: str, schema_tables: list[TableSchema]
) -> tuple[str, str] | None:
    """Best-effort "did you mean" suggestion for a missing-reference column error.

    A mechanical backstop for a real, recurring, two-part failure mode:
    this schema's naming convention prefixes many descriptive columns with
    a locale (e.g. `EnglishProductName`, `EnglishProductSubcategoryName`),
    and the model was observed -- reliably, across multiple questions,
    even after adding an explicit system-prompt instruction not to --
    generating the shorter, unprefixed variant instead. Worse, even after
    being told the correct column name (an earlier version of this
    function returned a bare name), the model was then observed attaching
    the corrected name to the *wrong table's* alias (e.g.
    `DimProduct`'s alias, when the column actually belongs to
    `DimProductSubcategory`) -- so the suggestion here names both the
    column *and* the table it actually belongs to, not just the column.

    Extracts the invalid identifier from the driver's own error text, then:

    1. First looks for a real column that plainly *contains* the guessed
       name (e.g. "EnglishProductSubcategoryName" contains
       "ProductSubcategoryName") -- the dominant real-world shape of this
       failure. Deliberately checked before fuzzy matching:
       `difflib.SequenceMatcher.ratio()`'s formula normalizes by combined
       string length, so it can score a short, unrelated column *higher*
       than the long-but-correct one purely because of the length
       difference (verified empirically: for invalid name "ProductName",
       plain `difflib` preferred "ProductKey" (0.76) over the actually-
       correct "EnglishProductName" (0.76, narrowly *lower*) -- exactly
       backwards for this failure mode). Ties among several containing
       candidates (e.g. this schema's English/Spanish/French name
       variants) prefer an "English"-named one, this app's conventional
       default language column for an otherwise-unqualified question.
    2. Falls back to `difflib` fuzzy matching (stdlib, no new dependency)
       only when no containing column exists -- for genuine typos/near-
       misses that aren't a clean substring relationship.

    Returns `(column_name, table_name)`, or None -- never a guess of its
    own -- if no identifier could be parsed out, it's too short to match
    safely, or no sufficiently close real column exists; silence is better
    than a confidently wrong suggestion.
    """
    invalid_name = None
    for pattern in _INVALID_IDENTIFIER_PATTERNS:
        match = pattern.search(error_text)
        if match:
            invalid_name = match.group(1)
            break
    if invalid_name is None or len(invalid_name) < _MIN_SUGGESTION_LENGTH:
        return None

    # First table (in retrieval order) that actually declares a given
    # column name -- good enough for a "here's where to find it" pointer;
    # this isn't trying to resolve genuine cross-table name collisions.
    column_owner: dict[str, str] = {}
    for table in schema_tables:
        for column_name in _extract_column_names(table["ddl"]):
            column_owner.setdefault(column_name, table["table_name"])
    if not column_owner:
        return None

    lowered_invalid = invalid_name.lower()
    containing = [
        col
        for col in column_owner
        if col.lower() != lowered_invalid and lowered_invalid in col.lower()
    ]
    if containing:
        containing.sort(key=lambda col: ("english" not in col.lower(), len(col), col))
        best = containing[0]
        return best, column_owner[best]

    matches = difflib.get_close_matches(invalid_name, list(column_owner), n=1, cutoff=0.6)
    if not matches or matches[0].lower() == lowered_invalid:
        return None
    best = matches[0]
    return best, column_owner[best]


@_timed_node("sanitize_input")
def sanitize_input_node(state: AgentState) -> dict[str, Any]:
    """The graph's true entry point: sanitize, then gate obviously-unusable input.

    Runs `agent.input_guard.check_input` -- length cap, Unicode
    normalization (NFKC + confusables-folding, closing the homoglyph-
    obfuscation gap plain NFKC leaves open), injection-pattern detection,
    and off-topic/gibberish classification -- before anything else touches
    the question, including `classify_followup_node`'s own text analysis.

    On rejection, `status` becomes "rejected" and the graph ends
    immediately (see `route_after_sanitization`) with a standardized,
    non-technical message (`agent.input_guard.rejection_message`) -- never
    the raw reason or which pattern matched, so a rejection gives an
    attacker no signal to iterate against and doesn't alarm a legitimate
    user who just phrased something unusually (CLAUDE.md's Part 3 rule).

    On a pass, `question` is overwritten with the *normalized* text -- every
    downstream node (classify_followup, retrieve_schema, generate_sql) reads
    `state["question"]` expecting the current, authoritative text, and
    should never need to re-normalize it themselves.
    """
    settings = get_settings()
    result = check_input(state["question"], max_length=settings.max_question_length)

    # 2026 Phase 2 security review (finding LLM-01): conversation_history
    # gets the same normalization/injection-detection treatment as the
    # live question, here (the graph's true entry point) rather than
    # later, closest to sanitize_input_node's own "before anything else
    # touches it" contract -- see sanitize_conversation_history's
    # docstring for why a match here doesn't reject the request the way
    # the live question's own match does.
    sanitized_history = sanitize_conversation_history(
        state.get("conversation_history") or [],
        max_length=settings.max_question_length,
        max_turns=settings.max_conversation_history_turns,
    )

    if not result.passed:
        if result.reason is None:
            # 2026 Phase 3 security review (bandit B101): explicit raise,
            # not `assert` -- see agent.nodes.execute_sql_node's identical
            # fix for why (stripped under `python -O`).
            raise RuntimeError("GuardResult.passed is False but reason is None")
        message = rejection_message(result.reason, db_name=settings.db_name)
        logger.info(
            "[sanitize_input] rejected reason=%s -- see agent.input_guard logs for detail",
            result.reason,
        )
        log_security_event(
            "input_rejected",
            "warning" if result.reason == "injection_detected" else "info",
            "A question was rejected at the input-sanitization gate before any "
            "schema retrieval or LLM call.",
            reason=result.reason,
        )
        return {
            "status": "rejected",
            "rejection_reason": result.reason,
            "rejection_message": message,
            "conversation_history": sanitized_history,
        }

    return {
        "question": result.cleaned_question,
        "status": "classifying_followup",
        "conversation_history": sanitized_history,
    }


@_timed_node("classify_followup")
def classify_followup_node(state: AgentState) -> dict[str, Any]:
    """Decides whether this question is standalone, a follow-up, or ambiguous.

    Runs first, before any schema retrieval or LLM call, on a cheap
    regex-only heuristic (`agent.followup.classify_followup`) -- see that
    module's docstring for the referring_signal / has_subject decision
    table. Logged the same way every other agent decision is logged (see
    CLAUDE.md), so the classification and (on a follow-up) which prior
    exchange it resolved against are visible in the terminal, not just
    encoded in state.

    On "ambiguous", the graph short-circuits straight to END with
    `status="needs_clarification"` rather than guessing -- the same
    fail-closed philosophy as the validator's SAFETY_VIOLATION_TYPES path,
    just for "I don't know what you're asking" instead of "I won't run
    that."
    """
    question = state["question"]
    conversation_history = state.get("conversation_history") or []
    result = classify_followup(question, has_history=bool(conversation_history))

    logger.info(
        "[classify_followup] question=%r classification=%s referring_signal=%s "
        "has_subject=%s matched_patterns=%s",
        question,
        result.classification,
        result.referring_signal,
        result.has_subject,
        result.matched_patterns,
    )

    if result.classification == "ambiguous":
        if result.referring_signal:
            message = (
                "This looks like it's referring back to a previous question ('that', "
                "'those', 'now', ...), but there's no earlier question in this session "
                "yet to resolve it against. Please ask the full question directly."
            )
        else:
            message = (
                "This question doesn't have enough on its own for me to tell what "
                "you're asking. Could you rephrase with the topic or metric you want?"
            )
        logger.info("[classify_followup] ambiguous -- asking for clarification: %s", message)
        return {
            "followup_classification": "ambiguous",
            "followup_resolved_against": None,
            "status": "needs_clarification",
            "clarification_message": message,
        }

    resolved_against: ConversationExchange | None = None
    if result.classification == "followup":
        resolved_against = conversation_history[-1]
        logger.info(
            "[classify_followup] resolved as follow-up against prior question=%r",
            resolved_against["question"],
        )

    return {
        "followup_classification": result.classification,
        "followup_resolved_against": resolved_against,
        "status": "retrieving_schema",
    }


@_timed_node("retrieve_schema")
def retrieve_schema_node(state: AgentState) -> dict[str, Any]:
    """Embeds the question and retrieves the top-k most relevant table DDLs.

    This is the schema-scoping step: for a large schema, only the tables
    ChromaDB judges relevant are passed to the LLM, keeping the prompt small
    and reducing the odds of the model inventing joins against irrelevant
    tables.

    Also the re-entry point on a `missing_reference` execution failure (see
    `execute_sql_node` / `route_after_execution`): if the previous attempt's
    SQL referenced a table/column that doesn't exist, the wrong tables may
    have been retrieved the first time around, not just badly written SQL.
    On that path (`retry_count > 0` and there's error history), the query
    text folds in the actual DB error -- which often names the missing
    identifier, a useful extra signal for the similarity search -- and
    `top_k` is widened, so the retry gets a genuinely different candidate
    set rather than re-asking the same question the same way.

    After retrieval, every table's DDL is re-augmented with the *current*
    contents of `config/table_descriptions.yaml` (via
    `config.table_descriptions.apply_table_description`) -- freshly loaded
    from disk on this call, not a snapshot baked in at embedding-build time
    (see `embeddings/schema_indexer.py`'s docstring for why). This is what
    makes a hand-edit to that file -- fixing a wrong column note, adding a
    new disambiguation -- take effect on the very next question, with no
    embeddings rebuild required.

    Also where multi-database auto-routing happens: on the *first* pass
    (`state["selected_database"]` not yet set), `embeddings.retriever.
    select_database` picks which configured database this question is
    about, before any per-table retrieval runs. On the missing_reference
    retry re-entry described above, the already-selected database is
    reused rather than re-routed -- a retry must keep targeting the same
    database it already generated/executed SQL against, not silently jump
    to a different one mid-attempt.
    """
    settings = get_settings()
    question = state["question"]
    error_history = state.get("error_history", [])
    is_schema_retry = state.get("retry_count", 0) > 0 and bool(error_history)
    resolved_against = state.get("followup_resolved_against")

    query_text = question
    top_k = settings.schema_top_k
    if is_schema_retry:
        query_text = f"{question}\n{error_history[-1]}"
        top_k = settings.schema_top_k + 2
        logger.info(
            "[retrieve_schema] retry after missing-reference failure: "
            "broadening top_k %d -> %d with error context",
            settings.schema_top_k,
            top_k,
        )
    elif resolved_against is not None:
        # The question alone may lack a subject ("now break that down by
        # month") -- fold the prior question's text in so similarity search
        # still has something to match tables against. Widened by 1 rather
        # than the +2 used for a missing-reference retry: this is filling a
        # gap, not correcting a wrong retrieval.
        query_text = f"{question}\n{resolved_against['question']}"
        top_k = settings.schema_top_k + 1
        logger.info(
            "[retrieve_schema] follow-up: folding prior question into retrieval "
            "query, top_k %d -> %d",
            settings.schema_top_k,
            top_k,
        )
    logger.info("[retrieve_schema] question=%r", question)

    try:
        selected_database = state.get("selected_database")
        if selected_database is None:
            # Prompt 20: routing only ever considers databases this caller's
            # tenant may query. `state["tenant_id"]` was resolved once, by
            # `run_agent`, from the request's verified identity -- never from
            # the question text or any client-supplied field.
            db_selection = select_database(query_text, settings, tenant_id=state.get("tenant_id"))
            selected_database = db_selection.db_name
            logger.info(
                "[retrieve_schema] auto-routed to database %r "
                "(top_table_score=%.4f, scores_by_db=%s)",
                selected_database,
                db_selection.top_table_score,
                db_selection.scores_by_db,
            )
        else:
            logger.info(
                "[retrieve_schema] retry: reusing previously selected database %r",
                selected_database,
            )
        tables = retrieve_relevant_schema(query_text, db_name=selected_database, top_k=top_k)
    except SchemaRetrievalError as exc:
        logger.error("[retrieve_schema] failed: %s", exc)
        attempt_number = state.get("retry_count", 0) + 1
        record: AttemptRecord = {
            "attempt": attempt_number,
            "sql": state.get("sql"),
            "outcome": "schema_retrieval_error",
            "error": str(exc),
            "will_retry": False,
        }
        return {
            "status": "failed",
            "error_history": [f"Schema retrieval failed: {exc}"],
            "attempt_history": [record],
            "failure_explanation": f"Could not retrieve schema context: {exc}",
        }

    if not tables:
        logger.warning("[retrieve_schema] no relevant tables found for question")

    descriptions = load_table_descriptions()
    tables = [
        TableSchema(
            table_name=table["table_name"],
            ddl=apply_table_description(table["ddl"], descriptions.get(table["table_name"])),
            similarity_score=table["similarity_score"],
        )
        for table in tables
    ]

    context_text = "\n\n".join(table["ddl"] for table in tables)
    logger.info(
        "[retrieve_schema] retrieved %d table(s): %s",
        len(tables),
        [t["table_name"] for t in tables],
    )

    # Detection-only, never blocks: the same normalize-and-frame-as-data
    # defenses already applied to sampled values (security.sanitization,
    # the system prompt's untrusted-data framing) are what actually bound
    # the consequences if this ever fires for real -- this is purely an
    # operator-visibility signal that the *content itself* looked
    # injection-shaped, reusing the exact same pattern set
    # agent.input_guard applies to typed questions (security.
    # injection_patterns) rather than a second, drifting copy. Blocking
    # here instead would risk refusing a legitimate question just because
    # real business data (a promo name, a free-text comment) happened to
    # contain an ordinary phrase this cheap regex layer also fires on --
    # see SECURITY.md's "database content is untrusted input too" section.
    matched_patterns = [
        name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(context_text)
    ]
    if matched_patterns:
        logger.warning(
            "[retrieve_schema] [rag_poisoning] retrieved schema context matched "
            "injection-style pattern(s): %s -- proceeding (detection only)",
            matched_patterns,
        )
        log_security_event(
            "possible_rag_poisoning",
            "warning",
            "Retrieved schema/sampled-value content matched an injection-style "
            "pattern before being included in the generation prompt.",
            matched_patterns=matched_patterns,
            tables=[t["table_name"] for t in tables],
        )

    return {
        "selected_database": selected_database,
        "schema_tables": tables,
        "schema_context_text": context_text,
        "status": "generating",
    }


@_timed_node("retrieve_golden_examples")
def retrieve_golden_examples_node(state: AgentState) -> dict[str, Any]:
    """Looks up human-approved past (question, SQL) pairs for this database's
    golden dataset (see `embeddings.golden_examples`) and stores the
    best-matching ones for `generate_sql_node` to inject as few-shot
    examples.

    Runs between `retrieve_schema` and `plan_query` (see `agent/graph.py`),
    only queries the store when `Settings.enable_golden_examples` is True,
    and fails open on absolutely anything -- a disabled flag, an empty/
    missing collection, or any lookup error all resolve to
    `golden_examples = None`, never a reason a question can't be answered.
    `embeddings.golden_examples.retrieve_golden_examples` already never
    raises on its own; the `except Exception` here is defense-in-depth
    against anything unexpected surfacing from this call site specifically,
    matching `plan_query_node`'s identical fail-open contract right below.
    """
    settings = get_settings()
    if not settings.enable_golden_examples:
        logger.info("[retrieve_golden_examples] skipped (enable_golden_examples=False)")
        return {"golden_examples": None, "status": "generating"}

    question = state["question"]
    db_name = state.get("selected_database") or "default"
    try:
        # Prompt 20: only this tenant's own approved examples may reach this
        # tenant's generation prompt, even when the database is shared.
        examples: list[GoldenExample] = retrieve_golden_examples(
            question,
            db_name,
            settings,
            tenant_id=state.get("tenant_id") or DEFAULT_TENANT_ID,
        )
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning("[retrieve_golden_examples] lookup failed, proceeding without: %s", exc)
        return {"golden_examples": None, "status": "generating"}

    logger.info(
        "[retrieve_golden_examples] found %d example(s) for database %r",
        len(examples),
        db_name,
    )
    return {"golden_examples": examples or None, "status": "generating"}


@_timed_node("retrieve_business_context")
def retrieve_business_context_node(state: AgentState) -> dict[str, Any]:
    """Looks up semantically-relevant business context -- table/column/
    relationship descriptions, glossary terms, metric definitions, curated
    SQL examples, and documentation snippets (see `retrieval/`) -- and
    stores the ranked result for `generate_sql_node` to inject as an
    additional, clearly-labeled prompt block.

    Runs between `retrieve_golden_examples` and `classify_analytical_intent`
    (see `agent/graph.py`): by this point `state["selected_database"]` is
    already resolved (needed to pick the right collection), and this node's
    own output has no bearing on golden-example retrieval or query planning,
    so the ordering relative to those two doesn't matter functionally --
    placed here so every downstream prompt-building step
    (`classify_analytical_intent_node`, `plan_query_node`,
    `generate_sql_node`) can see the *complete* set of retrieved context in
    one place.

    Also extracts `state["governing_metrics"]` (Prompt 10,
    `10_GOVERNED_METRICS_CONTRACT.md`) from the same already-retrieved
    result via `retrieval.retriever.extract_governing_metrics` -- no
    second query, just a filter over chunks already judged relevant to
    this question. Consumed by `generate_sql_node` (mandatory-use prompt
    block) and `review_metric_conformance_node` (enforcement).

    This is an accuracy aid, never a gate: `retrieval.retriever
    .retrieve_business_context` already never raises (see that function's
    own fail-open contract), so the `except Exception` here is
    defense-in-depth against anything unexpected surfacing from this call
    site specifically -- the same posture `retrieve_golden_examples_node`
    right above takes for its own Chroma-backed lookup. The live database
    schema (`state["schema_context_text"]`, from `retrieve_schema_node`)
    remains the sole authority on what tables/columns actually exist --
    this node only ever *adds* optional business-meaning context on top,
    never replaces or overrides it (see `docs/vector-retrieval-design.md`).
    """
    settings = get_settings()
    question = state["question"]
    db_name = state.get("selected_database") or "default"
    caller_roles = state.get("caller_roles", ())

    try:
        # Prompt 20: tenant-scoped -- a published semantic-catalog concept
        # belongs to one tenant and must never reach another's prompt, even
        # when both legitimately query the same database.
        result = retrieve_business_context(
            question,
            db_name,
            caller_roles,
            settings,
            tenant_id=state.get("tenant_id") or DEFAULT_TENANT_ID,
        )
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[retrieve_business_context] unexpected failure, proceeding without: %s", exc
        )
        return {
            "retrieved_context": [],
            "retrieval_query": question,
            "retrieval_sources": [],
            "retrieval_warnings": [f"Business-context retrieval failed unexpectedly: {exc}"],
            "retrieval_metadata": {},
            "governing_metrics": [],
            "status": "generating",
        }

    governing_metrics = extract_governing_metrics(result.items)
    logger.info(
        "[retrieve_business_context] database=%r retrieved %d chunk(s), "
        "governing_metrics=%d, warnings=%s",
        db_name,
        len(result.items),
        len(governing_metrics),
        result.warnings,
    )
    return {
        "retrieved_context": [item.to_context_dict() for item in result.items],
        "retrieval_query": result.query,
        "retrieval_sources": result.sources,
        "retrieval_warnings": result.warnings,
        "retrieval_metadata": result.metadata,
        "governing_metrics": governing_metrics,
        "status": "generating",
    }


@_timed_node("classify_analytical_intent")
def classify_analytical_intent_node(state: AgentState) -> dict[str, Any]:
    """Classifies what *kind* of analytical question this is -- Prompt 11
    (`11_ANALYTICAL_INTENT_CONTRACT.md`) -- before any SQL is generated.

    Runs between `retrieve_business_context` and `plan_query` (see
    `agent/graph.py`), on **every** question when `Settings
    .enable_intent_classification` is True (the default) -- unlike
    `plan_query_node`'s own LLM call, which only fires for a
    complexity-flagged question, this one is meant to be foundational
    (per the prompt's own "before SQL generation" framing), so it is not
    gated by `agent.complexity`'s signals.

    Fails open exactly like `plan_query_node`: a disabled flag, an
    unreachable Ollama server, or an unparseable response all resolve to
    `state["analytical_intent"] = None`, logged but never a reason the
    question can't be answered, and never a terminal status.

    **Why this cannot bypass authorization or the existing fallback
    behavior, by construction:** `Permission.ASK` is already enforced by
    a FastAPI dependency before `run_agent`/the graph are even built --
    this node runs deep inside an already-authorized request, and never
    itself reads or writes `caller_roles`, `max_retries`, or any field
    `validate_sql_node`'s own restricted-column authorization check
    reads. Its only consumer is `plan_query_node`'s gate/prompt (see that
    node's docstring) -- when `analytical_intent` is `None` for any
    reason, that gate reduces to exactly `agent.complexity`'s signals
    alone, byte-identical to this node's pre-Prompt-11 behavior.
    """
    settings = get_settings()
    if not settings.enable_intent_classification:
        logger.info("[classify_analytical_intent] skipped (enable_intent_classification=False)")
        return {"analytical_intent": None, "status": "generating"}

    question = state["question"]
    try:
        classification = generate_analytical_intent_from_llm(
            question,
            settings,
            model=state.get("selected_model"),
            retrieved_context=state.get("retrieved_context"),
            governing_metrics=state.get("governing_metrics"),
        )
    except OllamaUnavailableError as exc:
        logger.warning(
            "[classify_analytical_intent] LLM call failed, proceeding without a "
            "classification: %s",
            exc,
        )
        return {"analytical_intent": None, "status": "generating"}

    logger.info("[classify_analytical_intent] classification=%s", classification)
    return {"analytical_intent": classification, "status": "generating"}


def _planning_gate(state: AgentState, settings) -> tuple[bool, list[str], bool]:
    """Shared trigger condition for both `build_analytical_plan_node` and
    `plan_query_node` -- a question gets *either* planning path (never
    neither-then-both) exactly when `Settings.enable_query_planning` is
    True AND at least one of `state["complexity_signals"]` is non-empty or
    `state["analytical_intent"]` implies planning (see `agent.intent
    .intent_implies_planning`). Factored out so the two nodes can never
    silently drift apart on what counts as "non-trivial" -- see Prompt 12
    (`12_ANALYTICAL_PLANNING_CONTRACT.md`).

    Returns:
        `(should_plan, complexity_signals, intent_triggers_planning)`.
    """
    complexity_signals = state.get("complexity_signals") or []
    analytical_intent = state.get("analytical_intent")
    intent_triggers_planning = analytical_intent is not None and intent_implies_planning(
        analytical_intent
    )
    should_plan = settings.enable_query_planning and bool(
        complexity_signals or intent_triggers_planning
    )
    return should_plan, complexity_signals, intent_triggers_planning


@_timed_node("build_analytical_plan")
def build_analytical_plan_node(state: AgentState) -> dict[str, Any]:
    """Produces and deterministically re-verifies a structured
    `AnalyticalPlan` for a question judged non-trivial -- Prompt 12
    (`12_ANALYTICAL_PLANNING_CONTRACT.md`).

    Runs between `classify_analytical_intent` and `plan_query` (see
    `agent/graph.py`), gated by the exact same trigger `plan_query_node`
    itself uses (`_planning_gate` above) AND `Settings
    .enable_analytical_planning` -- so a question that skips planning
    entirely today (the overwhelming common case: no complexity signal,
    no planning-implying intent) still skips this node's LLM call too,
    zero added latency, byte-identical to pre-Prompt-12 behavior.

    This is the **first tier** of a three-tier fallback:
      1. A validated structured plan (this node) -- when `agent
         .llm_client.generate_analytical_plan_from_llm` returns a parseable
         plan AND `agent.plan_validator.validate_plan` finds zero
         violations against the real retrieved schema, governed metrics,
         sensitive-column policy, caller permissions, and the target
         database's dialect capabilities.
      2. `plan_query_node`'s existing free-text plan -- runs normally
         (unmodified) whenever this node leaves `state["analytical_plan"]`
         as `None`, for *any* reason: the gate didn't fire, the feature
         flag is off, Ollama was unreachable, the response was
         unparseable, or -- unlike every other advisory node in this
         graph -- the plan was syntactically fine but failed deterministic
         validation. An invalid plan is never partially trusted; it is
         discarded in full (see `agent.plan_validator.validate_plan`'s own
         docstring for why this is still "fails open," not a security
         bypass: the real, fail-closed gates -- `validate_sql_node`'s
         restricted-column check chief among them -- are completely
         unmodified and still run regardless).
      3. Direct generation -- when neither planning node ever fires.

    On success, also sets `state["query_plan"]` (rendered from the same
    validated plan via `agent.analytical_plan.render_plan_as_steps`) so
    `review_sql_node`'s existing plan-conformance LLM review keeps working
    unmodified against a plan now known to be deterministically valid,
    rather than adding a second review node (master-contract rule 3).
    """
    settings = get_settings()
    should_plan, complexity_signals, intent_triggers_planning = _planning_gate(state, settings)
    if not settings.enable_analytical_planning or not should_plan:
        logger.info(
            "[build_analytical_plan] skipped (enable_analytical_planning=%s "
            "enable_query_planning=%s complexity_signals=%s intent_triggers_planning=%s)",
            settings.enable_analytical_planning,
            settings.enable_query_planning,
            complexity_signals,
            intent_triggers_planning,
        )
        return {"analytical_plan": None, "analytical_plan_violations": None, "status": "generating"}

    question = state["question"]
    schema_context = state.get("schema_context_text", "")
    try:
        raw_plan = generate_analytical_plan_from_llm(
            question,
            schema_context,
            settings,
            model=state.get("selected_model"),
            retrieved_context=state.get("retrieved_context"),
            analytical_intent=state.get("analytical_intent"),
            governing_metrics=state.get("governing_metrics"),
        )
    except OllamaUnavailableError as exc:
        logger.warning(
            "[build_analytical_plan] LLM call failed, falling back to free-text planning: %s", exc
        )
        return {"analytical_plan": None, "analytical_plan_violations": None, "status": "generating"}

    if raw_plan is None:
        logger.warning("[build_analytical_plan] model response was unparseable, falling back")
        return {"analytical_plan": None, "analytical_plan_violations": None, "status": "generating"}

    db_config = get_connection(settings, _selected_db_name(state))
    result = validate_plan(
        AnalyticalPlan.model_validate(raw_plan),
        state.get("schema_tables", []),
        state.get("governing_metrics"),
        state.get("caller_roles", ()),
        db_config.db_type,
    )
    if not result.is_valid:
        violations = [v.model_dump(mode="json") for v in result.violations]
        logger.warning(
            "[build_analytical_plan] candidate plan failed validation, falling back: %s",
            violations,
        )
        return {
            "analytical_plan": None,
            "analytical_plan_violations": violations,
            "status": "generating",
        }

    plan_model = AnalyticalPlan.model_validate(raw_plan)
    rendered_steps = render_plan_as_steps(plan_model)
    logger.info(
        "[build_analytical_plan] validated plan accepted: metrics=%d dimensions=%d filters=%d",
        len(plan_model.metrics),
        len(plan_model.dimensions),
        len(plan_model.filters),
    )
    return {
        "analytical_plan": raw_plan,
        "analytical_plan_violations": None,
        "query_plan": rendered_steps,
        "status": "generating",
    }


@_timed_node("plan_query")
def plan_query_node(state: AgentState) -> dict[str, Any]:
    """Produces an up-front, ordered plan for a question judged non-trivial.

    Runs between `build_analytical_plan` and `generate_sql` (see
    `agent/graph.py`), but only makes an LLM call when the shared
    `_planning_gate` fires (`Settings.enable_query_planning` is True AND
    at least one of `state["complexity_signals"]` is non-empty or
    `state["analytical_intent"]` implies planning -- see `agent.intent
    .intent_implies_planning`) **AND** `state["analytical_plan"]` is still
    `None` -- i.e. `build_analytical_plan_node` (Prompt 12,
    `12_ANALYTICAL_PLANNING_CONTRACT.md`) didn't already produce a
    validated structured plan for this exact question. This is the
    "preserve direct Text-to-SQL fallback" requirement in practice: this
    node's own logic is completely unmodified from its pre-Prompt-12
    shape, it just no longer redundantly re-plans (a second LLM call for
    the same purpose) when a *better*, deterministically-verified plan
    already exists.

    An ordinary question (the overwhelming common case) matches no
    complexity signal and no intent that implies planning, and skips the
    LLM call entirely: `state["query_plan"]` stays None (unless
    `build_analytical_plan_node` already set it from a validated plan) and
    every downstream node behaves exactly as it did before this node
    existed -- zero latency/cost impact on the common path. When
    `analytical_intent` is `None` (classification disabled, unreachable,
    or unparseable), this gate reduces to exactly `complexity_signals`
    alone -- the pre-Prompt-11 behavior, preserved by construction, not
    convention.

    Fails open on any planning failure -- an unreachable Ollama server, or a
    response `agent.llm_client._parse_plan_response` couldn't parse as a
    JSON array of strings -- logging it and proceeding with `query_plan =
    None`, never as a reason the question itself can't be answered.

    Also threads `state["retrieved_context"]` through (Prompt 07,
    `07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`) -- already populated by
    `retrieve_business_context_node`, which runs before this node (see
    `agent/graph.py`). `generate_query_plan_from_llm` only ever renders
    the `relationship`-type entries from it (real *and* candidate -- see
    `agent.llm_client._build_plan_relationship_block`); every other chunk
    type is ignored for planning specifically. `state["analytical_intent"]`
    (Prompt 11) is threaded the same way, rendered by
    `agent.llm_client._build_plan_intent_block`.
    """
    settings = get_settings()
    should_plan, complexity_signals, intent_triggers_planning = _planning_gate(state, settings)
    if state.get("analytical_plan"):
        logger.info(
            "[plan_query] skipped (a validated structured analytical_plan already exists "
            "for this question)"
        )
        return {"status": "generating"}
    if not should_plan:
        logger.info(
            "[plan_query] skipped (enable_query_planning=%s complexity_signals=%s "
            "intent_triggers_planning=%s)",
            settings.enable_query_planning,
            complexity_signals,
            intent_triggers_planning,
        )
        return {"query_plan": None, "status": "generating"}

    question = state["question"]
    schema_context = state.get("schema_context_text", "")
    try:
        plan = generate_query_plan_from_llm(
            question,
            schema_context,
            settings,
            model=state.get("selected_model"),
            retrieved_context=state.get("retrieved_context"),
            analytical_intent=state.get("analytical_intent"),
        )
    except OllamaUnavailableError as exc:
        logger.warning("[plan_query] LLM call failed, proceeding without a plan: %s", exc)
        return {"query_plan": None, "status": "generating"}

    logger.info(
        "[plan_query] complexity_signals=%s intent_triggers_planning=%s plan=%s",
        complexity_signals,
        intent_triggers_planning,
        plan,
    )
    return {"query_plan": plan, "status": "generating"}


@_timed_node("generate_sql")
def generate_sql_node(state: AgentState) -> dict[str, Any]:
    """Calls the LLM to produce a candidate SQL statement.

    On a retry (retry_count > 0), the most recent error in `error_history`
    and the previous SQL attempt are included in the prompt so the model can
    self-correct instead of repeating the same mistake.

    Every attempt -- the first one and every retry -- first checks the
    process-wide LLM-call rate limiter (`agent.rate_limit.
    get_llm_call_limiter`) *before* calling Ollama at all. This is what
    actually bounds the retry loop's contribution to LLM load: without it,
    a single question could still burn up to `max_retries + 1` calls no
    matter how tight the question-submission limit is (see `api/main.py`'s
    separate, per-client-IP check on that one). A denial here ends the run
    immediately at `status="rate_limited"` -- never retried, since retrying
    would just hit the same limiter again for no benefit.
    """
    settings = get_settings()
    question = state["question"]
    schema_context = state.get("schema_context_text", "")
    error_history = state.get("error_history", [])
    last_error = error_history[-1] if error_history else None
    last_error_category = state.get("last_error_category")
    previous_sql = state.get("sql")
    attempt_number = state.get("retry_count", 0) + 1
    # Only offered on the *first* attempt of a follow-up -- a retry within
    # the same question already has its own previous_sql/error_feedback from
    # this run, which is the more relevant reference at that point.
    followup_context = state.get("followup_resolved_against") if attempt_number == 1 else None

    rate_limit_result = get_llm_call_limiter(settings.llm_call_rate_limit_per_minute).check()
    if not rate_limit_result.allowed:
        logger.warning(
            "[generate_sql] attempt %d: LLM call rate limit exceeded, retry_after=%.1fs",
            attempt_number,
            rate_limit_result.retry_after_seconds,
        )
        log_security_event(
            "rate_limit_tripped",
            "info",
            "The process-wide LLM-call rate limiter denied a generation attempt.",
            attempt=attempt_number,
            retry_after_seconds=round(rate_limit_result.retry_after_seconds, 1),
        )
        record: AttemptRecord = {
            "attempt": attempt_number,
            "sql": None,
            "outcome": "rate_limited",
            "error": (
                f"LLM call rate limit exceeded (retry after "
                f"{rate_limit_result.retry_after_seconds:.1f}s)"
            ),
            "will_retry": False,
        }
        return {
            "status": "rate_limited",
            "rate_limit_message": LLM_CALL_LIMIT_MESSAGE,
            "attempt_history": [record],
        }

    logger.info(
        "[generate_sql] attempt=%d/%d last_error_category=%s last_error=%r followup=%s",
        attempt_number,
        _effective_max_retries(state, settings) + 1,
        last_error_category,
        last_error,
        bool(followup_context),
    )
    try:
        raw_sql = generate_sql_from_llm(
            question=question,
            schema_context=schema_context,
            previous_sql=previous_sql,
            error_feedback=last_error,
            error_category=last_error_category,
            settings=settings,
            followup_context=followup_context,
            query_plan=state.get("query_plan"),
            golden_examples=state.get("golden_examples"),
            retrieved_context=state.get("retrieved_context"),
            governing_metrics=state.get("governing_metrics"),
            analytical_plan=state.get("analytical_plan"),
            model=state.get("selected_model"),
        )
    except OffTopicQuestionError as exc:
        # Defense-in-depth backstop, not the normal path: agent.input_guard's
        # pre-filter should catch the vast majority of these before a
        # generation cycle is ever spent. Reaching here means the model
        # itself judged the question unanswerable as SQL -- routed to the
        # same "rejected" terminal state and standardized message as the
        # pre-filter, so the UI has one rejection path regardless of which
        # layer caught it. Never retried (there's no "fix" to feed back).
        logger.warning(
            "[generate_sql] attempt %d: model declined (off-topic): %s", attempt_number, exc
        )
        record = {
            "attempt": attempt_number,
            "sql": None,
            "outcome": "off_topic",
            "error": str(exc),
            "will_retry": False,
        }
        return {
            "status": "rejected",
            "rejection_reason": "off_topic",
            "rejection_message": rejection_message("off_topic", db_name=settings.db_name),
            "attempt_history": [record],
        }
    except (OllamaUnavailableError, MalformedLLMOutputError) as exc:
        logger.error("[generate_sql] attempt %d: LLM call failed: %s", attempt_number, exc)
        record = {
            "attempt": attempt_number,
            "sql": previous_sql,
            "outcome": "llm_error",
            "error": str(exc),
            "will_retry": False,
        }
        return {
            "status": "failed",
            "error_history": [f"LLM generation failed: {exc}"],
            "attempt_history": [record],
            "failure_explanation": f"The LLM call itself failed on attempt {attempt_number}: {exc}",
        }

    logger.info("[generate_sql] attempt %d: generated SQL: %s", attempt_number, raw_sql)
    return {"sql": raw_sql, "status": "reviewing"}


@_timed_node("review_sql")
def review_sql_node(state: AgentState) -> dict[str, Any]:
    """Checks the just-generated SQL against `plan_query_node`'s plan, before validation.

    Runs between `generate_sql` and `validate_sql` (see `agent/graph.py`).
    Only makes an LLM call when `state["query_plan"]` is a non-empty list --
    i.e. only for a question `plan_query_node` judged worth planning in the
    first place. Skips straight through (zero cost, `status="validating"`
    either way) whenever there's no plan to check against, which is the
    overwhelming common case -- an ordinary question behaves exactly as it
    did before this node existed.

    A "FAIL" verdict is treated exactly like any other retryable
    correctness mistake (parse_error, syntax_error, ...): the reviewer's
    critique becomes this attempt's error feedback for the next
    `generate_sql` call, sharing the same `retry_count`/`state["max_retries"]`
    budget as every other retryable failure category -- not a separate,
    unbounded critique loop layered on top. Once that budget is spent, a
    FAIL alone no longer blocks: the query proceeds to validation with the
    critique recorded as advisory (see the branch below).

    Fails open on any review failure -- an unreachable Ollama server, or a
    verdict `agent.llm_client._parse_review_response` couldn't parse as a
    clean PASS/FAIL -- treated as a pass, the same "must never be the
    reason a legitimate query can't run" philosophy as
    `estimate_query_cost_node`'s own fail-open behavior. The validator and
    execution layers immediately downstream are still the real safety net
    regardless of what this step decides.
    """
    settings = get_settings()
    query_plan = state.get("query_plan")
    sql = state.get("sql") or ""
    attempt_number = state.get("retry_count", 0) + 1

    if not query_plan:
        return {"plan_review_passed": None, "plan_review_feedback": None, "status": "validating"}

    try:
        passed, feedback = review_sql_against_plan_from_llm(
            query_plan, sql, settings, model=state.get("selected_model")
        )
    except OllamaUnavailableError as exc:
        logger.warning(
            "[review_sql] attempt %d: LLM call failed, treating as a pass: %s", attempt_number, exc
        )
        return {"plan_review_passed": True, "plan_review_feedback": None, "status": "validating"}

    if passed:
        logger.info("[review_sql] attempt %d: plan satisfied", attempt_number)
        return {"plan_review_passed": True, "plan_review_feedback": None, "status": "validating"}

    retry_count = state.get("retry_count", 0)
    max_retries = _effective_max_retries(state, settings)
    can_retry = retry_count < max_retries
    logger.warning(
        "[review_sql] attempt %d: plan not satisfied (retry %d/%d, will_retry=%s): %s",
        attempt_number,
        retry_count,
        max_retries,
        can_retry,
        feedback,
    )
    record: AttemptRecord = {
        "attempt": attempt_number,
        "sql": sql,
        "outcome": "plan_not_satisfied",
        "error": feedback,
        "will_retry": can_retry,
    }
    if not can_retry:
        # Budget exhausted on a review verdict alone. The reviewer is an LLM
        # accuracy aid and can reject a correct query (e.g. judging a bare
        # `Region` against a plan step written as `vDMPrep.Region`). Blocking
        # here turned a runnable query into "could not produce a working
        # query". The deterministic validator and read-only execution still run
        # next, so the query is carried forward with the objection recorded.
        logger.warning(
            "[review_sql] plan review budget exhausted; proceeding to validation with advisory feedback"
        )
        return {
            "plan_review_passed": False,
            "plan_review_feedback": feedback,
            "error_history": [f"Plan review (advisory, not blocking): {feedback}"],
            "attempt_history": [{**record, "outcome": "plan_not_satisfied_advisory"}],
            "status": "validating",
        }
    return {
        "plan_review_passed": False,
        "plan_review_feedback": feedback,
        "error_history": [f"Plan review: {feedback}"],
        "attempt_history": [record],
        "last_error_category": "plan_not_satisfied",
        "retry_count": retry_count + 1,
        "status": "generating",
    }


@_timed_node("review_metric_conformance")
def review_metric_conformance_node(state: AgentState) -> dict[str, Any]:
    """Checks the just-generated SQL against every governing (PUBLISHED)
    metric definition retrieved for this question -- Prompt 10
    (`10_GOVERNED_METRICS_CONTRACT.md`): the enforcement half of
    "confirmed definitions take precedence over LLM-generated
    definitions."

    Runs between `review_sql` and `validate_sql` (see `agent/graph.py`).
    Structurally identical to `review_sql_node` immediately above --
    only makes an LLM call when `state["governing_metrics"]` is
    non-empty **and** `Settings.enable_metric_conformance_review` is
    True; skips straight through (zero cost, `status="validating"`
    either way) otherwise, which is the overwhelming common case for a
    question that names no governed KPI. Independent of `query_plan`'s
    own gate -- a simple question like "what's our revenue?" has no
    `agent.complexity` signal at all, but can still have a governing
    metric to enforce.

    A "FAIL" verdict is treated exactly like `review_sql_node`'s own
    plan-conformance failure: the reviewer's critique becomes this
    attempt's error feedback for the next `generate_sql` call, sharing
    the same `retry_count`/`state["max_retries"]` budget as every other
    retryable failure category.

    Fails open on any review failure -- an unreachable Ollama server, or
    an unparseable verdict -- treated as a pass, the same philosophy
    `review_sql_node` already follows: this is an accuracy aid, not a
    deterministic security control (see `agent/llm_client.py`'s own
    "metric-conformance review" section docstring for why this is
    deliberately never folded into `agent.sql_validator`'s fail-closed
    safety checks).
    """
    settings = get_settings()
    governing_metrics = state.get("governing_metrics")
    sql = state.get("sql") or ""
    attempt_number = state.get("retry_count", 0) + 1

    if not governing_metrics or not settings.enable_metric_conformance_review:
        return {
            "metric_conformance_passed": None,
            "metric_conformance_feedback": None,
            "status": "validating",
        }

    try:
        passed, feedback = review_sql_against_metrics_from_llm(
            governing_metrics, sql, settings, model=state.get("selected_model")
        )
    except OllamaUnavailableError as exc:
        logger.warning(
            "[review_metric_conformance] attempt %d: LLM call failed, treating as a pass: %s",
            attempt_number,
            exc,
        )
        return {
            "metric_conformance_passed": True,
            "metric_conformance_feedback": None,
            "status": "validating",
        }

    if passed:
        logger.info("[review_metric_conformance] attempt %d: metrics satisfied", attempt_number)
        return {
            "metric_conformance_passed": True,
            "metric_conformance_feedback": None,
            "status": "validating",
        }

    retry_count = state.get("retry_count", 0)
    max_retries = _effective_max_retries(state, settings)
    can_retry = retry_count < max_retries
    logger.warning(
        "[review_metric_conformance] attempt %d: metric not honored (retry %d/%d, "
        "will_retry=%s): %s",
        attempt_number,
        retry_count,
        max_retries,
        can_retry,
        feedback,
    )
    record: AttemptRecord = {
        "attempt": attempt_number,
        "sql": sql,
        "outcome": "metric_definition_not_used",
        "error": feedback,
        "will_retry": can_retry,
    }
    update: dict[str, Any] = {
        "metric_conformance_passed": False,
        "metric_conformance_feedback": feedback,
        "error_history": [f"Metric conformance: {feedback}"],
        "attempt_history": [record],
        "last_error_category": "metric_definition_not_used",
        "retry_count": retry_count + 1,
        "status": "generating" if can_retry else "failed",
    }
    if not can_retry:
        update["failure_explanation"] = _give_up_explanation(
            state,
            f"Gave up after {attempt_number} attempts. Last error (metric conformance): "
            f"{feedback}",
        )
    return update


@_timed_node("validate_sql")
def validate_sql_node(state: AgentState) -> dict[str, Any]:
    """Runs the generated SQL through the allowlist validator.

    Resolves the sqlglot dialect from the database `retrieve_schema_node`
    auto-routed this question to (`state["selected_database"]`, via
    `db.connection.get_connection` + `get_sqlglot_dialect`) so validation
    actually parses the SQL the way *that* target database will -- not the
    global default connection, which may be a different engine entirely
    once more than one database is configured.

    Two different failure shapes are handled differently:
      - `result.violation_type` in `SAFETY_VIOLATION_TYPES` (the LLM
        produced a non-SELECT, a stacked query, or a table-creating
        SELECT INTO): this is a security-gate failure, not a mistake worth
        coaching the model through, so the agent fails closed immediately --
        no retry, regardless of remaining budget.
      - Anything else (empty output, a parse error): an ordinary
        correctness mistake, retried with error feedback like before, up to
        `settings.max_retries`. `retry_count` is incremented here (rather
        than in the routing function) so "retry vs. give up" is decided from
        a single place using the freshly-incremented count.
    """
    settings = get_settings()
    db_config = get_connection(settings, _selected_db_name(state))
    dialect = get_sqlglot_dialect(db_config.db_type)
    sql = state.get("sql") or ""
    attempt_number = state.get("retry_count", 0) + 1
    result = validate_sql(sql, dialect=dialect)

    if not result.is_valid:
        if result.violation_type in SAFETY_VIOLATION_TYPES:
            logger.error(
                "[validate_sql] SAFETY VIOLATION on attempt %d, failing closed (no retry): %s",
                attempt_number,
                result.error,
            )
            log_security_event(
                "sql_safety_violation",
                "warning",
                "Generated SQL failed the validator's SELECT-only allowlist -- "
                "failing closed, never retried.",
                violation_type=result.violation_type,
                attempt=attempt_number,
            )
            record: AttemptRecord = {
                "attempt": attempt_number,
                "sql": sql,
                "outcome": "safety_violation",
                "error": result.error,
                "will_retry": False,
            }
            return {
                "validation_error": result.error,
                "error_history": [f"SQL validation error (safety): {result.error}"],
                "attempt_history": [record],
                "last_error_category": "safety_violation",
                "status": "failed",
                "failure_explanation": (
                    f"Stopped after attempt {attempt_number}: the generated SQL was not a "
                    f"read-only SELECT statement ({result.error}). This is a security gate, "
                    "not a retry-able mistake, so the agent does not get another attempt."
                ),
            }

        retry_count = state.get("retry_count", 0)
        max_retries = _effective_max_retries(state, settings)
        can_retry = retry_count < max_retries
        # Propagates the validator's actual violation_type ("empty",
        # "parse_error", "nested_aggregate", ...) rather than a hardcoded
        # "parse_error" -- this is what lets generate_sql_from_llm's
        # _ERROR_CATEGORY_HINTS give a category-specific retry hint (e.g.
        # the nested-aggregate rewrite instruction) instead of only the raw
        # error text.
        violation_category = result.violation_type or "parse_error"
        logger.warning(
            "[validate_sql] rejected (attempt %d, retry %d/%d, category=%s, will_retry=%s): %s",
            attempt_number,
            retry_count,
            max_retries,
            violation_category,
            can_retry,
            result.error,
        )
        record = {
            "attempt": attempt_number,
            "sql": sql,
            "outcome": violation_category,
            "error": result.error,
            "will_retry": can_retry,
        }
        update: dict[str, Any] = {
            "validation_error": result.error,
            "error_history": [f"SQL validation error: {result.error}"],
            "attempt_history": [record],
            "last_error_category": violation_category,
            "retry_count": retry_count + 1,
            "status": "generating" if can_retry else "failed",
        }
        if not can_retry:
            update["failure_explanation"] = _give_up_explanation(
                state, f"Gave up after {attempt_number} attempts. Last error: {result.error}"
            )
        return update

    safe_sql = enforce_row_limit(
        result.normalized_sql or sql, settings.max_result_rows, dialect=dialect
    )
    logger.info("[validate_sql] accepted, row-limited SQL: %s", safe_sql)

    # Detection signal, not a new gate (see find_unexpected_table_references'
    # docstring) -- logged distinctly so a pattern of it is inspectable, but
    # never blocks or retries by itself. A table showing up here is one
    # concrete symptom a successful prompt injection via poisoned schema/
    # sampled-value content could leave behind.
    known_tables = {t["table_name"] for t in state.get("schema_tables", [])}
    anomaly_tables = find_unexpected_table_references(safe_sql, known_tables, dialect=dialect)
    if anomaly_tables:
        logger.warning(
            "[validate_sql] [schema_anomaly] SQL references table(s) never part of "
            "the retrieved schema context: %s",
            anomaly_tables,
        )

    # Enforces config/sensitive_columns.yaml's classification (see
    # config.sensitive_columns, docs/GOVERNANCE.md's "Data classification
    # policy") -- unlike the schema-anomaly check just above, this IS a new
    # gate: a "restricted" column reference is rejected here, not merely
    # logged. Treated exactly like an ordinary validation failure (retryable,
    # same budget, same error-feedback loop) rather than a
    # SAFETY_VIOLATION_TYPES failure, since the model can plausibly
    # self-correct by dropping the column and answering with what remains --
    # this is a data-governance policy, not a security-gate failure the
    # model has no way to recover from. Empty by default (the classification
    # file ships with no entries), so this has zero effect until a column is
    # deliberately classified.
    # 2026 Phase 2 security review: a caller whose role(s) grant
    # VIEW_RESTRICTED_COLUMNS (see agent/authz.py, docs/AUTHORIZATION.md)
    # skips this gate entirely -- the classification still exists and
    # still matters for everyone else, it just isn't a blanket rule
    # anymore now that per-caller identity exists. `caller_roles` defaults
    # to `()` for a caller with no elevated permissions (or a script that
    # never set it), which resolves to "no permission" the same way an
    # unrecognized role would, so this is a pure narrowing of who gets
    # blocked, never a widening.
    caller_can_view_restricted = has_role_permission(
        state.get("caller_roles", ()), Permission.VIEW_RESTRICTED_COLUMNS
    )
    classifications = load_sensitive_columns()
    restricted_pairs = {pair for pair, tier in classifications.items() if tier == "restricted"}
    if restricted_pairs and not caller_can_view_restricted:
        restricted_hits = find_restricted_column_references(
            safe_sql, restricted_pairs, known_tables, dialect=dialect
        )
        if restricted_hits:
            logger.warning(
                "[validate_sql] rejected -- references restricted column(s): %s",
                restricted_hits,
            )
            log_security_event(
                "sensitive_column_blocked",
                "warning",
                "Generated SQL directly selected a column classified 'restricted' "
                "in config/sensitive_columns.yaml.",
                columns=restricted_hits,
                attempt=attempt_number,
            )
            retry_count = state.get("retry_count", 0)
            can_retry = retry_count < _effective_max_retries(state, settings)
            restricted_record: AttemptRecord = {
                "attempt": attempt_number,
                "sql": safe_sql,
                "outcome": "restricted_column",
                "error": f"References restricted column(s): {restricted_hits}",
                "will_retry": can_retry,
            }
            restricted_update: dict[str, Any] = {
                "sql": safe_sql,
                "validation_error": f"References restricted column(s): {restricted_hits}",
                "error_history": [
                    f"SQL references restricted column(s) {restricted_hits} -- "
                    "these may never be selected; choose different columns."
                ],
                "attempt_history": [restricted_record],
                "last_error_category": "restricted_column",
                "retry_count": retry_count + 1,
                "status": "generating" if can_retry else "failed",
                "schema_anomaly_tables": anomaly_tables,
            }
            if not can_retry:
                restricted_update["failure_explanation"] = _give_up_explanation(
                    state,
                    f"Gave up after {attempt_number} attempts. The generated SQL kept "
                    f"referencing restricted column(s): {restricted_hits}.",
                )
            return restricted_update

    return {
        "sql": safe_sql,
        "validation_error": None,
        "status": "executing",
        "schema_anomaly_tables": anomaly_tables,
    }


@_timed_node("estimate_query_cost")
def estimate_query_cost_node(state: AgentState) -> dict[str, Any]:
    """Runs a non-executing EXPLAIN/SHOWPLAN estimate before the validated SQL executes.

    An earlier, additional layer in front of the existing timeout-based
    protection in `execute_sql_node` -- not a replacement for it (see
    `db.query_cost`'s module docstring). Always fails open: any estimation
    problem (unsupported dialect, timeout, driver error) is logged at debug
    and treated exactly like "low cost, proceed" -- a bug or unusual
    environment here must never be the reason a legitimate query can't run.

    Severity handling:
      - **low** (or estimation unavailable): proceeds silently, same as
        before this node existed.
      - **moderate**: proceeds, but sets `cost_notice` so the UI can show
        "this may take a moment" before/during execution.
      - **high**: does NOT execute. Treated exactly like any other
        retryable correctness mistake (parse_error, syntax_error, ...) --
        shares the same `retry_count` budget, routes back to `generate_sql`
        with the cost problem fed back as error feedback so the model can
        try a more targeted query (e.g. add a WHERE filter) on its own,
        and gives up the same way (`status="failed"`) once
        `max_retries` is exhausted.

    Estimates the query with its row cap (`TOP`/`LIMIT`) stripped first
    (`agent.sql_validator.strip_row_limit`) -- verified against a real
    accidental cross join on AdventureWorksDW2025 that a row cap makes the
    optimizer stop early and report only ~1,000 estimated rows regardless
    of the true underlying scan/join size (over 1.1 billion, unlimited),
    which would otherwise make this whole check nearly useless: every
    generated query already has a cap applied by `validate_sql_node`
    before this node ever runs. `state["sql"]` itself -- what actually
    executes next -- is never modified; the limit-stripped text exists
    only for this estimate.
    """
    settings = get_settings()
    db_config = get_connection(settings, _selected_db_name(state))
    sql = state.get("sql") or ""
    attempt_number = state.get("retry_count", 0) + 1
    dialect = get_sqlglot_dialect(db_config.db_type)

    try:
        unlimited_sql = strip_row_limit(sql, dialect=dialect)
    except TypeError:
        # Precondition violation (sql isn't SELECT-shaped) -- can't happen
        # on the normal path (validate_sql_node already guarantees this),
        # but if it ever did, fail open on the *estimate* rather than the
        # whole run: fall back to estimating the capped SQL as-is.
        unlimited_sql = sql

    estimate = estimate_query_cost(
        get_read_only_engine(db_config), unlimited_sql, db_config.db_type, settings
    )

    if estimate is None or estimate.severity == "low":
        return {"cost_estimate": estimate, "cost_notice": None, "status": "executing"}

    if estimate.severity == "moderate":
        logger.info(
            "[estimate_query_cost] moderate cost (rows=%s cost=%s plan=%r) -- "
            "proceeding with a notice",
            estimate.estimated_rows,
            estimate.estimated_cost,
            estimate.plan_summary,
        )
        return {
            "cost_estimate": estimate,
            "cost_notice": MODERATE_COST_NOTICE,
            "status": "executing",
        }

    # severity == "high"
    message = high_cost_error_message(estimate)
    logger.warning(
        "[estimate_query_cost] HIGH cost (rows=%s cost=%s plan=%r) -- not executing, "
        "feeding back as a retryable error: %s",
        estimate.estimated_rows,
        estimate.estimated_cost,
        estimate.plan_summary,
        message,
    )
    retry_count = state.get("retry_count", 0)
    can_retry = retry_count < _effective_max_retries(state, settings)
    record: AttemptRecord = {
        "attempt": attempt_number,
        "sql": sql,
        "outcome": "high_cost",
        "error": message,
        "will_retry": can_retry,
    }
    update: dict[str, Any] = {
        "cost_estimate": estimate,
        "cost_notice": None,
        "error_history": [f"Query cost estimate too high: {message}"],
        "attempt_history": [record],
        "last_error_category": "high_cost",
        "retry_count": retry_count + 1,
        "status": "generating" if can_retry else "failed",
    }
    if not can_retry:
        update["failure_explanation"] = _give_up_explanation(
            state, f"Gave up after {attempt_number} attempts. {message}"
        )
    return update


@_timed_node("execute_sql")
def execute_sql_node(state: AgentState) -> dict[str, Any]:
    """Executes validated SQL against the read-only database connection.

    Deliberately resolves and passes the read-only engine for the database
    this question was auto-routed to (`state["selected_database"]`, via
    `db.connection.get_connection` + `get_read_only_engine`) -- never a
    writable connection -- as a second layer of defense beyond
    `sql_validator`: even a validator bug can't cause a mutation against a
    connection intended to be read-only (see `db/connection.py`'s docstring
    on how that's enforced in practice: a DB-level read-only user,
    documented in README).

    A failure here is classified (`agent.error_classification.
    classify_execution_error`) and handled differently by category:
      - TIMEOUT: never retried, even with budget remaining. Retrying an
        expensive query with the same shape wastes the retry budget on
        something a retry can't fix; the agent fails immediately with a
        message suggesting a narrower question instead.
      - MISSING_REFERENCE: routes back to `retrieve_schema` (not straight to
        `generate_sql`) -- the wrong tables may have been retrieved in the
        first place, not just badly written SQL. See `retrieve_schema_node`
        for how it broadens the search on this path.
      - SYNTAX / UNKNOWN: retried via `generate_sql` with the actual driver
        error fed back, same as before -- the schema context was fine, the
        SQL text wasn't.

    A success with zero rows across a multi-table join also sets
    `low_confidence_notice` -- a detection-only signal (never a retry, never
    a gate) for the UI to show alongside an otherwise normal "succeeded"
    result. See `agent.sql_validator.references_multiple_tables`.
    """
    settings = get_settings()
    db_config = get_connection(settings, _selected_db_name(state))
    sql = state["sql"]
    if sql is None:
        # 2026 Phase 3 security review (bandit B101): explicit raise, not
        # `assert` -- stripped entirely under `python -O`, which would turn
        # this precondition violation into a confusing downstream error.
        raise RuntimeError("execute_sql_node reached with no SQL; validate_sql_node must run first")
    retry_count = state.get("retry_count", 0)
    attempt_number = retry_count + 1

    # Schema-qualifies unqualified table references for this execution only
    # -- see qualify_table_schema's docstring. `sql` itself (used below in
    # every attempt/error record, and still what `state["sql"]` holds) stays
    # exactly what the model produced and the UI shows.
    execution_sql = qualify_table_schema(
        sql, db_config.db_schema, dialect=get_sqlglot_dialect(db_config.db_type)
    )

    # Prompt 22 (scale/performance hardening): fast-fail once this
    # database's own connection pool is already fully occupied by other
    # in-flight executions, rather than blocking on `QueuePool` checkout
    # for up to SQLAlchemy's own `pool_timeout` (30s default) only to fail
    # anyway -- see `agent.rate_limit.get_database_execution_limiter`'s
    # docstring. Never retried, same reasoning as the TIMEOUT category
    # below: retrying into an already-saturated database wastes the retry
    # budget on something a retry can't fix.
    database_limiter = None
    if settings.enable_database_concurrency_limit:
        max_concurrent = (
            settings.db_pool_size
            + settings.db_max_overflow
            + settings.database_concurrency_limit_overhead
        )
        database_limiter = get_database_execution_limiter(_selected_db_name(state), max_concurrent)
        if not database_limiter.try_acquire():
            get_default_metrics().record_database_concurrency_rejection(state.get("tenant_id"))
            logger.warning(
                "[execute_sql] attempt %d rejected -- database %r execution concurrency "
                "limit reached",
                attempt_number,
                _selected_db_name(state),
            )
            busy_record: AttemptRecord = {
                "attempt": attempt_number,
                "sql": sql,
                "outcome": "database_busy",
                "error": "Database concurrency limit reached",
                "will_retry": False,
            }
            return {
                "execution_error": "Database concurrency limit reached",
                "error_history": [
                    "SQL execution error (database busy): too many concurrent queries "
                    "against this database right now"
                ],
                "attempt_history": [busy_record],
                "last_error_category": "database_busy",
                "retry_count": attempt_number,
                "status": "failed",
                "failure_explanation": (
                    "This database is already handling the maximum number of concurrent "
                    "queries this deployment allows. This isn't a problem with your "
                    "question -- please try again in a moment."
                ),
            }

    try:
        columns, rows = execute_readonly_sql(
            execution_sql,
            settings.query_timeout_seconds,
            settings.max_result_rows,
            engine=get_read_only_engine(db_config),
        )
    except (SQLAlchemyError, TimeoutError) as exc:
        category = classify_execution_error(exc)
        # Redacted once, right here, before this raw driver text touches
        # anything else -- the retry-feedback prompt and attempt_history
        # both still need the real error content to be useful (a redacted
        # password doesn't change what a syntax/missing-column error says),
        # but a driver can render the full connection string (password
        # included) verbatim on some failure modes; see
        # security/redaction.py's module docstring.
        redacted_detail = redact_secrets(str(exc), db_config)
        logger.warning(
            "[execute_sql] attempt %d failed (category=%s): %s",
            attempt_number,
            category.value,
            redacted_detail,
        )

        if category is ExecutionErrorCategory.TIMEOUT:
            record: AttemptRecord = {
                "attempt": attempt_number,
                "sql": sql,
                "outcome": "timeout",
                "error": redacted_detail,
                "will_retry": False,
            }
            return {
                "execution_error": redacted_detail,
                "error_history": [f"SQL execution error (timeout): {redacted_detail}"],
                "attempt_history": [record],
                "last_error_category": "timeout",
                "retry_count": attempt_number,
                "status": "failed",
                "failure_explanation": (
                    f"The query timed out after {settings.query_timeout_seconds}s on attempt "
                    f"{attempt_number}. This looks like an expensive query -- try narrowing your "
                    "question (a smaller date range, an added filter, fewer joined tables) rather "
                    "than retrying the same broad request."
                ),
            }

        can_retry = retry_count < _effective_max_retries(state, settings)
        outcome = (
            "missing_reference"
            if category is ExecutionErrorCategory.MISSING_REFERENCE
            else (
                "aggregate_nesting"
                if category is ExecutionErrorCategory.AGGREGATE_NESTING
                else (
                    "syntax_error" if category is ExecutionErrorCategory.SYNTAX else "unknown_error"
                )
            )
        )

        error_text = redacted_detail
        if category is ExecutionErrorCategory.MISSING_REFERENCE:
            suggestion = _suggest_correct_column(error_text, state.get("schema_tables", []))
            if suggestion:
                suggested_column, owning_table = suggestion
                error_text = (
                    f"{error_text}\nThe schema does not have that column, but table "
                    f"'{owning_table}' has a column named '{suggested_column}' -- if that's what "
                    f"you meant, use that exact name, qualified with whichever alias you already "
                    f"gave to '{owning_table}' in this query (not any other table's alias)."
                )

        record = {
            "attempt": attempt_number,
            "sql": sql,
            "outcome": outcome,
            "error": error_text,
            "will_retry": can_retry,
        }
        next_status = (
            "failed"
            if not can_retry
            else (
                "retrieving_schema"
                if category is ExecutionErrorCategory.MISSING_REFERENCE
                else "generating"
            )
        )
        update: dict[str, Any] = {
            "execution_error": redacted_detail,
            "error_history": [f"SQL execution error: {error_text}"],
            "attempt_history": [record],
            "last_error_category": category.value,
            "retry_count": attempt_number,
            "status": next_status,
        }
        if not can_retry:
            update["failure_explanation"] = _give_up_explanation(
                state, f"Gave up after {attempt_number} attempts. Last error: {error_text}"
            )
        return update
    finally:
        if database_limiter is not None:
            database_limiter.release()

    # Log shape, not content -- result sets may contain sensitive data.
    logger.info(
        "[execute_sql] attempt %d succeeded: %d row(s), %d column(s)",
        attempt_number,
        len(rows),
        len(columns),
    )
    record = {
        "attempt": attempt_number,
        "sql": sql,
        "outcome": "succeeded",
        "error": None,
        "will_retry": False,
    }

    # Detection-only, never a new gate (same philosophy as
    # schema_anomaly_tables): a legitimate zero-row answer to a
    # multi-table question is common, but this exact shape is also the
    # observable symptom of a join that matched columns from unrelated
    # surrogate-key spaces -- see agent.sql_validator.
    # references_multiple_tables and agent.llm_client._system_prompt's
    # join-correctness rules, added after a reproduced real case (a
    # subcategory table joined straight to a fact table, skipping the
    # intermediate dimension).
    low_confidence_notice = None
    if not rows and references_multiple_tables(sql, get_sqlglot_dialect(db_config.db_type)):
        low_confidence_notice = (
            "This query joined multiple tables and returned 0 rows. That can be a "
            "legitimate answer, but it's also a common symptom of a JOIN condition "
            "matching columns that aren't actually related (e.g. two different "
            "surrogate key spaces) -- double-check the SQL and the schema's "
            "declared foreign keys before trusting this as final."
        )
        logger.info(
            "[execute_sql] attempt %d succeeded with 0 rows across a multi-table join "
            "-- flagging as low-confidence (detection only, not retried)",
            attempt_number,
        )

    return {
        "result_columns": columns,
        "result_rows": rows,
        "row_count": len(rows),
        "execution_error": None,
        "attempt_history": [record],
        "status": "succeeded",
        "low_confidence_notice": low_confidence_notice,
    }


@_timed_node("compute_analytics")
def compute_analytics_node(state: AgentState) -> dict[str, Any]:
    """Computes the full deterministic statistical breakdown of a
    successfully executed query result -- Prompt 13
    (`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`).

    Only reachable from `execute_sql_node`'s success path, immediately
    before `generate_insight` (see `agent/graph.py`) -- never runs on a
    failed or needs-clarification run. Runs on **every** successful
    execution when `Settings.enable_analytics_engine` is True (the
    default) -- unlike `generate_insight_node` right after it, this has
    no `enable_insight`-style gate: a pure, deterministic, zero-I/O
    computation (no Ollama call, no extra query) has no narrative cost or
    risk to skip on.

    Delegates entirely to `analytics.engine.compute_analytics_result`
    (this node owns none of the actual statistics) and stores the result
    as a plain dict on `state["analytical_result"]`. Fails open on any
    unexpected error -- logged, `state["analytical_result"]` stays
    `None`, never a reason the question itself fails, the same posture
    every other accuracy-aid node in this graph already takes.

    **Deliberately does not feed `generate_insight_node`'s LLM prompt in
    this pass** -- see `agent.state.AgentState.analytical_result`'s own
    docstring for the full, disclosed reasoning (the live insight prompt
    is benchmark-pinned). This node's output is computed, tested, and
    surfaced (`AskResponse.analytical_result`) ahead of that separate
    wiring decision.
    """
    settings = get_settings()
    if not settings.enable_analytics_engine:
        logger.info("[compute_analytics] skipped (enable_analytics_engine=False)")
        return {"analytical_result": None}

    columns = state.get("result_columns") or []
    rows = state.get("result_rows") or []
    try:
        result = compute_analytics_result(columns, rows, settings)
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[compute_analytics] computation failed unexpectedly, proceeding without: %s", exc
        )
        return {"analytical_result": None}

    logger.info(
        "[compute_analytics] shape=%s findings=%d insufficient_data_reasons=%s",
        result.shape,
        len(result.findings),
        result.insufficient_data_reasons,
    )
    return {"analytical_result": result.model_dump(mode="json")}


def _extract_growth_series(analytical_result: dict | None) -> list[tuple[str, float]] | None:
    """Pulls the `(period, value)` series out of an already-computed
    `analytics.engine.compute_analytics_result` dict's own `GROWTH`
    finding, if it has one -- `None` when there isn't one (the result
    wasn't classified `TIME_SERIES`, `compute_analytics_node` didn't run,
    or growth itself was skipped for having fewer than 2 periods). Zero
    recomputation: this is the exact series `analytics.anomaly
    .detect_anomalies` already reuses via `analytics.engine
    ._anomaly_findings`, reused here the same way for forecasting.
    """
    if not analytical_result:
        return None
    for finding in analytical_result.get("findings", []):
        if finding.get("kind") == "growth" and finding.get("growth"):
            return [(p["period"], p["value"]) for p in finding["growth"]["points"]]
    return None


@_timed_node("generate_forecast")
def generate_forecast_node(state: AgentState) -> dict[str, Any]:
    """Forecasts future periods for a successfully executed, time-series-
    shaped query result -- Prompt 16 (`16_FORECASTING_CONTRACT.md`).

    Only reachable from `compute_analytics_node`, immediately before
    `generate_insight` (see `agent/graph.py`) -- a straight edge, never a
    conditional one, so this node can never short-circuit the graph the
    way `classify_followup_node`'s "ambiguous" classification can.

    Unlike `compute_analytics_node` right before it (which runs on *every*
    successful execution), this node only ever attempts a forecast when
    `classify_analytical_intent_node` already classified the question as
    `agent.intent.AnalyticalIntentType.FORECAST` -- no new classification,
    no keyword regex, purely reusing a judgment already made earlier in
    this same run. `Settings.enable_forecasting` off, a `None`/non-FORECAST
    `state["analytical_intent"]`, or `_extract_growth_series` finding no
    growth series to forecast from (the result wasn't a recognized time
    series) all skip the actual forecasting call -- leaving
    `state["forecast_result"]` as `None`, never a reason the question
    itself fails.

    Delegates entirely to `analytics.forecasting.generate_forecast`,
    reusing `state["analytical_result"]`'s already-computed
    `GrowthStat.points` series (zero new queries). A data-insufficient or
    untrusted-temporal-semantics series resolves to a normal, typed
    `ForecastResult(status="rejected", ...)` from that function -- not an
    exception -- which is still stored here (so the caller can see *why*
    forecasting didn't happen), not treated as the empty `None` case
    above. Fails open on any genuinely unexpected error, the same posture
    `compute_analytics_node` already establishes.

    Every forecasted value is `DataTruthLevel.AI_INFERENCE`
    (`ForecastResult.truth_level`) -- see `analytics.forecasting`'s own
    module docstring for why a deterministic, non-LLM extrapolation is
    still classified this way rather than as `DATABASE_FACT`, and never
    silently promoted further (master-contract rules 9-10).
    """
    settings = get_settings()
    if not settings.enable_forecasting:
        logger.info("[generate_forecast] skipped (enable_forecasting=False)")
        return {"forecast_result": None}

    analytical_intent = state.get("analytical_intent")
    if (
        not analytical_intent
        or AnalyticalIntentType(analytical_intent["intent"]) != AnalyticalIntentType.FORECAST
    ):
        return {"forecast_result": None}

    series = _extract_growth_series(state.get("analytical_result"))
    if series is None:
        logger.info(
            "[generate_forecast] FORECAST intent classified, but no time-series growth "
            "data is available for this result -- skipping"
        )
        return {"forecast_result": None}

    try:
        result = generate_forecast(series, horizon=state.get("forecast_horizon"), settings=settings)
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[generate_forecast] computation failed unexpectedly, proceeding without: %s", exc
        )
        return {"forecast_result": None}

    logger.info(
        "[generate_forecast] status=%s model=%s horizon=%d",
        result.status,
        result.model.model.value if result.model else None,
        result.horizon,
    )
    return {"forecast_result": result.model_dump(mode="json")}


def _restricted_column_hits_in_sql(
    sql: str, state: AgentState, settings: Any
) -> tuple[tuple[str, str], ...]:
    """Re-detects which already-classified-restricted (table, column)
    pairs the final, executed SQL references -- reuses `agent
    .sql_validator.find_restricted_column_references` directly (the exact
    function `validate_sql_node` already applies pre-execution) rather
    than a second detection implementation. Unlike that node's own gate
    (which only runs this check for a caller who *lacks*
    `Permission.VIEW_RESTRICTED_COLUMNS`, since that's the only case where
    a hit matters for *blocking execution*), this always runs it: a
    restricted column reached a successful execution only because the
    executing caller *did* have permission, but `recommendation.engine`'s
    own SECURITY rule exists precisely to surface that fact for review --
    see that module's own docstring. Fails open (returns `()`) on any
    parse/lookup error, the same posture every other accuracy-aid
    computation in this node already takes.
    """
    try:
        classifications = load_sensitive_columns()
        restricted_pairs = {pair for pair, tier in classifications.items() if tier == "restricted"}
        if not restricted_pairs:
            return ()
        db_config = get_connection(settings, state.get("selected_database") or "default")
        dialect = get_sqlglot_dialect(db_config.db_type)
        known_tables = {t["table_name"] for t in state.get("schema_tables", [])}
        hits = find_restricted_column_references(
            sql, restricted_pairs, known_tables, dialect=dialect
        )
        return tuple(hits)
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[generate_recommendations] restricted-column re-detection failed, proceeding "
            "without: %s",
            exc,
        )
        return ()


def _query_store_findings_for_state(state: AgentState, settings: Any) -> QueryStoreFindings | None:
    """SQL Server Query Store evidence for whichever database this
    question was routed to -- Prompt 19
    (`19_QUERY_STORE_PERFORMANCE_CONTRACT.md`). A structural no-op for
    every other `DB_TYPE`: the engine/connection lookup still happens
    (cheap -- no new connection is opened), but `db.query_store` itself
    short-circuits on `db_type != "mssql"` before ever touching the
    database (see that module's own docstring) -- a non-MSSQL deployment
    never pays for, or risks, a Query Store DMV query at all.

    Uses `db.query_store.get_cached_query_store_findings` (not the bare
    `get_query_store_findings`) so the live `/ask` pipeline's added cost
    is "usually free," bounded by `Settings
    .query_store_refresh_interval_seconds` -- Query Store reflects
    server-wide historical activity, not anything specific to this one
    question, so re-querying its DMVs on every single request would be
    pure waste. Fails open (returns `None`) on any error, the same
    posture every other accuracy-aid computation in this node takes.
    """
    if not settings.enable_query_store_insights:
        return None
    try:
        db_name = state.get("selected_database") or "default"
        db_config = get_connection(settings, db_name)
        if db_config.db_type != "mssql":
            return None
        engine = get_read_only_engine(db_config)
        return get_cached_query_store_findings(engine, db_config.db_type, db_name, settings)
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[generate_recommendations] Query Store lookup failed, proceeding without: %s",
            exc,
        )
        return None


@_timed_node("generate_recommendations")
def generate_recommendations_node(state: AgentState) -> dict[str, Any]:
    """Builds evidence-backed recommendations for a successfully executed
    query result -- Prompt 17 (`17_RECOMMENDATION_ENGINE_CONTRACT.md`).

    Only reachable from `generate_forecast_node`, immediately before
    `generate_insight` (see `agent/graph.py`) -- a straight edge, never a
    conditional one, so this node can never short-circuit the graph.

    Delegates entirely to `recommendation.engine.generate_recommendations`
    (this node owns none of the actual rules). Feeds it whichever typed
    evidence this request already has in hand, zero new queries:
    `state["analytical_result"]` (reconstructed into the typed
    `AnalyticsResult` `recommendation.engine` expects), `state
    ["cost_estimate"]` (already the typed `db.query_cost.CostEstimate`
    object, see `AgentState.cost_estimate`'s own docstring), a live
    `observability.metrics.PerformanceMetrics.snapshot()` (an existing,
    cheap, in-process rollup -- no new instrumentation), and any
    restricted-column hit in the final executed SQL (see
    `_restricted_column_hits_in_sql` above). `root_cause_result` is never
    supplied here -- this single-query pipeline never produces the
    second, comparison dataset `analytics.root_cause
    .investigate_root_cause` needs (see that module's own disclosed gap,
    inherited rather than worked around); the OPERATIONS category
    therefore never fires from this live node, only from a direct,
    standalone call to `recommendation.engine.generate_recommendations`.

    `inputs.caller_roles` is this same run's `state["caller_roles"]` --
    the authorization check this enables is provably redundant with
    `validate_sql_node`'s own pre-execution gate *in this one place*
    (a restricted column could only have reached a successful execution
    because this exact caller already had permission), but is not
    redundant for any other caller of the engine; see `recommendation
    .engine`'s own module docstring for the full reasoning.

    Fails open on any unexpected error -- logged,
    `state["recommendations"]` stays `[]`, never a reason the question
    itself fails, the same posture every other accuracy-aid node in this
    graph already takes.
    """
    settings = get_settings()
    if not settings.enable_recommendation_engine:
        logger.info("[generate_recommendations] skipped (enable_recommendation_engine=False)")
        return {"recommendations": []}

    analytical_result_dict = state.get("analytical_result")
    try:
        analytics_result = (
            AnalyticsResult(**analytical_result_dict) if analytical_result_dict else None
        )
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[generate_recommendations] could not reconstruct AnalyticsResult, proceeding "
            "without it: %s",
            exc,
        )
        analytics_result = None

    sql = state.get("sql") or ""
    restricted_hits = _restricted_column_hits_in_sql(sql, state, settings) if sql else ()

    try:
        # Prompt 20: only this tenant's own performance window may inform a
        # recommendation shown to this tenant -- a PERFORMANCE finding
        # derived from another tenant's latency would both be wrong and leak
        # their operational data.
        performance_snapshot = get_default_metrics().snapshot(state.get("tenant_id"))
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[generate_recommendations] could not read performance snapshot, proceeding "
            "without it: %s",
            exc,
        )
        performance_snapshot = None

    query_store_findings = _query_store_findings_for_state(state, settings)

    inputs = RecommendationInputs(
        analytics_result=analytics_result,
        root_cause_result=None,
        cost_estimate=state.get("cost_estimate"),
        query_store_findings=query_store_findings,
        performance_snapshot=performance_snapshot,
        restricted_column_hits=restricted_hits,
        caller_roles=tuple(state.get("caller_roles", ())),
    )

    try:
        recommendations = generate_recommendations(inputs, settings)
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[generate_recommendations] computation failed unexpectedly, proceeding without: %s",
            exc,
        )
        return {"recommendations": []}

    logger.info(
        "[generate_recommendations] produced %d recommendation(s)",
        len(recommendations),
    )
    return {"recommendations": [rec.model_dump(mode="json") for rec in recommendations]}


@_timed_node("generate_insight")
def generate_insight_node(state: AgentState) -> dict[str, Any]:
    """Generates a short, plain-English insight for a successfully executed query.

    Only reachable from `execute_sql_node`'s success path (see
    `agent/graph.py`) -- never runs on a failed or needs-clarification run.
    Purely a narrative layer on top of already-correct results: nothing here
    can change `state["sql"]` / `state["result_rows"]` / `state["row_count"]`,
    and every early-return below leaves `state["insight"]` as None rather
    than showing something unreliable.

    Skips the LLM call entirely (no cost) when:
      - `enable_insight` is False (the UI's toggle, off).
      - The result is empty or a single-row/single-column value (see
        `agent.insight.should_skip_insight`) -- e.g. "how many customers are
        there?" already has its full answer in the one cell; a sentence
        restating it adds nothing.

    After a real LLM call, the response is graded against
    `agent.insight.is_insight_grounded` before it's ever stored -- an
    insight that mentions a number not supported by the result summary (or
    a literal value from the question/SQL) is logged and dropped rather than
    shown, since a wrong "AI interpretation" is worse than none at all.
    """
    if not state.get("enable_insight", True):
        logger.info("[generate_insight] disabled via enable_insight -- skipping")
        return {"insight": None, "insight_summary": None}

    columns = state.get("result_columns") or []
    rows = state.get("result_rows") or []
    if should_skip_insight(columns, rows):
        logger.info(
            "[generate_insight] skipped -- result has no value beyond the raw "
            "row(s) (rows=%d cols=%d)",
            len(rows),
            len(columns),
        )
        return {"insight": None, "insight_summary": None}

    settings = get_settings()
    summary = summarize_result(columns, rows)
    question = state["question"]
    sql = state.get("sql") or ""

    try:
        insight_text = generate_insight_from_llm(
            question=question,
            sql=sql,
            summary=summary,
            settings=settings,
            model=state.get("selected_model"),
        )
    except OllamaUnavailableError as exc:
        logger.warning("[generate_insight] LLM call failed, omitting insight: %s", exc)
        return {"insight": None, "insight_summary": summary}

    if insight_text is None:
        logger.info("[generate_insight] model declined to produce an insight")
        return {"insight": None, "insight_summary": summary}

    if not is_insight_grounded(insight_text, summary, question=question, sql=sql):
        logger.warning(
            "[generate_insight] dropped ungrounded insight (contains a number not "
            "supported by the result): %r",
            insight_text,
        )
        return {"insight": None, "insight_summary": summary}

    logger.info("[generate_insight] generated: %r", insight_text)
    return {"insight": insight_text, "insight_summary": summary}


def route_after_sanitization(state: AgentState) -> str:
    """Conditional edge after sanitize_input: proceed, or stop and reject."""
    if state.get("status") == "rejected":
        return "rejected"
    return "classify_followup"


def route_after_classification(state: AgentState) -> str:
    """Conditional edge after classify_followup: proceed, or stop and ask."""
    if state.get("status") == "needs_clarification":
        return "needs_clarification"
    return "retrieve_schema"


def route_after_generation(state: AgentState) -> str:
    """Conditional edge after generate_sql: review, or stop (rejected/failed/rate_limited).

    Every terminal status generate_sql_node can set without ever producing
    SQL -- "rejected" (the `OffTopicQuestionError` backstop), "failed" (an
    `OllamaUnavailableError`/`MalformedLLMOutputError`, i.e. the LLM call
    itself never returned usable text), and "rate_limited" (the process-
    wide LLM-call limiter denied this attempt -- see `agent.rate_limit`) --
    routes straight to END here rather than falling through to
    `review_sql_node`/`validate_sql_node`. Letting any of these fall through
    would have those nodes operate on `state["sql"]` (unset on all three
    paths) -- silently overwriting the real status and burning the retry
    budget (or, for rate_limited specifically, immediately re-tripping the
    same limiter) on a problem retrying can't fix. Only the ordinary success
    path (`status="reviewing"`) proceeds to `review_sql`.
    """
    status = state.get("status")
    if status == "rejected":
        return "rejected"
    if status == "failed":
        return "failed"
    if status == "rate_limited":
        return "rate_limited"
    return "review_sql"


def route_after_review(state: AgentState) -> str:
    """Conditional edge after review_sql: metric conformance, retry, or give up.

    Mirrors `route_after_validation`'s shape: "validating" (review passed,
    or was skipped entirely because `state["query_plan"]` was empty/None)
    proceeds to `review_metric_conformance` (Prompt 10,
    `10_GOVERNED_METRICS_CONTRACT.md` -- itself a pure pass-through to
    `validate_sql` when no governing metric applies, so an ordinary
    question's path is functionally unchanged); "failed" (a FAIL verdict
    with the retry budget exhausted) ends the run; anything else (a FAIL
    verdict with retries remaining) loops back to `generate_sql` with the
    reviewer's critique now in `error_history`.
    """
    status = state.get("status")
    if status == "validating":
        return "review_metric_conformance"
    if status == "failed":
        return "failed"
    return "generate_sql"


def route_after_metric_conformance(state: AgentState) -> str:
    """Conditional edge after review_metric_conformance: validate, retry, or give up.

    Mirrors `route_after_review`'s shape exactly -- "validating" (check
    passed, or was skipped because `state["governing_metrics"]` was empty
    or the feature is disabled) proceeds to `validate_sql`; "failed" (a
    FAIL verdict with the retry budget exhausted) ends the run; anything
    else (a FAIL verdict with retries remaining) loops back to
    `generate_sql` with the reviewer's critique now in `error_history`.
    """
    status = state.get("status")
    if status == "validating":
        return "validate_sql"
    if status == "failed":
        return "failed"
    return "generate_sql"


def route_after_validation(state: AgentState) -> str:
    """Conditional edge after validate_sql: estimate cost, retry, or give up."""
    status = state.get("status")
    if status == "executing":
        return "estimate_cost"
    if status == "failed":
        return "failed"
    return "generate_sql"


def route_after_cost_estimate(state: AgentState) -> str:
    """Conditional edge after estimate_query_cost: execute, retry, or give up.

    A "high" severity estimate never reaches `execute_sql` -- see
    `estimate_query_cost_node`, which sets status="generating" (retry,
    shares the normal retry budget) or status="failed" (budget exhausted)
    for that case, same as `status="executing"` means "low/moderate cost,
    proceed" here.
    """
    status = state.get("status")
    if status == "executing":
        return "execute_sql"
    if status == "failed":
        return "failed"
    return "generate_sql"


def route_after_execution(state: AgentState) -> str:
    """Conditional edge after execute_sql: succeed, retry, re-retrieve schema, or give up."""
    status = state.get("status")
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "retrieving_schema":
        return "retrieve_schema"
    return "generate_sql"
