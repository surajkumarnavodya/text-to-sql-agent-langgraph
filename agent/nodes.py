"""LangGraph node functions for the self-correcting Text-to-SQL agent.

Each function takes the current `AgentState` and returns a partial dict of
updates (LangGraph's convention) -- this is what makes the state
transitions "visible": every node's contract is exactly its input and
output state, independent of graph wiring (see `agent/graph.py`).

Flow: retrieve_schema -> generate_sql -> validate_sql -> execute_sql, with
validate_sql/execute_sql routing back to generate_sql on failure (see
`route_after_validation` / `route_after_execution`), capped at
`Settings.max_retries`.
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
from agent.exceptions import MalformedLLMOutputError, OllamaUnavailableError, SchemaRetrievalError
from agent.llm_client import generate_sql_from_llm
from agent.sql_validator import SAFETY_VIOLATION_TYPES, enforce_row_limit, validate_sql
from agent.state import AgentState, AttemptRecord, StageTiming, TableSchema
from config.settings import get_settings
from config.table_descriptions import apply_table_description, load_table_descriptions
from db.connection import get_read_only_engine, get_sqlglot_dialect
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
    """
    settings = get_settings()
    question = state["question"]
    schema_context = state.get("schema_context_text", "")
    error_history = state.get("error_history", [])
    last_error = error_history[-1] if error_history else None
    last_error_category = state.get("last_error_category")
    previous_sql = state.get("sql")
    attempt_number = state.get("retry_count", 0) + 1

    logger.info(
        "[generate_sql] attempt=%d/%d last_error_category=%s last_error=%r",
        attempt_number,
        settings.max_retries + 1,
        last_error_category,
        last_error,
    )
    try:
        raw_sql = generate_sql_from_llm(
            question=question,
            schema_context=schema_context,
            previous_sql=previous_sql,
            error_feedback=last_error,
            error_category=last_error_category,
            settings=settings,
        )
    except (OllamaUnavailableError, MalformedLLMOutputError) as exc:
        logger.error("[generate_sql] attempt %d: LLM call failed: %s", attempt_number, exc)
        record: AttemptRecord = {
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
            update["failure_explanation"] = (
                f"Gave up after {attempt_number} attempts. Last error: {result.error}"
            )
        return update

    safe_sql = enforce_row_limit(
        result.normalized_sql or sql, settings.max_result_rows, dialect=dialect
    )
    logger.info("[validate_sql] accepted, row-limited SQL: %s", safe_sql)
    return {"sql": safe_sql, "validation_error": None, "status": "executing"}


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
            update["failure_explanation"] = (
                f"Gave up after {attempt_number} attempts. Last error: {exc}"
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


def route_after_validation(state: AgentState) -> str:
    """Conditional edge after validate_sql: execute, retry, or give up."""
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
