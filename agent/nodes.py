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

import contextlib
import functools
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from agent.error_classification import ExecutionErrorCategory, classify_execution_error
from agent.exceptions import (
    MalformedLLMOutputError,
    OffTopicQuestionError,
    OllamaUnavailableError,
    SchemaRetrievalError,
)
from agent.followup import classify_followup
from agent.input_guard import NO_RELEVANT_DATA_MESSAGE, check_input, rejection_message
from agent.insight import is_insight_grounded, should_skip_insight, summarize_result
from agent.llm_client import generate_insight_from_llm, generate_sql_from_llm
from agent.rate_limit import LLM_CALL_LIMIT_MESSAGE, get_llm_call_limiter
from agent.sql_validator import (
    SAFETY_VIOLATION_TYPES,
    enforce_row_limit,
    find_unexpected_table_references,
    strip_row_limit,
    validate_sql,
)
from agent.state import AgentState, AttemptRecord, ConversationExchange, StageTiming, TableSchema
from config.settings import get_settings
from config.table_descriptions import apply_table_description, load_table_descriptions
from db.connection import get_read_only_engine, get_sqlglot_dialect
from db.query_cost import MODERATE_COST_NOTICE, estimate_query_cost, high_cost_error_message
from embeddings.retriever import retrieve_relevant_schema

logger = logging.getLogger(__name__)


def _timed_node(stage: str) -> Callable[[Callable[[AgentState], dict[str, Any]]], Callable]:
    """Records a node's wall-clock duration into `stage_timings`, uniformly.

    Applied as a decorator rather than hand-timing each node body: every
    node here has several early-return branches for different outcomes
    (safety violation, retryable failure, success, ...), and this measures
    the call the same way regardless of which branch it took, without the
    node's own logic needing to know timing exists. See
    `scripts/profile_pipeline.py` for how this data gets turned into a
    stage-by-stage breakdown.
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


# DB_TYPE -> a one-line SET statement that caps execution time at the
# database itself, for engines where that's a cheap, well-known statement.
# Applied best-effort right after opening the connection, before running the
# actual query -- this is the "enforced at the driver level" half of the
# timeout story (see `_execute_with_timeout` for the other half, which
# covers every dialect including the two without a simple SET form below).
_STATEMENT_TIMEOUT_SQL: dict[str, str] = {
    "postgresql": "SET statement_timeout = {timeout_ms}",
    "mysql": "SET SESSION MAX_EXECUTION_TIME = {timeout_ms}",
}


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

    if not result.passed:
        assert result.reason is not None  # guaranteed when passed is False
        message = rejection_message(result.reason, db_name=settings.db_name)
        logger.info(
            "[sanitize_input] rejected reason=%s -- see agent.input_guard logs for detail",
            result.reason,
        )
        return {
            "status": "rejected",
            "rejection_reason": result.reason,
            "rejection_message": message,
        }

    return {"question": result.cleaned_question, "status": "classifying_followup"}


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
        tables = retrieve_relevant_schema(query_text, top_k=top_k)
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
    return {
        "schema_tables": tables,
        "schema_context_text": context_text,
        "status": "generating",
    }


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
    matter how tight the question-submission limit is (see `ui/app.py`'s
    separate, per-session check on that one). A denial here ends the run
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
        settings.max_retries + 1,
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
    return {"sql": raw_sql, "status": "validating"}


@_timed_node("validate_sql")
def validate_sql_node(state: AgentState) -> dict[str, Any]:
    """Runs the generated SQL through the allowlist validator.

    Resolves the sqlglot dialect from `Settings.db_type` (via
    `db.connection.get_sqlglot_dialect`) so validation actually parses the
    SQL the way the target database will -- this is not optional now that
    the target is a real, configurable engine rather than always DuckDB.

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
    dialect = get_sqlglot_dialect(settings.db_type)
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
        can_retry = retry_count < settings.max_retries
        logger.warning(
            "[validate_sql] rejected (attempt %d, retry %d/%d, will_retry=%s): %s",
            attempt_number,
            retry_count,
            settings.max_retries,
            can_retry,
            result.error,
        )
        record = {
            "attempt": attempt_number,
            "sql": sql,
            "outcome": "parse_error",
            "error": result.error,
            "will_retry": can_retry,
        }
        update: dict[str, Any] = {
            "validation_error": result.error,
            "error_history": [f"SQL validation error: {result.error}"],
            "attempt_history": [record],
            "last_error_category": "parse_error",
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
    sql = state.get("sql") or ""
    attempt_number = state.get("retry_count", 0) + 1
    dialect = get_sqlglot_dialect(settings.db_type)

    try:
        unlimited_sql = strip_row_limit(sql, dialect=dialect)
    except TypeError:
        # Precondition violation (sql isn't SELECT-shaped) -- can't happen
        # on the normal path (validate_sql_node already guarantees this),
        # but if it ever did, fail open on the *estimate* rather than the
        # whole run: fall back to estimating the capped SQL as-is.
        unlimited_sql = sql

    estimate = estimate_query_cost(get_read_only_engine(), unlimited_sql, settings)

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
    can_retry = retry_count < settings.max_retries
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


def _apply_statement_timeout(connection, dialect_name: str, timeout_seconds: int) -> None:
    """Best-effort driver-level statement timeout, where a simple SET exists.

    Not every engine has a one-line session-level timeout (MSSQL/Oracle
    don't), which is why this is "best effort" and paired with the
    thread-based cancellation fallback in `_execute_with_timeout` that
    covers every dialect uniformly.
    """
    template = _STATEMENT_TIMEOUT_SQL.get(dialect_name)
    if not template:
        return
    timeout_ms = int(timeout_seconds * 1000)
    try:
        connection.execute(text(template.format(timeout_ms=timeout_ms)))
    except SQLAlchemyError as exc:
        # Non-fatal: the thread-based fallback below still enforces the
        # wall-clock cutoff even if this driver-level SET isn't permitted
        # for this user/role.
        logger.debug("[execute_sql] could not set driver-level statement timeout: %s", exc)


def _execute_with_timeout(
    sql: str, query_timeout_seconds: int, max_result_rows: int
) -> tuple[list[str], list[tuple]]:
    """Runs `sql` on a worker thread and force-aborts it past `query_timeout_seconds`.

    SQLAlchemy has no universal, cross-dialect "cancel this query" call, so
    the fallback that works for every one of the four supported engines is:
    run the query on a background thread, and if it hasn't finished by the
    deadline, close the underlying connection from the *calling* thread.
    Closing the socket out from under an in-flight query forces the
    database server to notice and kill it -- a real, driver-level
    cancellation, not just "stop waiting for the response" on our side.

    Row cap is enforced with `fetchmany(max_result_rows)` rather than
    `fetchall()` -- a defense-in-depth measure independent of the `LIMIT`
    clause already added by `agent.sql_validator.enforce_row_limit`, so a
    malformed or dialect-mistranslated query that ignores/lacks a LIMIT
    still can't pull an unbounded result set into memory.
    """
    engine = get_read_only_engine()
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}
    connection_holder: dict[str, Any] = {}

    def _run() -> None:
        try:
            with engine.connect() as connection:
                connection_holder["connection"] = connection
                _apply_statement_timeout(connection, engine.dialect.name, query_timeout_seconds)
                cursor_result = connection.execute(text(sql))
                result["columns"] = list(cursor_result.keys())
                result["rows"] = cursor_result.fetchmany(max_result_rows)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error["error"] = exc
        finally:
            connection_holder.pop("connection", None)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(query_timeout_seconds)

    if worker.is_alive():
        connection = connection_holder.get("connection")
        if connection is not None:
            # Best-effort abort; must not mask the TimeoutError raised below.
            with contextlib.suppress(Exception):
                connection.close()
        worker.join(query_timeout_seconds)
        raise TimeoutError(f"Query exceeded the {query_timeout_seconds}s timeout and was aborted.")

    if "error" in error:
        raise error["error"]

    return result["columns"], result["rows"]


def execute_readonly_sql(
    sql: str, query_timeout_seconds: int, max_result_rows: int | None = None
) -> tuple[list[str], list[tuple]]:
    """Executes already-validated, already row-limited SQL read-only.

    Shared by `execute_sql_node` (the agent's internal self-correction loop)
    and `ui/app.py`'s manual "Confirm and Run" path, so both go through the
    exact same connection reuse, timeout, and read-only enforcement -- the
    UI does not get its own, separately-maintained execution logic.

    Returns:
        (columns, rows).

    Raises:
        SQLAlchemyError: on a SQL execution error (e.g. unknown column).
        TimeoutError: if execution exceeds `query_timeout_seconds`.
    """
    resolved_max_rows = (
        max_result_rows if max_result_rows is not None else get_settings().max_result_rows
    )
    return _execute_with_timeout(sql, query_timeout_seconds, resolved_max_rows)


@_timed_node("execute_sql")
def execute_sql_node(state: AgentState) -> dict[str, Any]:
    """Executes validated SQL against the read-only database connection.

    Deliberately uses `get_read_only_engine()` -- never a writable
    connection -- as a second layer of defense beyond `sql_validator`: even
    a validator bug can't cause a mutation against a connection intended to
    be read-only (see `db/connection.py`'s docstring on how that's enforced
    in practice: a DB-level read-only user, documented in README).

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
    """
    settings = get_settings()
    sql = state["sql"]
    assert sql is not None, "execute_sql_node reached with no SQL; validate_sql_node must run first"
    retry_count = state.get("retry_count", 0)
    attempt_number = retry_count + 1

    try:
        columns, rows = execute_readonly_sql(
            sql, settings.query_timeout_seconds, settings.max_result_rows
        )
    except (SQLAlchemyError, TimeoutError) as exc:
        category = classify_execution_error(exc)
        logger.warning(
            "[execute_sql] attempt %d failed (category=%s): %s",
            attempt_number,
            category.value,
            exc,
        )

        if category is ExecutionErrorCategory.TIMEOUT:
            record: AttemptRecord = {
                "attempt": attempt_number,
                "sql": sql,
                "outcome": "timeout",
                "error": str(exc),
                "will_retry": False,
            }
            return {
                "execution_error": str(exc),
                "error_history": [f"SQL execution error (timeout): {exc}"],
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

        can_retry = retry_count < settings.max_retries
        outcome = (
            "missing_reference"
            if category is ExecutionErrorCategory.MISSING_REFERENCE
            else "syntax_error" if category is ExecutionErrorCategory.SYNTAX else "unknown_error"
        )
        record = {
            "attempt": attempt_number,
            "sql": sql,
            "outcome": outcome,
            "error": str(exc),
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
            "execution_error": str(exc),
            "error_history": [f"SQL execution error: {exc}"],
            "attempt_history": [record],
            "last_error_category": category.value,
            "retry_count": attempt_number,
            "status": next_status,
        }
        if not can_retry:
            update["failure_explanation"] = _give_up_explanation(
                state, f"Gave up after {attempt_number} attempts. Last error: {exc}"
            )
        return update

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
    return {
        "result_columns": columns,
        "result_rows": rows,
        "row_count": len(rows),
        "execution_error": None,
        "attempt_history": [record],
        "status": "succeeded",
    }


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
            question=question, sql=sql, summary=summary, settings=settings
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
    """Conditional edge after generate_sql: validate, or stop (rejected/failed/rate_limited).

    Every terminal status generate_sql_node can set without ever producing
    SQL -- "rejected" (the `OffTopicQuestionError` backstop), "failed" (an
    `OllamaUnavailableError`/`MalformedLLMOutputError`, i.e. the LLM call
    itself never returned usable text), and "rate_limited" (the process-
    wide LLM-call limiter denied this attempt -- see `agent.rate_limit`) --
    routes straight to END here rather than falling through to
    `validate_sql_node`. Letting any of these fall through would have
    `validate_sql_node` re-validate `state["sql"]` (unset on all three
    paths), see an "empty SQL" parse error, and retry -- silently
    overwriting the real status and burning the retry budget (or, for
    rate_limited specifically, immediately re-tripping the same limiter) on
    a problem retrying can't fix. Only the ordinary success path
    (`status="validating"`) proceeds to validation.
    """
    status = state.get("status")
    if status == "rejected":
        return "rejected"
    if status == "failed":
        return "failed"
    if status == "rate_limited":
        return "rate_limited"
    return "validate_sql"


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
