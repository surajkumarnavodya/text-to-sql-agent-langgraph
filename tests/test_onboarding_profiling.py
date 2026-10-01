"""Unit tests for onboarding/profiling.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`).

Uses a **real** in-memory SQLite database (via `StaticPool` so every
`engine.connect()` call shares the same in-memory database -- a bare
`sqlite:///:memory:` gives each new connection its own, separate, empty
database, which would make every query here silently see no data) --
these are genuine SQL aggregate/sampling queries, not something a mock
can meaningfully stand in for. No network, no external service.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from db.schema_introspection import ColumnInfo, TableSchemaInfo
from onboarding.profiling import find_duplicate_keys, find_orphan_rows, profile_column


def _engine():
    return create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


class TestProfileColumnNumeric:
    def test_aggregates_and_percentiles(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (amount REAL)"))
            for i in range(20):
                conn.execute(text("INSERT INTO t VALUES (:v)"), {"v": float(i)})
            conn.commit()

        profile = profile_column(engine, "t", "amount", "REAL")

        assert profile.total_rows == 20
        assert profile.null_count == 0
        assert profile.distinct_count == 20
        assert profile.min_value == "0.0"
        assert profile.max_value == "19.0"
        assert profile.avg_value == 9.5
        assert profile.percentiles is not None
        assert profile.percentiles["p50"] == 9.5
        assert profile.top_values == ()

    def test_null_fraction(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (amount REAL)"))
            for v in [1.0, None, None, 4.0]:
                conn.execute(text("INSERT INTO t VALUES (:v)"), {"v": v})
            conn.commit()

        profile = profile_column(engine, "t", "amount", "REAL")

        assert profile.null_count == 2
        assert profile.null_fraction == 0.5


class TestProfileColumnCategorical:
    def test_low_cardinality_text_gets_top_values_not_percentiles(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (region TEXT)"))
            for r in ["East"] * 7 + ["West"] * 3:
                conn.execute(text("INSERT INTO t VALUES (:v)"), {"v": r})
            conn.commit()

        profile = profile_column(engine, "t", "region", "VARCHAR(50)")

        assert profile.percentiles is None
        assert dict(profile.top_values) == {"East": 7, "West": 3}

    def test_high_cardinality_text_gets_no_top_values(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (description TEXT)"))
            for i in range(50):
                conn.execute(text("INSERT INTO t VALUES (:v)"), {"v": f"unique-{i}"})
            conn.commit()

        profile = profile_column(engine, "t", "description", "VARCHAR(200)", sample_size=10)

        assert profile.top_values == ()


class TestProfileColumnFailsOpen:
    def test_returns_none_on_query_error(self):
        engine = _engine()  # no table created at all
        assert profile_column(engine, "missing_table", "col", "INTEGER") is None


class TestFindDuplicateKeys:
    def test_no_duplicates_in_a_real_primary_key(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            for i in range(5):
                conn.execute(text("INSERT INTO t VALUES (:i)"), {"i": i})
            conn.commit()

        tables = [
            TableSchemaInfo(
                table_name="t",
                columns=(ColumnInfo("id", "INTEGER", False, True),),
                foreign_keys=(),
                ddl="",
            )
        ]
        assert find_duplicate_keys(engine, tables) == []

    def test_detects_real_duplicates_in_a_unique_constrained_column(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER, email TEXT)"))
            conn.execute(text("INSERT INTO t VALUES (1, 'dup@x.com')"))
            conn.execute(text("INSERT INTO t VALUES (2, 'dup@x.com')"))
            conn.execute(text("INSERT INTO t VALUES (3, 'unique@x.com')"))
            conn.commit()

        tables = [
            TableSchemaInfo(
                table_name="t",
                columns=(
                    ColumnInfo("id", "INTEGER", False, True),
                    ColumnInfo("email", "TEXT", True, False),
                ),
                foreign_keys=(),
                ddl="",
                unique_constraints=(("email",),),
            )
        ]
        findings = find_duplicate_keys(engine, tables)

        assert len(findings) == 1
        assert findings[0].table_name == "t"
        assert findings[0].column_name == "email"
        assert findings[0].duplicate_group_count == 1

    def test_views_are_skipped(self):
        engine = _engine()
        tables = [
            TableSchemaInfo(
                table_name="v",
                columns=(ColumnInfo("id", "INTEGER", False, True),),
                foreign_keys=(),
                ddl="",
                is_view=True,
            )
        ]
        assert find_duplicate_keys(engine, tables) == []


class TestFindOrphanRows:
    def test_detects_a_real_orphan(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE customers (id INTEGER)"))
            conn.execute(text("CREATE TABLE orders (customer_id INTEGER)"))
            for cid in [1, 2, 3]:
                conn.execute(text("INSERT INTO customers VALUES (:i)"), {"i": cid})
            for cid in [1, 2, 99]:  # 99 has no matching customer
                conn.execute(text("INSERT INTO orders VALUES (:i)"), {"i": cid})
            conn.commit()

        finding = find_orphan_rows(engine, "orders", "customer_id", "customers", "id")

        assert finding is not None
        assert finding.sample_size == 3
        assert round(finding.orphan_fraction, 2) == round(1 / 3, 2)

    def test_no_orphans_when_every_value_matches(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE customers (id INTEGER)"))
            conn.execute(text("CREATE TABLE orders (customer_id INTEGER)"))
            conn.execute(text("INSERT INTO customers VALUES (1)"))
            conn.execute(text("INSERT INTO orders VALUES (1)"))
            conn.commit()

        finding = find_orphan_rows(engine, "orders", "customer_id", "customers", "id")

        assert finding.orphan_fraction == 0.0

    def test_returns_none_when_no_non_null_source_values_exist(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE customers (id INTEGER)"))
            conn.execute(text("CREATE TABLE orders (customer_id INTEGER)"))
            conn.execute(text("INSERT INTO orders VALUES (NULL)"))
            conn.commit()

        assert find_orphan_rows(engine, "orders", "customer_id", "customers", "id") is None

    def test_returns_none_on_query_error(self):
        engine = _engine()  # neither table exists
        assert find_orphan_rows(engine, "orders", "customer_id", "customers", "id") is None
