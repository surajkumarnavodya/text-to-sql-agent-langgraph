"""Regression tests for a confirmed audit finding: `db/value_sampling.py`
used to build `SELECT DISTINCT {column_name} FROM {table_name}` via raw,
unquoted string interpolation of live-introspected identifiers -- a
second-order SQL injection point if the connected database ever contains a
maliciously-named table/column (creatable via quoted-identifier DDL by
anyone with CREATE privileges on that schema).

These tests use a real (but connection-less) SQLAlchemy dialect object to
capture the exact SQL text `_sample_column` builds, so the assertions are
about the actual query text sent to the database, not just "it didn't
crash."
"""

from __future__ import annotations

from sqlalchemy.engine.default import DefaultDialect

from db.value_sampling import _sample_column


class _CapturingCursorResult:
    def fetchmany(self, n: int) -> list[tuple]:
        return [("ok",)]


class _CapturingConnection:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self._sink.append(str(sql))
        return _CapturingCursorResult()


class _CapturingEngine:
    """Captures the exact SQL text `_sample_column` builds and executes."""

    def __init__(self) -> None:
        self.executed_sql: list[str] = []
        self.dialect = DefaultDialect()

    def connect(self):
        return _CapturingConnection(self.executed_sql)


class TestMaliciousIdentifierQuoting:
    def test_malicious_table_name_is_quoted_not_interpolated_raw(self):
        engine = _CapturingEngine()
        _sample_column(engine, "Products; DROP TABLE Orders;--", "ProductLine")

        assert len(engine.executed_sql) == 1
        sql = engine.executed_sql[0]
        # The dangerous identifier must appear only inside a quoted
        # identifier, never as bare, breakout-capable SQL text.
        assert '"Products; DROP TABLE Orders;--"' in sql
        # And critically, it must not be possible to read this as two
        # separate statements/clauses -- the whole thing stays one quoted
        # token wherever it's referenced.
        assert sql.count('"Products; DROP TABLE Orders;--"') == 1

    def test_malicious_column_name_is_quoted_not_interpolated_raw(self):
        engine = _CapturingEngine()
        _sample_column(engine, "DimProduct", "Col; DELETE FROM DimProduct;--")

        sql = engine.executed_sql[0]
        assert '"Col; DELETE FROM DimProduct;--"' in sql

    def test_identifier_containing_a_literal_quote_is_escaped_not_a_breakout(self):
        """The sharpest case: an identifier containing the dialect's own
        quote character must have it escaped (doubled, for ANSI-quoted
        dialects), not left as a raw quote that could close the identifier
        early and let the rest of the string execute as SQL."""
        engine = _CapturingEngine()
        _sample_column(engine, 'x" ; DROP TABLE customers; --', "col")

        sql = engine.executed_sql[0]
        # SQLAlchemy's identifier_preparer doubles an embedded quote
        # character per the ANSI-SQL quoted-identifier escaping rule.
        assert '""' in sql
        # The statement must still be exactly one FROM-clause reference --
        # not a syntactically separate DROP TABLE statement appended after
        # an early-closed quote.
        assert sql.count("DROP TABLE customers") == 1

    def test_ordinary_identifiers_produce_the_expected_simple_query(self):
        """Sanity check: normal, unremarkable identifiers still produce a
        clean, readable query -- this hardening must not make the common
        case ugly or wrong."""
        engine = _CapturingEngine()
        _sample_column(engine, "DimProduct", "ProductLine")

        sql = engine.executed_sql[0]
        assert "SELECT DISTINCT" in sql.upper()
        assert "DimProduct" in sql
        assert "ProductLine" in sql
