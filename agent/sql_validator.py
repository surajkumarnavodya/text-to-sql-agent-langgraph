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
import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

logger = logging.getLogger(__name__)

# Read-only, single-query statement shapes. exp.Union/Except/Intersect cover
# compound `SELECT ... UNION SELECT ...` queries, which are still read-only.
_ALLOWED_ROOT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

# Write/DDL-shaped node types, checked *anywhere* in the parsed tree -- not
# just at the root (see _ALLOWED_ROOT_TYPES above). This closes a real
# bypass class: several engines (Postgres chief among them) support
# data-modifying CTEs -- `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM
# x` -- whose *root* node is an ordinary exp.Select even though it deletes
# real rows. A root-type-only check accepts that statement; this full-tree
# walk does not, regardless of how deeply the write is nested (a CTE, a
# subquery, ...). exp.Command is included because it's sqlglot's fallback
# for a fragment it couldn't parse into anything more specific (e.g. an
# engine-specific EXEC/CALL form) -- a legitimate read-only query never
# produces one anywhere in its tree, so its presence is only ever a sign the
# statement isn't what it appears to be.
_DISALLOWED_NESTED_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
)

# Known-dangerous function/procedure/table-valued-function names, callable
# from inside an otherwise ordinary, single-statement SELECT -- so the
# statement-type allowlist above (by design) never sees them as anything
# other than "a SELECT." Unlike that allowlist, this list is a **denylist**:
# it can only ever be as complete as what's enumerated here, and an
# engine-specific dangerous function not on this list is a real, standing
# residual risk (documented in SECURITY.md, the same honesty this project
# already applies to `agent/input_guard.py`'s own regex layer).
#   - postgresql: pg_sleep/pg_read_file/pg_read_binary_file/pg_ls_dir/
#     pg_stat_file (DoS / local file & directory disclosure); lo_import/
#     lo_export (large-object file I/O); dblink* (cross-database/network
#     connections).
#   - mysql: sleep/benchmark (DoS); load_file (local file disclosure).
#   - mssql: openrowset/openquery/opendatasource (remote/linked-server
#     query execution -- SSRF/lateral-pivot capable); xp_cmdshell/
#     xp_dirtree/xp_fileexist (OS command execution / filesystem probing);
#     sp_configure/sp_oacreate (server reconfiguration / OLE automation).
#   - oracle: utl_http/utl_tcp/utl_smtp (SSRF / network egress); utl_file
#     (filesystem I/O); utl_inaddr (DNS/hostname resolution, exfiltration-
#     capable); dbms_lock (sleep-based DoS); dbms_scheduler/dbms_java
#     (arbitrary job/code execution).
_DANGEROUS_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_connect",
        "dblink_exec",
        "sleep",
        "benchmark",
        "load_file",
        "openrowset",
        "openquery",
        "opendatasource",
        "xp_cmdshell",
        "xp_dirtree",
        "xp_fileexist",
        "sp_configure",
        "sp_oacreate",
        "utl_http",
        "utl_tcp",
        "utl_smtp",
        "utl_file",
        "utl_inaddr",
        "dbms_lock",
        "dbms_scheduler",
        "dbms_java",
    }
)

# Raw-text fallback for the same names, requiring the name (as a whole word)
# to be immediately followed by either `(` or a `.qualifier(` chain -- e.g.
# `pg_sleep(` and `UTL_HTTP.REQUEST(` both match, but a column merely *named*
# "sleep_duration" or "utl_http_helper" cannot (no word boundary lands there).
# This exists only to catch a dialect-qualified call where sqlglot's AST
# stores the package qualifier (e.g. "UTL_HTTP") on a different node than the
# immediate call name ("REQUEST") the tree walk below inspects directly.
_DANGEROUS_FUNCTION_RE = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in sorted(_DANGEROUS_FUNCTION_NAMES))
    + r")\b(?:\.\w+)*\s*\(",
    re.IGNORECASE,
)

# None is sqlglot's own "generic/standard SQL" dialect -- used as the default
# here so this module has no dependency on which real database is configured.
# Callers on the actual query path (agent/nodes.py, ui/app.py) resolve and
# pass the real dialect via `db.connection.get_sqlglot_dialect(settings.db_type)`.
DEFAULT_DIALECT: str | None = None


# Distinguishes *why* validation failed, so callers (agent/nodes.py) can
# react differently: a violation type in `SAFETY_VIOLATION_TYPES` means the
# LLM produced a statement this app must never execute (a non-SELECT, a
# stacked query, a SELECT that creates a table as a side effect, a write
# operation embedded inside an otherwise read-only query, or a call to a
# known-dangerous function) -- that is a security-gate failure, not a
# mistake worth coaching the model through, so the agent fails closed
# immediately rather than retrying. "empty" and "parse_error" are ordinary
# correctness mistakes (the LLM produced malformed or no text) and are safe
# to retry with error feedback.
ViolationType = Literal[
    "empty",
    "parse_error",
    "multiple_statements",
    "disallowed_statement",
    "select_into",
    "embedded_write",
    "dangerous_function",
]

SAFETY_VIOLATION_TYPES: frozenset[str] = frozenset(
    {
        "multiple_statements",
        "disallowed_statement",
        "select_into",
        "embedded_write",
        "dangerous_function",
    }
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
        - A write/DDL operation embedded *anywhere* in the tree, not just at
          the root -- e.g. a data-modifying CTE
          (`WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`), whose
          root is an ordinary SELECT even though it deletes real rows.
        - A call to a known-dangerous function/procedure (e.g. `pg_sleep`,
          `xp_cmdshell`, `OPENQUERY`, `UTL_HTTP.REQUEST`) -- see
          `_DANGEROUS_FUNCTION_NAMES` for the full list and why each is
          there. This is a denylist, not an allowlist, and is documented as
          such (not exhaustive) in SECURITY.md.

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
    except SqlglotError as exc:
        # Deliberately the broad `SqlglotError` base, not just `ParseError`:
        # sqlglot's *tokenizer* can fail before parsing even starts (e.g. an
        # unterminated quoted string in malformed LLM output), raising a
        # sibling `TokenError` that a narrower `except ParseError` would not
        # catch -- confirmed live during benchmark authoring, where a
        # malformed model response crashed the whole agent run with an
        # unhandled `TokenError` instead of failing closed here as an
        # ordinary "parse_error" (retryable, see SAFETY_VIOLATION_TYPES).
        # Untrusted LLM output failing to tokenize is exactly the same kind
        # of "ordinary correctness mistake" as failing to parse -- it must
        # never be the reason the whole request crashes instead of
        # retrying/failing cleanly.
        logger.debug("SQL failed to tokenize/parse: %s", exc)
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

    nested_write = next(
        (node for node in statement.walk() if isinstance(node, _DISALLOWED_NESTED_TYPES)), None
    )
    if nested_write is not None:
        return ValidationResult(
            is_valid=False,
            error=(
                f"The statement contains a {type(nested_write).__name__} operation "
                "embedded inside an otherwise read-only query (e.g. a data-modifying "
                "CTE). Write and DDL operations are never allowed, regardless of the "
                "outer statement shape."
            ),
            violation_type="embedded_write",
        )

    dangerous_name: str | None = next(
        (
            node.name.lower()
            for node in statement.walk()
            if isinstance(node, exp.Func) and (node.name or "").lower() in _DANGEROUS_FUNCTION_NAMES
        ),
        None,
    )
    if dangerous_name is None:
        text_match = _DANGEROUS_FUNCTION_RE.search(stripped)
        if text_match:
            dangerous_name = text_match.group(1).lower()
    if dangerous_name is not None:
        return ValidationResult(
            is_valid=False,
            error=(
                f"The function/procedure '{dangerous_name}' is not allowed -- it can "
                "access the filesystem, network, or server configuration rather than "
                "just querying data."
            ),
            violation_type="dangerous_function",
        )

    return ValidationResult(
        is_valid=True,
        normalized_sql=statement.sql(dialect=dialect),
    )


def find_unexpected_table_references(
    sql: str, known_tables: set[str], dialect: str | None = DEFAULT_DIALECT
) -> list[str]:
    """Flags tables `sql` references that were never part of the retrieved schema context.

    This is a *detection* signal, not a new gate -- the SELECT-only
    allowlist above plus the read-only database connection already bound
    what any generated SQL can actually do, regardless of which tables it
    names. What this catches is a different thing: one concrete symptom of
    a successful prompt injection via poisoned schema/sampled-value content
    (see `db/value_sampling.py`'s security note) is the model suddenly
    referencing a table or column it was never shown, because something in
    the data it was given told it to. A table appearing here doesn't by
    itself prove an injection happened -- it's also what a plain
    hallucinated table name looks like -- but either way it's worth logging
    distinctly so a pattern of it is inspectable (see `agent.nodes.
    validate_sql_node`, the caller, for how this is logged).

    Args:
        sql: Already-validated SQL (only called after `validate_sql`
            confirms it parses and is SELECT-shaped; a parse failure here
            is treated as "nothing to flag," not re-raised -- that
            diagnosis is `validate_sql`'s job).
        known_tables: Table names that were actually part of the retrieved
            schema context for this attempt (`AgentState["schema_tables"]`),
            compared case-insensitively.
        dialect: sqlglot dialect to parse with.

    Returns:
        Sorted list of table names referenced in `sql` that aren't in
        `known_tables` -- empty if none (the common case) or if `sql`
        doesn't parse.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return []

    referenced = {table.name for table in statement.find_all(exp.Table) if table.name}
    known_lower = {name.lower() for name in known_tables}
    return sorted(name for name in referenced if name.lower() not in known_lower)


def references_multiple_tables(sql: str, dialect: str | None = DEFAULT_DIALECT) -> bool:
    """Whether `sql` references two or more distinct tables (i.e. involves a join).

    Used by `agent.nodes.execute_sql_node` to decide whether a successful
    but zero-row result is worth flagging as low-confidence: a single-table
    zero-row result (e.g. "how many orders in 2050") is almost always a
    legitimate answer, but a multi-table zero-row result is also the
    observable symptom of a join that matched columns from unrelated key
    spaces (see `agent.llm_client._system_prompt`'s join-correctness rules)
    -- a query that runs without error but is silently wrong.

    Args:
        sql: Already-validated SQL.
        dialect: sqlglot dialect to parse with.

    Returns:
        True if 2+ distinct table names are referenced. False if `sql`
        doesn't parse (diagnosing that is `validate_sql`'s job, not this
        function's).
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return False

    referenced = {table.name.lower() for table in statement.find_all(exp.Table) if table.name}
    return len(referenced) >= 2


def find_restricted_column_references(
    sql: str,
    restricted_columns: set[tuple[str, str]],
    known_tables: set[str],
    dialect: str | None = DEFAULT_DIALECT,
) -> list[tuple[str, str]]:
    """Flags (table, column) pairs `sql` selects that are classified "restricted".

    Enforces `config/sensitive_columns.yaml`'s policy (see
    `config.sensitive_columns`, and `docs/GOVERNANCE.md`'s "Data
    classification policy") -- unlike `find_unexpected_table_references`
    above (a detection signal only), a hit here **is** a new gate: the
    caller (`agent.nodes.validate_sql_node`) treats it exactly like a
    validation failure and does not let the query execute.

    Args:
        sql: Already-validated SQL.
        restricted_columns: (table_name, column_name) pairs classified
            "restricted" -- pre-filtered by the caller from
            `config.sensitive_columns.load_sensitive_columns()`'s full
            tier map (this function doesn't know about "internal", only
            "restricted").
        known_tables: Table names actually part of this attempt's
            retrieved schema context, compared case-insensitively -- same
            convention as `find_unexpected_table_references`.
        dialect: sqlglot dialect to parse with.

    Returns:
        Sorted list of (table_name, column_name) pairs referenced in `sql`
        that are classified restricted -- empty if none (the overwhelming
        common case while the classification file is unpopulated), or if
        `sql` doesn't parse.

        Matching is name-based (a restricted column name appearing
        anywhere in the statement, whose owning table is among
        `known_tables`), not full table-qualification resolution --
        deliberately conservative in the safe direction: a false positive
        only costs a retry (the model gets a chance to drop the column and
        answer with what remains), while a false negative would let a
        restricted column through undetected.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return []

    known_tables_lower = {name.lower() for name in known_tables}
    referenced_columns_lower = {
        column.name.lower() for column in statement.find_all(exp.Column) if column.name
    }

    flagged = {
        (table_name, column_name)
        for table_name, column_name in restricted_columns
        if table_name.lower() in known_tables_lower
        and column_name.lower() in referenced_columns_lower
    }
    return sorted(flagged)


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


def strip_row_limit(sql: str, dialect: str | None = DEFAULT_DIALECT) -> str:
    """Removes a `LIMIT`/`TOP` clause -- for cost *estimation* only, never for execution.

    A row cap dramatically changes what a query optimizer estimates: a
    `TOP 1000` (or `LIMIT 1000`) lets the engine stop scanning/joining as
    soon as 1000 rows are found, so a plan's reported row estimate collapses
    to ~1000 regardless of how expensive satisfying the query's actual
    WHERE/JOIN logic would be -- verified against a real accidental cross
    join on AdventureWorksDW2025, where adding `TOP 1000` made SQL Server
    report ~1,000 estimated rows for a query that, unlimited, estimates
    over 1.1 *billion*. `agent.nodes.estimate_query_cost_node` calls this
    to see the query's true shape before the cap is applied, but the SQL
    that's actually *executed* always keeps the real limit (`state["sql"]`
    is never replaced by this function's output) -- this exists purely to
    give the cost-estimation step an accurate picture, not to change what
    runs.

    Args:
        sql: Already row-limited SQL (the output of `enforce_row_limit`).
        dialect: sqlglot dialect to parse/render with.

    Returns:
        The same SQL with its `LIMIT`/`TOP` clause removed.
    """
    statement = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(statement, exp.Query):
        raise TypeError(
            f"strip_row_limit expects already-validated SELECT-shaped SQL, "
            f"got a {type(statement).__name__} statement instead."
        )
    statement.set("limit", None)
    return statement.sql(dialect=dialect)
