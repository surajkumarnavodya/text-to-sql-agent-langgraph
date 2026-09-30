"""Prompt 05 (`05_SQL_SERVER_DIALECT_VALIDATION_CONTRACT.md`) regression suite.

Two purposes, kept in one file since they were investigated together:

1. **Lock in** that every T-SQL construct the prompt named (TOP, OFFSET/
   FETCH, CTEs, window functions, the DATEADD/DATEDIFF/DATEPART/DATENAME
   family, EOMONTH, STRING_AGG, TRY_CAST/TRY_CONVERT, PERCENTILE_CONT
   WITHIN GROUP, PIVOT/UNPIVOT, bracket identifiers) already passes
   `validate_sql` under `dialect="tsql"` -- sqlglot's own grammar handles
   all of these; this suite exists so a future change to
   `agent/sql_validator.py` can't silently regress one of them unnoticed.
2. **Prove the two real bugs found while verifying (1) are fixed**: a CTE
   reference being schema-qualified as if it were a real table
   (`qualify_table_schema`), and an explicit `FETCH NEXT n ROWS ONLY`
   being silently widened to `max_rows` (`enforce_row_limit`).

Every case here was run against the *real* functions during investigation
(not assumed from reading the code) -- see this prompt's own contract doc
for the full inspection record.
"""

from __future__ import annotations

import pytest

from agent.sql_validator import (
    enforce_row_limit,
    find_restricted_column_references,
    find_unexpected_table_references,
    qualify_table_schema,
    references_multiple_tables,
    validate_sql,
)

DIALECT = "tsql"


class TestTSqlConstructsAreAlreadyAccepted:
    """sqlglot's own tsql grammar already parses these into an ordinary
    exp.Select tree -- the validator never special-cases syntax, only
    statement shape, so none of these ever needed new code. This class
    exists purely as a regression lock."""

    @pytest.mark.parametrize(
        "name,sql",
        [
            ("TOP", "SELECT TOP 10 CustomerKey FROM DimCustomer ORDER BY CustomerKey"),
            (
                "OFFSET/FETCH",
                "SELECT CustomerKey FROM DimCustomer ORDER BY CustomerKey "
                "OFFSET 10 ROWS FETCH NEXT 20 ROWS ONLY",
            ),
            (
                "CTE",
                "WITH RecentOrders AS (SELECT SalesOrderNumber FROM FactInternetSales) "
                "SELECT * FROM RecentOrders",
            ),
            (
                "window function",
                "SELECT CustomerKey, SUM(SalesAmount) OVER (PARTITION BY CustomerKey) "
                "AS RunningTotal FROM FactInternetSales",
            ),
            ("DATEADD", "SELECT DATEADD(day, 30, OrderDate) FROM FactInternetSales"),
            ("DATEDIFF", "SELECT DATEDIFF(day, OrderDate, ShipDate) FROM FactInternetSales"),
            ("DATEPART", "SELECT DATEPART(year, OrderDate) FROM FactInternetSales"),
            ("DATENAME", "SELECT DATENAME(month, OrderDate) FROM FactInternetSales"),
            ("EOMONTH", "SELECT EOMONTH(OrderDate) FROM FactInternetSales"),
            (
                "STRING_AGG",
                "SELECT CustomerKey, STRING_AGG(SalesOrderNumber, ', ') FROM FactInternetSales "
                "GROUP BY CustomerKey",
            ),
            ("TRY_CAST", "SELECT TRY_CAST(SalesAmount AS INT) FROM FactInternetSales"),
            ("TRY_CONVERT", "SELECT TRY_CONVERT(INT, SalesAmount) FROM FactInternetSales"),
            (
                "PERCENTILE_CONT",
                "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY SalesAmount) "
                "OVER (PARTITION BY CustomerKey) FROM FactInternetSales",
            ),
            (
                "PIVOT",
                "SELECT * FROM (SELECT CustomerKey, SalesAmount, CalendarYear "
                "FROM FactInternetSales) AS src "
                "PIVOT (SUM(SalesAmount) FOR CalendarYear IN ([2020],[2021])) AS pvt",
            ),
            (
                "UNPIVOT",
                "SELECT CustomerKey, CalendarYear, SalesAmount FROM "
                "(SELECT CustomerKey, [2020], [2021] FROM PivotedSales) AS src "
                "UNPIVOT (SalesAmount FOR CalendarYear IN ([2020],[2021])) AS unpvt",
            ),
            (
                "bracket identifiers",
                "SELECT [Customer Key], [First Name] FROM [dbo].[DimCustomer]",
            ),
        ],
    )
    def test_construct_is_valid(self, name, sql):
        result = validate_sql(sql, dialect=DIALECT)
        assert result.is_valid, f"{name} unexpectedly rejected: {result.error}"


class TestTSqlMaliciousAndObfuscatedInputsAreStillRejected:
    """Confirms the existing AST-allowlist/denylist protections hold for
    mssql-specific attack shapes, including obfuscation -- run against the
    real function, not inferred from reading the code."""

    @pytest.mark.parametrize(
        "name,sql",
        [
            ("standalone EXEC", "EXEC sp_who"),
            ("EXEC xp_cmdshell", "EXEC xp_cmdshell 'dir'"),
            (
                "stacked sp_executesql",
                "SELECT 1; EXEC sp_executesql N'DROP TABLE Employee'",
            ),
            ("comment-hidden stack", "SELECT 1 --\n; DROP TABLE Employee"),
            ("bracket-quoted sys catalog", "SELECT name FROM [sys].[database_principals]"),
            (
                "bracket-quoted cross-db catalog",
                "SELECT * FROM [master].[sys].[databases]",
            ),
            ("case-mixed catalog", "sElEcT * FrOm SyS.database_principals"),
            ("OPENQUERY", "SELECT * FROM OPENQUERY(LinkedServer, 'SELECT 1')"),
        ],
    )
    def test_rejected(self, name, sql):
        result = validate_sql(sql, dialect=DIALECT)
        assert not result.is_valid, f"{name} unexpectedly accepted"


class TestCteReferenceIsNeverMistakenForARealTable:
    """The critical bug found this prompt: `qualify_table_schema` schema-
    qualifying a CTE alias breaks every CTE query on a schema-configured
    connection (this project's own real HrAutomationDb case). Same root
    cause fixed across all four table-walking helpers."""

    CTE_SQL = (
        "WITH RecentOrders AS (SELECT SalesOrderNumber, OrderDate FROM FactInternetSales) "
        "SELECT * FROM RecentOrders"
    )

    def test_qualify_table_schema_leaves_the_cte_reference_unqualified(self):
        qualified = qualify_table_schema(self.CTE_SQL, "employee", dialect=DIALECT)
        assert "FROM RecentOrders" in qualified
        assert "employee.RecentOrders" not in qualified

    def test_qualify_table_schema_still_qualifies_the_real_table_inside_the_cte(self):
        qualified = qualify_table_schema(self.CTE_SQL, "employee", dialect=DIALECT)
        assert "employee.FactInternetSales" in qualified

    def test_find_unexpected_table_references_does_not_flag_the_cte_alias(self):
        unexpected = find_unexpected_table_references(
            self.CTE_SQL, {"FactInternetSales"}, dialect=DIALECT
        )
        assert unexpected == []

    def test_references_multiple_tables_is_false_for_a_single_real_table_cte(self):
        assert references_multiple_tables(self.CTE_SQL, dialect=DIALECT) is False

    def test_chained_cte_referencing_another_cte_is_also_excluded(self):
        sql = "WITH a AS (SELECT x FROM RealTable), b AS (SELECT x FROM a) " "SELECT * FROM b"
        qualified = qualify_table_schema(sql, "employee", dialect=DIALECT)
        assert "employee.a" not in qualified
        assert "employee.b" not in qualified
        assert "employee.RealTable" in qualified
        assert references_multiple_tables(sql, dialect=DIALECT) is False

    def test_restricted_column_check_still_catches_the_real_table_inside_the_cte(self):
        """The CTE fix must never reduce restricted-column detection --
        only remove the CTE alias's own spurious match."""
        flagged = find_restricted_column_references(
            self.CTE_SQL,
            restricted_columns={("FactInternetSales", "OrderDate")},
            known_tables={"FactInternetSales"},
            dialect=DIALECT,
        )
        assert flagged == [("FactInternetSales", "OrderDate")]

    def test_query_with_no_cte_is_completely_unaffected(self):
        """Regression guard: the fix must be a no-op for the overwhelming
        common case (no WITH clause at all)."""
        sql = "SELECT * FROM FactInternetSales"
        assert qualify_table_schema(sql, "employee", dialect=DIALECT) == (
            "SELECT * FROM employee.FactInternetSales"
        )


class TestEnforceRowLimitRecognizesFetchNextAsAnExistingCap:
    """The second bug found this prompt: `enforce_row_limit` only checked
    `exp.Limit`, never `exp.Fetch` -- so an explicit `FETCH NEXT 20 ROWS
    ONLY` was silently widened to `max_rows` on every call, even though 20
    was already well under the cap."""

    def test_small_existing_fetch_next_is_left_alone(self):
        sql = (
            "SELECT CustomerKey FROM DimCustomer ORDER BY CustomerKey "
            "OFFSET 10 ROWS FETCH NEXT 20 ROWS ONLY"
        )
        result = enforce_row_limit(sql, 1000, dialect=DIALECT)
        assert "FETCH NEXT 20 ROWS ONLY" in result or "FETCH FIRST 20 ROWS ONLY" in result

    def test_fetch_next_above_the_cap_is_still_clamped(self):
        sql = (
            "SELECT CustomerKey FROM DimCustomer ORDER BY CustomerKey "
            "OFFSET 10 ROWS FETCH NEXT 5000 ROWS ONLY"
        )
        result = enforce_row_limit(sql, 1000, dialect=DIALECT)
        assert "5000" not in result
        assert "1000" in result

    def test_top_clause_behavior_is_unchanged(self):
        """Regression guard: the exp.Fetch branch must not affect the
        pre-existing exp.Limit (TOP/LIMIT) handling at all."""
        sql = "SELECT TOP 10 CustomerKey FROM DimCustomer"
        assert enforce_row_limit(sql, 1000, dialect=DIALECT) == sql
