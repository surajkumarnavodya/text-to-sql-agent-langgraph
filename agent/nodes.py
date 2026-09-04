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
import logging
import re
import time
from collections.abc import Callable
from typing import Any

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
    find_restricted_column_references,
    find_unexpected_table_references,
    references_multiple_tables,
    strip_row_limit,
    validate_sql,
)
from agent.state import AgentState, AttemptRecord, ConversationExchange, StageTiming, TableSchema
from config.sensitive_columns import load_sensitive_columns
from config.settings import get_settings
from config.table_descriptions import apply_table_description, load_table_descriptions
from db.connection import get_connection, get_read_only_engine, get_sqlglot_dialect
from db.execution import execute_readonly_sql
from db.query_cost import MODERATE_COST_NOTICE, estimate_query_cost, high_cost_error_message
from embeddings.retriever import retrieve_relevant_schema, select_database
from security.audit_log import log_security_event
from security.injection_patterns import INJECTION_PATTERNS

# Re-exported for `tests/test_agent_nodes.py`'s
# `monkeypatch.setattr("agent.nodes.execute_readonly_sql", ...)` seam -- the
# actual implementation (and its own tests) lives in `db/execution.py`, a
# pure database-execution concern with no LangGraph/state dependency. Kept as
# a plain re-import (not re-implemented) so there is exactly one definition.
__all__ = ["execute_readonly_sql"]

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


def _selected_db_name(state: AgentState) -> str:
    """Returns `state["selected_database"]`, guaranteed non-None by this point.

    `retrieve_schema_node` always runs (and sets this) before
    `validate_sql`/`estimate_query_cost`/`execute_sql` -- see `agent/graph.py`
    -- so a None here is a precondition violation, not a real runtime case.
    """
    selected_database = state["selected_database"]
    assert (
        selected_database is not None
    ), "reached with no selected_database; retrieve_schema_node must run first"
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


def _extract_column_names(ddl: str) -> list[str]:
    """Pulls column names out of one table's synthesized DDL text.

    Relies only on `db.schema_introspection.render_ddl`'s consistent
    one-column-per-line rendering (`    ColumnName TYPE ...,`) -- takes the
    first whitespace-separated token of each body line, skipping the
    `CREATE TABLE`/closing-paren lines and `FOREIGN KEY (...)`/
    `PRIMARY KEY (...)` table-level constraint lines (which would otherwise
    contribute "FOREIGN"/"PRIMARY" as false column names).
    """
    names = []
    for line in ddl.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or stripped.startswith(("CREATE TABLE", "FOREIGN KEY", "PRIMARY KEY", ")")):
            continue
        first_token = stripped.split(None, 1)[0]
        if first_token.isidentifier():
            names.append(first_token)
    return names


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

    if not result.passed:
        assert result.reason is not None  # guaranteed when passed is False
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
            db_selection = select_database(query_text, settings)
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
    classifications = load_sensitive_columns()
    restricted_pairs = {pair for pair, tier in classifications.items() if tier == "restricted"}
    if restricted_pairs:
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
            can_retry = retry_count < settings.max_retries
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
    assert sql is not None, "execute_sql_node reached with no SQL; validate_sql_node must run first"
    retry_count = state.get("retry_count", 0)
    attempt_number = retry_count + 1

    try:
        columns, rows = execute_readonly_sql(
            sql,
            settings.query_timeout_seconds,
            settings.max_result_rows,
            engine=get_read_only_engine(db_config),
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

        error_text = str(exc)
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
            "execution_error": str(exc),
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
