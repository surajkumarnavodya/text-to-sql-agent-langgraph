"""Wraps calls to the local Ollama server for SQL generation.

Kept separate from `nodes.py` so the LangGraph node stays focused on state
transitions while this module owns prompt construction and the raw
Ollama call -- and so it can be unit-tested / mocked independently of the
graph.
"""

from __future__ import annotations

import logging
import re
import time

import ollama

from agent.exceptions import MalformedLLMOutputError, OllamaUnavailableError
from config.settings import Settings

logger = logging.getLogger(__name__)

# DB_TYPE -> a human-readable name for the prompt, so the model writes SQL in
# the right flavor (e.g. TOP/OFFSET-FETCH for SQL Server vs LIMIT for
# Postgres/MySQL) instead of assuming one specific engine.
_DB_TYPE_DISPLAY_NAMES: dict[str, str] = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mssql": "Microsoft SQL Server (T-SQL)",
    "oracle": "Oracle Database",
}


def _system_prompt(db_type: str) -> str:
    engine_name = _DB_TYPE_DISPLAY_NAMES.get(db_type, "the connected SQL database")
    return (
        f"You are a SQL generation assistant for a {engine_name} database. "
        "Given a database schema and a natural-language question, respond with "
        "exactly one read-only SQL SELECT statement that answers the question, "
        f"written in {engine_name} SQL syntax. Rules:\n"
        "- Output SQL only. No explanation, no commentary, no markdown fences.\n"
        "- Use only the tables and columns shown in the schema below.\n"
        "- Never use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, ATTACH, or COPY.\n"
        "- Write exactly one statement, ending in at most one semicolon.\n"
        "- Some columns are shown with '-- e.g. <values>', listing every distinct "
        "value actually present in that column. If the question mentions a literal "
        "value (e.g. a name), only filter a column on that value if it appears in "
        "that column's own sample list. If it doesn't appear there, that column is "
        "the wrong one -- look for a different column, often in a related table, "
        "whose sample values (or plain meaning) actually match.\n"
        "- Never filter or join on a short coded column (sample values that look like "
        "abbreviations, e.g. single letters) as if it held a human-readable business "
        "term unless that exact term is one of its sample values.\n"
        "- When joining a fact table out to a dimension for grouping or display, and "
        "that dimension is reached through another dimension (e.g. a 3-level "
        "hierarchy), join through every intermediate table -- never join the fact "
        "table (or an unrelated dimension) directly to a table two or more hops away.\n"
        "- If the final result would otherwise show only a surrogate key column "
        "(a column named like '...Key' or '...ID'), join in and SELECT a "
        "human-readable descriptive column from that same dimension instead (e.g. "
        "a name, region, or category column) -- unless the question explicitly asks "
        "for the raw key/ID."
    )


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

# Per-failure-category hint appended to the retry prompt, on top of the raw
# error text -- makes the retry "targeted" rather than a generic "try again"
# (see agent/error_classification.py for how a failure gets categorized, and
# agent/nodes.py for how the category reaches here). Categories with no
# useful extra hint (or that are never retried at all, like
# "safety_violation") map to "".
_ERROR_CATEGORY_HINTS: dict[str, str] = {
    "parse_error": (
        "The previous SQL failed to parse. Check for syntax mistakes: "
        "missing commas, unbalanced parentheses, misspelled keywords."
    ),
    "syntax_error": (
        "The database rejected the previous SQL for a syntax reason. "
        "Re-check SQL syntax for the target dialect stated above."
    ),
    "missing_reference": (
        "The previous SQL referenced a table or column that does not exist. "
        "Use ONLY the exact table and column names shown in the schema "
        "below -- do not guess or invent names."
    ),
}


def _build_user_prompt(
    question: str,
    schema_context: str,
    previous_sql: str | None,
    error_feedback: str | None,
    error_category: str | None = None,
) -> str:
    """Builds the user-turn prompt, including error feedback on a retry."""
    sections = [f"Schema:\n{schema_context}", f"Question: {question}"]
    if previous_sql and error_feedback:
        retry_block = (
            "Your previous attempt failed. Fix it and return corrected SQL only.\n"
            f"Previous SQL:\n{previous_sql}\n"
            f"Error:\n{error_feedback}"
        )
        hint = _ERROR_CATEGORY_HINTS.get(error_category or "", "")
        if hint:
            retry_block += f"\n{hint}"
        sections.append(retry_block)
    return "\n\n".join(sections)


def extract_sql(raw_response: str) -> str:
    """Extracts a bare SQL statement from a raw LLM response.

    Handles the common case of the model wrapping its answer in a markdown
    code fence (```sql ... ```) despite being told not to, and strips any
    leading/trailing prose-looking lines around a fenced block.

    Args:
        raw_response: Full text returned by the LLM.

    Returns:
        The extracted SQL text (still unvalidated).

    Raises:
        MalformedLLMOutputError: if the response is empty after stripping.
    """
    match = _SQL_FENCE_RE.search(raw_response)
    candidate = match.group(1) if match else raw_response
    candidate = candidate.strip()
    if not candidate:
        raise MalformedLLMOutputError("LLM returned an empty response.")
    return candidate


def generate_sql_from_llm(
    question: str,
    schema_context: str,
    previous_sql: str | None,
    error_feedback: str | None,
    settings: Settings,
    error_category: str | None = None,
) -> str:
    """Calls Ollama to generate a candidate SQL statement.

    Args:
        question: The user's natural-language question.
        schema_context: DDL text for the retrieved top-k relevant tables.
        previous_sql: The prior attempt's SQL, if this is a retry.
        error_feedback: The error from the prior attempt, if this is a retry.
        settings: Application settings (model name, host, token/time limits).
        error_category: Classification of the prior failure (see
            `agent.error_classification.ExecutionErrorCategory` and
            `agent.sql_validator.ViolationType`), used to pick a more
            targeted retry hint than the raw error text alone. None on the
            first attempt.

    Returns:
        Extracted SQL text (not yet validated -- caller must run it through
        `agent.sql_validator.validate_sql`).

    Raises:
        OllamaUnavailableError: if the Ollama server can't be reached or
            returns an error (e.g. the model hasn't been pulled).
        MalformedLLMOutputError: if the response contains no usable text.
    """
    assembly_start = time.perf_counter()
    user_prompt = _build_user_prompt(
        question, schema_context, previous_sql, error_feedback, error_category
    )
    assembly_ms = (time.perf_counter() - assembly_start) * 1000
    logger.info(
        "[timing] stage=prompt_assembly duration_ms=%.2f prompt_chars=%d",
        assembly_ms,
        len(user_prompt),
    )

    client = ollama.Client(
        host=settings.ollama_host, timeout=settings.ollama_request_timeout_seconds
    )

    logger.debug("Calling Ollama model=%s prompt=%r", settings.ollama_model, user_prompt)
    try:
        response = client.chat(
            model=settings.ollama_model,
            messages=[
                {"role": "system", "content": _system_prompt(settings.db_type)},
                {"role": "user", "content": user_prompt},
            ],
            options={
                "num_predict": settings.llm_max_tokens,
                "temperature": 0.0,
            },
        )
    except (ollama.ResponseError, ConnectionError, TimeoutError, OSError) as exc:
        raise OllamaUnavailableError(
            f"Could not reach Ollama at {settings.ollama_host} with model "
            f"'{settings.ollama_model}': {exc}. Is `ollama serve` running and "
            f"has the model been pulled (`ollama pull {settings.ollama_model}`)?"
        ) from exc

    _log_ollama_timing(response)

    content = (
        response.get("message", {}).get("content", "")
        if isinstance(response, dict)
        else getattr(getattr(response, "message", None), "content", "")
    )
    return extract_sql(content)


def _get_field(response: object, name: str) -> int | None:
    """Reads one Ollama response field, tolerating both dict and object responses."""
    if isinstance(response, dict):
        return response.get(name)
    return getattr(response, name, None)


def _log_ollama_timing(response: object) -> None:
    """Logs Ollama's own reported call breakdown (all fields are nanoseconds).

    Far more precise than wrapping the call in `time.perf_counter()`: Ollama
    separates model *load* time (only nonzero on a cold model, e.g. after
    `keep_alive` expires), *prompt* processing time (scales with input
    tokens -- this is what prompt-size trimming would actually reduce), and
    *generation* time (scales with output tokens -- this is what
    `num_predict` bounds). Wall-clock alone can't distinguish these, and
    which one dominates determines whether prompt trimming, a smaller
    model, or neither is the right lever.
    """
    total_ns = _get_field(response, "total_duration")
    load_ns = _get_field(response, "load_duration")
    prompt_eval_ns = _get_field(response, "prompt_eval_duration")
    eval_ns = _get_field(response, "eval_duration")
    prompt_tokens = _get_field(response, "prompt_eval_count")
    output_tokens = _get_field(response, "eval_count")

    if total_ns is None:
        return  # Older Ollama server or a mocked response in tests -- nothing to log.

    logger.info(
        "[timing] stage=llm_call total_ms=%.1f load_ms=%.1f prompt_eval_ms=%.1f "
        "generation_ms=%.1f prompt_tokens=%s output_tokens=%s",
        total_ns / 1e6,
        (load_ns or 0) / 1e6,
        (prompt_eval_ns or 0) / 1e6,
        (eval_ns or 0) / 1e6,
        prompt_tokens,
        output_tokens,
    )
