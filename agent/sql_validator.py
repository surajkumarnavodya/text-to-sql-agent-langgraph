"""Validates LLM-generated SQL before it is ever allowed to execute.

Security model: the LLM's output is treated as untrusted input, exactly like
a request from an untrusted client, regardless of the fact that "we" wrote
the prompt. Validation is allowlist-based on the *parsed statement type*
(via sqlglot), not a regex/keyword blocklist -- a blocklist can always be
bypassed by a syntax variant it didn't anticipate (comments, casing, string
tricks); an allowlist on the AST node type cannot, because there is no way
to construct an INSERT/DROP/ATTACH/etc. statement that parses to an
`exp.Select`/`exp.Union` root node.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

# Read-only, single-query statement shapes. exp.Union/Except/Intersect cover
# compound `SELECT ... UNION SELECT ...` queries, which are still read-only.
_ALLOWED_ROOT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

# None is sqlglot's own "generic/standard SQL" dialect -- used as the default
# here so this module has no dependency on which real database is configured.
# Callers on the actual query path (agent/nodes.py, ui/app.py) resolve and
# pass the real dialect via `db.connection.get_sqlglot_dialect(settings.db_type)`.
DEFAULT_DIALECT: str | None = None


# Distinguishes *why* validation failed, so callers (agent/nodes.py) can
# react differently: a violation type in `SAFETY_VIOLATION_TYPES` means the
# LLM produced a statement this app must never execute (a non-SELECT, a
# stacked query, or a SELECT that creates a table as a side effect) -- that
# is a security-gate failure, not a mistake worth coaching the model through,
# so the agent fails closed immediately rather than retrying. "empty" and
# "parse_error" are ordinary correctness mistakes (the LLM produced
# malformed or no text) and are safe to retry with error feedback.
ViolationType = Literal[
    "empty", "parse_error", "multiple_statements", "disallowed_statement", "select_into"
]

SAFETY_VIOLATION_TYPES: frozenset[str] = frozenset(
    {"multiple_statements", "disallowed_statement", "select_into"}
)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a candidate SQL string.

    Attributes:
        is_valid: Whether the SQL passed all checks.
        error: Human-readable reason for rejection (None if valid). This text
            is fed back to the LLM verbatim on retry, so it's written to be
            actionable ("X is not allowed" rather than a stack trace).
        normalized_sql: The re-serialized SQL from the parsed AST (None if
            invalid). Re-serializing (rather than passing the original
            string through) ensures what gets executed is exactly what was
            parsed and validated -- no gap between "what we checked" and
            "what we run".
        violation_type: Machine-readable reason for rejection (None if
            valid) -- see `SAFETY_VIOLATION_TYPES` for which values mean
            "fail closed, do not retry" versus "ordinary retry-able mistake."
    """

    is_valid: bool
    error: str | None = None
    normalized_sql: str | None = None
    violation_type: ViolationType | None = None


def validate_sql(sql: str, dialect: str | None = DEFAULT_DIALECT) -> ValidationResult:
    """Parses `sql` and checks it against the SELECT-only allowlist.

    Rejects:
        - Empty input.
        - SQL that fails to parse.
        - More than one statement (blocks `SELECT 1; DROP TABLE x`).
        - Any root statement type other than SELECT/UNION/EXCEPT/INTERSECT
          (blocks INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/ATTACH/COPY/etc.).
        - `SELECT ... INTO <table>`, which would create a table as a
          side effect despite being SELECT-shaped.

    Args:
        sql: Raw SQL text, as produced by the LLM (already stripped of any
            markdown code fences by the caller).
        dialect: sqlglot dialect to parse with. Defaults to sqlglot's
            generic/standard SQL dialect; pass the real one (via
            `db.connection.get_sqlglot_dialect(settings.db_type)`) on the
            actual query path.

    Returns:
        A `ValidationResult`.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return ValidationResult(
            is_valid=False, error="Generated SQL is empty.", violation_type="empty"
        )

    try:
        statements = [s for s in sqlglot.parse(stripped, read=dialect) if s is not None]
    except ParseError as exc:
        logger.debug("SQL failed to parse: %s", exc)
        return ValidationResult(
            is_valid=False, error=f"SQL failed to parse: {exc}", violation_type="parse_error"
        )

    if len(statements) == 0:
        return ValidationResult(
            is_valid=False, error="Generated SQL is empty.", violation_type="empty"
        )
    if len(statements) > 1:
        return ValidationResult(
            is_valid=False,
            error="Only a single SQL statement is allowed; found multiple statements.",
            violation_type="multiple_statements",
        )

    statement = statements[0]
    if not isinstance(statement, _ALLOWED_ROOT_TYPES):
        return ValidationResult(
            is_valid=False,
            error=(
                f"Only SELECT statements are allowed; got a "
                f"{type(statement).__name__} statement instead."
            ),
            violation_type="disallowed_statement",
        )

    if statement.args.get("into") is not None:
        return ValidationResult(
            is_valid=False,
            error="'SELECT ... INTO <table>' is not allowed (it creates a table).",
            violation_type="select_into",
        )

    return ValidationResult(
        is_valid=True,
        normalized_sql=statement.sql(dialect=dialect),
    )


def enforce_row_limit(sql: str, max_rows: int, dialect: str | None = DEFAULT_DIALECT) -> str:
    """Clamps or adds a `LIMIT` clause so a query can never return more than `max_rows`.

    Must only be called on SQL that has already passed `validate_sql` --
    it assumes a single SELECT/UNION/EXCEPT/INTERSECT statement.

    Args:
        sql: Already-validated SQL text.
        max_rows: Row cap to enforce (e.g. `Settings.max_result_rows`).
        dialect: sqlglot dialect to parse/render with.

    Returns:
        SQL text with a LIMIT clause no greater than `max_rows`.
    """
    statement = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(statement, exp.Query):
        # Precondition violation: callers must only pass SQL that already
        # passed validate_sql, which guarantees a Select/Union/Except/Intersect
        # (all exp.Query subclasses, the ones with a .limit() method).
        raise TypeError(
            f"enforce_row_limit expects already-validated SELECT-shaped SQL, "
            f"got a {type(statement).__name__} statement instead."
        )

    current_limit: int | None = None
    limit_clause = statement.args.get("limit")
    if limit_clause is not None:
        literal = limit_clause.expression
        if isinstance(literal, exp.Literal) and literal.is_number:
            try:
                current_limit = int(literal.this)
            except (TypeError, ValueError):
                current_limit = None

    if current_limit is None or current_limit > max_rows:
        statement = statement.limit(max_rows)

    return statement.sql(dialect=dialect)
