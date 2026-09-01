"""Unit tests for the SELECT-only allowlist validator (agent/sql_validator.py).

These are the most important tests in the project: this module is the
security boundary between "SQL an LLM produced" and "SQL that touches a
real (even if read-only) database connection."
"""

from __future__ import annotations

import pytest

from agent.sql_validator import (
    SAFETY_VIOLATION_TYPES,
    enforce_row_limit,
    strip_row_limit,
    validate_sql,
)


class TestValidateSqlAcceptsReadOnlyQueries:
    def test_simple_select(self):
        result = validate_sql("SELECT * FROM customers")
        assert result.is_valid
        assert result.error is None
        assert result.normalized_sql is not None
        assert "SELECT" in result.normalized_sql.upper()

    def test_select_with_join_and_aggregation(self):
        sql = """
            SELECT c.region, SUM(oi.quantity * oi.unit_price) AS revenue
            FROM customers c
            JOIN orders o ON o.customer_id = c.customer_id
            JOIN order_items oi ON oi.order_id = o.order_id
            GROUP BY c.region
        """
        result = validate_sql(sql)
        assert result.is_valid

    def test_select_with_cte(self):
        sql = """
            WITH regional_totals AS (
                SELECT region, COUNT(*) AS n FROM customers GROUP BY region
            )
            SELECT * FROM regional_totals WHERE n > 1
        """
        result = validate_sql(sql)
        assert result.is_valid

    def test_compound_union_select(self):
        sql = "SELECT customer_id FROM customers UNION SELECT customer_id FROM orders"
        result = validate_sql(sql)
        assert result.is_valid

    def test_trailing_semicolon_is_tolerated(self):
        result = validate_sql("SELECT 1;")
        assert result.is_valid


class TestValidateSqlRejectsUnsafeInput:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO customers (customer_id) VALUES (1)",
            "UPDATE customers SET region = 'West'",
            "DELETE FROM customers",
            "DROP TABLE customers",
            "ALTER TABLE customers ADD COLUMN foo INT",
            "TRUNCATE TABLE customers",
            "ATTACH 'other.duckdb' AS other",
            "COPY customers TO 'out.csv'",
            "CREATE TABLE evil (id INT)",
        ],
    )
    def test_rejects_non_select_statements(self, sql):
        result = validate_sql(sql)
        assert not result.is_valid
        assert result.error is not None
        # Most of these parse fine as their real statement type and get
        # rejected by the allowlist check ("disallowed_statement"); ATTACH
        # is DuckDB-specific syntax that isn't recognized by sqlglot's
        # generic dialect at all, so it's rejected earlier, at parse time
        # ("parse_error") -- either way, nothing here ever reaches the DB.
        assert result.violation_type in {"disallowed_statement", "parse_error"}

    def test_rejects_multiple_statements(self):
        result = validate_sql("SELECT 1; DROP TABLE customers;")
        assert not result.is_valid
        assert result.error is not None
        assert "single" in result.error.lower()
        assert result.violation_type == "multiple_statements"
        assert result.violation_type in SAFETY_VIOLATION_TYPES

    def test_rejects_select_into(self):
        result = validate_sql("SELECT * INTO evil_table FROM customers")
        assert not result.is_valid
        assert result.violation_type == "select_into"
        assert result.violation_type in SAFETY_VIOLATION_TYPES

    def test_rejects_empty_string(self):
        result = validate_sql("")
        assert not result.is_valid
        assert result.violation_type == "empty"
        assert result.violation_type not in SAFETY_VIOLATION_TYPES

    def test_rejects_whitespace_only(self):
        result = validate_sql("   \n\t  ")
        assert not result.is_valid
        assert result.violation_type == "empty"

    def test_rejects_unparseable_sql(self):
        result = validate_sql("SELEKT * FORM nowhere !!!")
        assert not result.is_valid
        assert result.error is not None
        assert "parse" in result.error.lower()
        assert result.violation_type == "parse_error"
        assert result.violation_type not in SAFETY_VIOLATION_TYPES


class TestValidateSqlAdversarialCases:
    """Cases specifically shaped to probe the allowlist, not just the happy path.

    These target the failure modes a regex/keyword blocklist would miss:
    case variation, comments used to obscure or pad malicious SQL, and
    classic stacked-query injection (`SELECT ...; DROP ...;`) -- including
    the variant where *both* statements are themselves harmless SELECTs,
    since stacking is rejected structurally (more than one statement),
    not because either half looks dangerous on its own.
    """

    def test_case_variation_does_not_bypass_rejection(self):
        result = validate_sql("DrOp TaBlE customers")
        assert not result.is_valid

    def test_lowercase_select_is_still_accepted(self):
        result = validate_sql("select * from customers")
        assert result.is_valid

    def test_classic_stacked_query_injection_is_rejected(self):
        result = validate_sql("SELECT * FROM customers WHERE 1=1; DROP TABLE customers; --")
        assert not result.is_valid
        assert result.error is not None
        assert "single" in result.error.lower()

    def test_stacking_two_harmless_selects_is_still_rejected(self):
        """Multiple statements are rejected structurally, even if every one of
        them is individually a harmless SELECT -- stacking itself is the
        thing being blocked, not just "does any statement look dangerous"."""
        result = validate_sql("SELECT 1; SELECT 2;")
        assert not result.is_valid

    def test_multiline_stacked_query_is_rejected(self):
        result = validate_sql("SELECT 1\n;\nDROP TABLE x")
        assert not result.is_valid

    def test_block_comment_content_does_not_affect_validity(self):
        """A comment mentioning a forbidden keyword is inert text, not SQL --
        the AST-based check looks at parsed statement types, not substrings,
        so it correctly ignores comment contents either way."""
        result = validate_sql(
            "SELECT * FROM customers /* inline comment mentioning DROP */ WHERE id = 1"
        )
        assert result.is_valid

    def test_leading_comment_before_select_is_accepted(self):
        result = validate_sql("/* setup */ SELECT 1")
        assert result.is_valid

    def test_trailing_comment_after_select_is_conservatively_rejected(self):
        """A trailing `-- comment` after the statement parses as extra
        (non-empty) content beyond the first statement, so it's rejected by
        the "exactly one statement" rule -- stricter than necessary for this
        harmless case, but the safer failure mode versus trying to
        special-case "which trailing content is safe to ignore"."""
        result = validate_sql("SELECT * FROM customers; -- comment")
        assert not result.is_valid

    def test_keyword_split_by_comment_is_rejected(self):
        """A classic WAF-bypass trick (`SEL/**/ECT`) -- this doesn't even
        parse as valid SQL, so it's rejected via the parse-failure path
        rather than the statement-type check, but the end result is the
        same: nothing resembling this executes."""
        result = validate_sql("SEL/**/ECT 1")
        assert not result.is_valid


class TestEnforceRowLimit:
    def test_adds_limit_when_missing(self):
        sql = enforce_row_limit("SELECT * FROM customers", max_rows=1000)
        assert "LIMIT 1000" in sql.upper()

    def test_clamps_limit_above_max(self):
        sql = enforce_row_limit("SELECT * FROM customers LIMIT 100000", max_rows=1000)
        assert "LIMIT 1000" in sql.upper()
        assert "100000" not in sql

    def test_keeps_limit_below_max(self):
        sql = enforce_row_limit("SELECT * FROM customers LIMIT 10", max_rows=1000)
        assert "LIMIT 10" in sql.upper()
        assert "LIMIT 1000" not in sql.upper()


class TestStripRowLimit:
    """Regression coverage for a real bug found during development: a
    `TOP`/`LIMIT` clause makes a query's optimizer stop scanning/joining
    early, so its reported estimate collapses to roughly the cap
    regardless of the true underlying cost -- verified against a real
    accidental cross join on AdventureWorksDW2025 (SECURITY.md has the
    numbers). db.query_cost's cost-estimation step is useless against
    already-row-limited SQL (which is *all* generated SQL, since
    validate_sql_node always applies enforce_row_limit first) without this.
    """

    def test_removes_limit_clause(self):
        sql = strip_row_limit("SELECT * FROM customers LIMIT 1000")
        assert "LIMIT" not in sql.upper()

    def test_removes_mssql_top_clause(self):
        sql = strip_row_limit("SELECT TOP 1000 * FROM customers", dialect="tsql")
        assert "TOP" not in sql.upper()

    def test_no_limit_present_is_a_no_op(self):
        sql = strip_row_limit("SELECT * FROM customers WHERE id = 1")
        assert "customers" in sql
        assert "WHERE" in sql.upper()

    def test_never_mutates_the_actual_sql_text_used_for_execution(self):
        """The whole point: enforce_row_limit's output must stay usable for
        real execution -- strip_row_limit only ever produces a *separate*
        string for cost estimation, never replacing the caller's original."""
        limited = enforce_row_limit("SELECT * FROM customers", max_rows=1000)
        unlimited_copy = strip_row_limit(limited)
        assert "LIMIT" in limited.upper()
        assert "LIMIT" not in unlimited_copy.upper()

    def test_rejects_non_query_statement_shape(self):
        # Precondition: only ever called on SQL that already passed
        # validate_sql, which guarantees a Select/Union/Except/Intersect.
        with pytest.raises(TypeError):
            strip_row_limit("INSERT INTO customers VALUES (1)")
