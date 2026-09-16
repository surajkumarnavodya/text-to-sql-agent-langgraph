"""Regression tests for two findings against `agent.sql_validator
.find_restricted_column_references` from the 2026 Phase 1 security review
(docs/PHASE1_BASELINE.md's security-review section):

1. **AUTHZ-01 (High)**: a wildcard projection (`SELECT *` / `SELECT t.*`)
   never mentions a restricted column by name, so the old name-only match
   silently let a restricted column through unflagged.
2. **AUTHZ-02 (High)**: the old check additionally required the restricted
   table to be part of the caller's `known_tables` argument -- in
   production that's `agent.nodes.validate_sql_node`'s attempt-scoped,
   RAG-*retrieved* schema subset for this one question, not the full
   configured schema. A restricted table the retriever didn't happen to
   surface for this attempt (but that the generated SQL genuinely
   references) was never checked at all.

Both are closed by the same change: matching is now grounded in the
tables the statement's own parsed `FROM`/`JOIN` clauses actually
reference, and a wildcard projection flags every restricted column of
every referenced table (since a named-column match can't see through a
wildcard). `known_tables` is still accepted for call-site compatibility
with `agent.nodes.validate_sql_node` (which also feeds it to the separate,
detection-only `find_unexpected_table_references`) but is no longer part
of the matching decision.

Kept separate from `tests/test_sql_validator_hardening.py` (that file
covers the SELECT-only allowlist and the dangerous-function denylist, an
unrelated bypass surface) and from `tests/test_nodes_security_wiring.py`
(node-level wiring of this same gate, not the matching function's own
bypass surface) -- matching this project's "one file per audit
finding/control" convention.
"""

from __future__ import annotations

from agent.sql_validator import find_restricted_column_references

_RESTRICTED = {("DimCustomer", "EmailAddress")}


class TestWildcardProjectionBypass:
    """AUTHZ-01: a `SELECT *`/`SELECT t.*` must be treated as if it could
    return any column of the tables it selects from."""

    def test_bare_star_is_flagged(self):
        hits = find_restricted_column_references(
            "SELECT * FROM DimCustomer",
            _RESTRICTED,
            known_tables={"DimCustomer"},
        )
        assert hits == [("DimCustomer", "EmailAddress")]

    def test_qualified_table_star_is_flagged(self):
        hits = find_restricted_column_references(
            "SELECT c.* FROM DimCustomer c",
            _RESTRICTED,
            known_tables={"DimCustomer"},
        )
        assert hits == [("DimCustomer", "EmailAddress")]

    def test_star_on_an_unrelated_table_is_not_flagged(self):
        """A wildcard is only dangerous against a table that actually has
        a restricted column -- sanity check against over-blocking."""
        hits = find_restricted_column_references(
            "SELECT * FROM DimProduct",
            _RESTRICTED,
            known_tables={"DimProduct"},
        )
        assert hits == []

    def test_count_star_is_not_a_projection_wildcard(self):
        """COUNT(*) is a function argument, not a column projection -- it
        must not itself trigger the wildcard rule (no restricted column
        could possibly be returned by a bare row count)."""
        hits = find_restricted_column_references(
            "SELECT COUNT(*) FROM DimCustomer",
            _RESTRICTED,
            known_tables={"DimCustomer"},
        )
        assert hits == []

    def test_explicit_columns_alongside_an_unrelated_table_star_is_unaffected(self):
        """A wildcard on one table in a join must not falsely flag a
        different, unrestricted table's explicit columns."""
        hits = find_restricted_column_references(
            "SELECT p.* FROM DimProduct p JOIN DimCustomer c ON p.CustomerKey = c.CustomerKey",
            _RESTRICTED,
            known_tables={"DimProduct", "DimCustomer"},
        )
        assert hits == [("DimCustomer", "EmailAddress")]


class TestKnownTablesScopingBypass:
    """AUTHZ-02: a restricted table referenced by the SQL must be checked
    even when the caller's `known_tables` (the attempt-scoped retrieved
    schema subset) doesn't include it."""

    def test_restricted_table_outside_known_tables_is_still_flagged(self):
        hits = find_restricted_column_references(
            "SELECT EmailAddress FROM DimCustomer",
            _RESTRICTED,
            known_tables=set(),  # empty: nothing was "retrieved" for this attempt
        )
        assert hits == [("DimCustomer", "EmailAddress")]

    def test_restricted_table_reached_via_join_outside_known_tables_is_flagged(self):
        hits = find_restricted_column_references(
            "SELECT o.OrderId, c.EmailAddress "
            "FROM Orders o JOIN DimCustomer c ON o.CustomerKey = c.CustomerKey",
            _RESTRICTED,
            known_tables={"Orders"},  # DimCustomer wasn't part of retrieval for this attempt
        )
        assert hits == [("DimCustomer", "EmailAddress")]

    def test_restricted_table_never_referenced_in_sql_is_not_flagged(self):
        """Unchanged behavior (still covered at the node-wiring level by
        tests/test_nodes_security_wiring.py's
        test_restricted_column_on_a_table_not_in_this_attempts_schema_is_not_flagged):
        a classification for a table the query never touches must never
        affect it, regardless of `known_tables`."""
        hits = find_restricted_column_references(
            "SELECT EmailAddress FROM DimCustomer",
            {("SomeOtherTable", "SSN")},
            known_tables={"DimCustomer"},
        )
        assert hits == []

    def test_unparseable_sql_returns_no_hits(self):
        """Matches validate_sql's own fail-safe-to-the-next-layer posture --
        this function is a secondary gate on already-validated SQL, not the
        parser's own error path."""
        hits = find_restricted_column_references(
            "SELECT SELECT FROM FROM",
            _RESTRICTED,
            known_tables={"DimCustomer"},
        )
        assert hits == []
