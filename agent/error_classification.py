"""Classifies execution-time SQL failures so the retry loop can react
differently depending on *why* a query failed, instead of treating every
failure as "try again with the same generic feedback."

Mirrors the classification pattern in `db/connection.py`'s `_classify_error`
(keyword-matching on driver error text, since SQLAlchemy does not normalize
error causes consistently across psycopg2/pymysql/pyodbc/oracledb) but for a
different purpose: this one drives *retry routing* -- which node the graph
goes back to, and what hint the next generation attempt gets -- not just a
human-readable message for a connection test.
"""

from __future__ import annotations

from enum import Enum


class ExecutionErrorCategory(str, Enum):
    """Why a validated, executing query failed.

    SYNTAX: the database rejected the query for a syntax/logic reason
        (bad keyword, ambiguous column, etc.) -- the schema context was
        fine, the generated SQL text wasn't. Worth retrying via
        `generate_sql` with the same schema context.
    MISSING_REFERENCE: the query referenced a table or column that does not
        exist in the target database. This can mean the LLM hallucinated a
        name, but it can just as easily mean schema retrieval surfaced the
        wrong tables in the first place -- so this category routes back to
        `retrieve_schema`, not straight to `generate_sql`.
    TIMEOUT: the query exceeded `QUERY_TIMEOUT_SECONDS`. Retrying the same
        (or a similarly-shaped) expensive query is unlikely to help and
        wastes a retry budget slot -- this category fails immediately
        rather than looping.
    UNKNOWN: driver error text didn't match a known pattern. Still retried
        like SYNTAX (the safest default), just without a more targeted
        prompt hint.
    """

    SYNTAX = "syntax"
    MISSING_REFERENCE = "missing_reference"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# Keyword fragments observed across the four supported drivers' error text
# for "you referenced something that doesn't exist." Deliberately broad
# (lowercased substring match) rather than parsing driver-specific error
# codes, since sqlglot already guarantees the SQL *parses*; what's left to
# diagnose here is a semantic mismatch against the real schema.
_MISSING_REFERENCE_KEYWORDS = (
    "invalid object name",  # mssql: table/view doesn't exist
    "invalid column name",  # mssql: column doesn't exist
    "unknown column",  # mysql
    "unknown table",  # mysql
    "no such table",  # sqlite-style drivers, harmless to include
    "no such column",
    "does not exist",  # postgres: relation/column "x" does not exist
    "ora-00904",  # oracle: invalid identifier
    "ora-00942",  # oracle: table or view does not exist
)

_SYNTAX_KEYWORDS = (
    "incorrect syntax",  # mssql
    "syntax error",  # postgres / mysql
    "ambiguous column name",  # mssql -- a real column, just unqualified; the
    # fix is in the SQL text (qualify it), not the schema, so treat as syntax
    "ora-00933",  # oracle: sql command not properly ended
)


def classify_execution_error(exc: BaseException) -> ExecutionErrorCategory:
    """Best-effort classification of an execution failure.

    Args:
        exc: The exception raised by `agent.nodes.execute_readonly_sql`
            (a `sqlalchemy.exc.SQLAlchemyError` or a `TimeoutError`).

    Returns:
        The best-matching `ExecutionErrorCategory`. `TIMEOUT` is checked by
        exception type first (unambiguous); the rest is keyword-matched on
        `str(exc)`, which for SQLAlchemy errors includes the underlying
        driver's original error text.
    """
    if isinstance(exc, TimeoutError):
        return ExecutionErrorCategory.TIMEOUT

    message = str(exc).lower()
    if any(keyword in message for keyword in _MISSING_REFERENCE_KEYWORDS):
        return ExecutionErrorCategory.MISSING_REFERENCE
    if any(keyword in message for keyword in _SYNTAX_KEYWORDS):
        return ExecutionErrorCategory.SYNTAX
    return ExecutionErrorCategory.UNKNOWN
