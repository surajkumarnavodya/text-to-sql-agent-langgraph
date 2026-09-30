"""Unit tests for db/schema_introspection.py.

Mocks `sqlalchemy.inspect()`'s return value (an Inspector-like object) so
these tests never touch a real database -- they check that
`introspect_schema` correctly turns Inspector output into `TableSchemaInfo`
objects (including PK/FK rendering into the synthesized DDL), and that
`get_schema_fingerprint` is deterministic and order-independent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import Integer, Numeric, String

from db.schema_introspection import TableSchemaInfo, get_schema_fingerprint, introspect_schema


def _mock_inspector() -> MagicMock:
    inspector = MagicMock()
    inspector.get_table_names.return_value = ["orders", "customers"]

    columns_by_table = {
        "orders": [
            {"name": "order_id", "type": "INTEGER", "nullable": False},
            {"name": "customer_id", "type": "INTEGER", "nullable": False},
            {"name": "status", "type": "VARCHAR", "nullable": True},
        ],
        "customers": [
            {"name": "customer_id", "type": "INTEGER", "nullable": False},
            {"name": "email", "type": "VARCHAR", "nullable": False},
        ],
    }
    pk_by_table = {
        "orders": {"constrained_columns": ["order_id"]},
        "customers": {"constrained_columns": ["customer_id"]},
    }
    fks_by_table = {
        "orders": [
            {
                "constrained_columns": ["customer_id"],
                "referred_table": "customers",
                "referred_columns": ["customer_id"],
            }
        ],
        "customers": [],
    }

    inspector.get_columns.side_effect = lambda table_name, schema=None: columns_by_table[table_name]
    inspector.get_pk_constraint.side_effect = lambda table_name, schema=None: pk_by_table[
        table_name
    ]
    inspector.get_foreign_keys.side_effect = lambda table_name, schema=None: fks_by_table[
        table_name
    ]
    return inspector


class TestIntrospectSchema:
    def test_returns_tables_sorted_by_name(self, monkeypatch):
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: _mock_inspector())

        tables = introspect_schema(engine=MagicMock(), schema=None)

        assert [t.table_name for t in tables] == ["customers", "orders"]

    def test_marks_primary_key_columns(self, monkeypatch):
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: _mock_inspector())

        tables = introspect_schema(engine=MagicMock())
        customers = next(t for t in tables if t.table_name == "customers")

        pk_column = next(c for c in customers.columns if c.name == "customer_id")
        assert pk_column.is_primary_key
        non_pk_column = next(c for c in customers.columns if c.name == "email")
        assert not non_pk_column.is_primary_key

    def test_captures_foreign_keys(self, monkeypatch):
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: _mock_inspector())

        tables = introspect_schema(engine=MagicMock())
        orders = next(t for t in tables if t.table_name == "orders")

        assert len(orders.foreign_keys) == 1
        fk = orders.foreign_keys[0]
        assert fk.constrained_columns == ("customer_id",)
        assert fk.referred_table == "customers"
        assert fk.referred_columns == ("customer_id",)

    def test_renders_ddl_with_primary_and_foreign_keys(self, monkeypatch):
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: _mock_inspector())

        tables = introspect_schema(engine=MagicMock())
        orders = next(t for t in tables if t.table_name == "orders")

        assert "CREATE TABLE orders" in orders.ddl
        assert "PRIMARY KEY" in orders.ddl
        assert "FOREIGN KEY (customer_id) REFERENCES customers (customer_id)" in orders.ddl

    def test_ignores_foreign_keys_missing_referred_table(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_foreign_keys.side_effect = lambda table_name, schema=None: (
            [{"constrained_columns": [], "referred_table": None, "referred_columns": []}]
            if table_name == "orders"
            else []
        )
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())
        orders = next(t for t in tables if t.table_name == "orders")

        assert orders.foreign_keys == ()

    def test_passes_schema_through_to_inspector_calls(self, monkeypatch):
        inspector = _mock_inspector()
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        introspect_schema(engine=MagicMock(), schema="reporting")

        inspector.get_table_names.assert_called_once_with(schema="reporting")


class TestIntrospectSchemaDiscoversViews:
    """Prompt 06 (`06_DATABASE_DISCOVERY_CONTRACT.md`): views must be
    discovered alongside tables, tagged `is_view=True`, and rendered as
    `CREATE VIEW` rather than `CREATE TABLE`."""

    def test_views_are_included_and_tagged(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_view_names.return_value = ["customer_summary"]
        inspector.get_columns.side_effect = lambda table_name, schema=None: {
            "orders": [{"name": "order_id", "type": "INTEGER", "nullable": False}],
            "customers": [{"name": "customer_id", "type": "INTEGER", "nullable": False}],
            "customer_summary": [{"name": "customer_id", "type": "INTEGER", "nullable": True}],
        }[table_name]
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())

        view = next(t for t in tables if t.table_name == "customer_summary")
        assert view.is_view is True
        assert "CREATE VIEW customer_summary" in view.ddl
        table = next(t for t in tables if t.table_name == "orders")
        assert table.is_view is False
        assert "CREATE TABLE orders" in table.ddl

    def test_views_skip_pk_and_fk_reflection(self, monkeypatch):
        """Views have no PK/FK constraints -- get_pk_constraint/
        get_foreign_keys must never be called for a view (some dialects
        raise if you try)."""
        inspector = _mock_inspector()
        inspector.get_view_names.return_value = ["customer_summary"]
        inspector.get_columns.side_effect = lambda table_name, schema=None: {
            "orders": [{"name": "order_id", "type": "INTEGER", "nullable": False}],
            "customers": [{"name": "customer_id", "type": "INTEGER", "nullable": False}],
            "customer_summary": [{"name": "customer_id", "type": "INTEGER", "nullable": True}],
        }[table_name]
        inspector.get_pk_constraint.side_effect = AssertionError("must not be called for a view")
        inspector.get_foreign_keys.side_effect = AssertionError("must not be called for a view")
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        # Restore the real per-table side effects for the two real tables,
        # only the view's own calls must avoid PK/FK reflection.
        pk_by_table = {
            "orders": {"constrained_columns": ["order_id"]},
            "customers": {"constrained_columns": ["customer_id"]},
        }
        fks_by_table = {"orders": [], "customers": []}
        inspector.get_pk_constraint.side_effect = lambda table_name, schema=None: pk_by_table[
            table_name
        ]
        inspector.get_foreign_keys.side_effect = lambda table_name, schema=None: fks_by_table[
            table_name
        ]

        tables = introspect_schema(engine=MagicMock())
        view = next(t for t in tables if t.table_name == "customer_summary")
        assert view.foreign_keys == ()
        assert not any(c.is_primary_key for c in view.columns)

    def test_dialect_without_view_reflection_yields_no_views_not_an_error(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_view_names.side_effect = NotImplementedError
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())
        assert all(not t.is_view for t in tables)


class TestIntrospectSchemaColumnMetadata:
    """Prompt 06: default/computed/identity/length/precision/scale, all
    sourced from data the Inspector already returns per column."""

    def test_length_precision_scale_from_the_type_object(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_columns.side_effect = lambda table_name, schema=None: {
            "orders": [
                {"name": "notes", "type": String(50), "nullable": True},
                {"name": "total", "type": Numeric(18, 2), "nullable": True},
                {"name": "qty", "type": Integer(), "nullable": True},
            ],
            "customers": [],
        }[table_name]
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())
        orders = next(t for t in tables if t.table_name == "orders")
        notes = next(c for c in orders.columns if c.name == "notes")
        total = next(c for c in orders.columns if c.name == "total")
        qty = next(c for c in orders.columns if c.name == "qty")

        assert notes.length == 50
        assert total.precision == 18 and total.scale == 2
        assert qty.length is None and qty.precision is None and qty.scale is None

    def test_default_computed_identity_extracted_when_present(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_columns.side_effect = lambda table_name, schema=None: {
            "orders": [
                {
                    "name": "order_id",
                    "type": Integer(),
                    "nullable": False,
                    "identity": {"start": 1, "increment": 1},
                },
                {
                    "name": "total_with_tax",
                    "type": Numeric(18, 2),
                    "nullable": True,
                    "computed": {"sqltext": "total * 1.1", "persisted": True},
                },
                {
                    "name": "status",
                    "type": String(20),
                    "nullable": True,
                    "default": "'pending'",
                },
            ],
            "customers": [],
        }[table_name]
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())
        orders = next(t for t in tables if t.table_name == "orders")
        order_id = next(c for c in orders.columns if c.name == "order_id")
        total_with_tax = next(c for c in orders.columns if c.name == "total_with_tax")
        status = next(c for c in orders.columns if c.name == "status")

        assert order_id.is_identity is True
        assert total_with_tax.is_computed is True
        assert status.default == "'pending'"
        # Sanity check: absence of these keys must not be mistaken for presence.
        assert order_id.is_computed is False
        assert status.is_identity is False

    def test_ddl_renders_the_new_annotations(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_columns.side_effect = lambda table_name, schema=None: {
            "orders": [
                {
                    "name": "order_id",
                    "type": Integer(),
                    "nullable": False,
                    "identity": {"start": 1},
                },
                {
                    "name": "status",
                    "type": String(20),
                    "nullable": True,
                    "default": "'pending'",
                },
            ],
            "customers": [],
        }[table_name]
        inspector.get_pk_constraint.side_effect = lambda table_name, schema=None: (
            {"constrained_columns": ["order_id"]} if table_name == "orders" else {}
        )
        inspector.get_foreign_keys.side_effect = lambda table_name, schema=None: []
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())
        orders = next(t for t in tables if t.table_name == "orders")

        assert "IDENTITY" in orders.ddl
        assert "DEFAULT 'pending'" in orders.ddl


class TestIntrospectSchemaPerObjectFailureIsolation:
    """Prompt 06: one table/view's own introspection failing (a
    permissions issue, a dialect quirk) must not abort discovery for
    every other object in the same database."""

    def test_one_bad_table_does_not_block_the_rest(self, monkeypatch):
        inspector = _mock_inspector()

        def _raising_get_columns(table_name, schema=None):
            if table_name == "orders":
                raise PermissionError("no SELECT on orders")
            return {"customers": [{"name": "customer_id", "type": "INTEGER", "nullable": False}]}[
                table_name
            ]

        inspector.get_columns.side_effect = _raising_get_columns
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())

        assert [t.table_name for t in tables] == ["customers"]

    def test_returns_empty_list_not_raises_when_every_object_fails(self, monkeypatch):
        inspector = _mock_inspector()
        inspector.get_columns.side_effect = RuntimeError("boom")
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)

        tables = introspect_schema(engine=MagicMock())

        assert tables == []


class TestIntrospectSchemaIncludeRowCounts:
    """Prompt 06: `include_row_counts` is opt-in, default off -- must not
    issue the extra per-table catalog query unless explicitly requested,
    and must thread through the engine's own dialect name as db_type."""

    def test_off_by_default_no_extra_query(self, monkeypatch):
        inspector = _mock_inspector()
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)
        called = []
        monkeypatch.setattr(
            "db.schema_introspection.estimate_row_count",
            lambda *a, **k: called.append(1) or 999,
        )

        tables = introspect_schema(engine=MagicMock())

        assert called == []
        assert all(t.row_count_estimate is None for t in tables)

    def test_opt_in_populates_row_count_estimate(self, monkeypatch):
        inspector = _mock_inspector()
        monkeypatch.setattr("db.schema_introspection.inspect", lambda engine: inspector)
        captured = []

        def _fake_estimate(engine, table_name, schema, db_type):
            captured.append((table_name, schema, db_type))
            return 42

        monkeypatch.setattr("db.schema_introspection.estimate_row_count", _fake_estimate)

        engine = MagicMock()
        engine.dialect.name = "mssql"
        tables = introspect_schema(engine=engine, schema="dbo", include_row_counts=True)

        assert all(t.row_count_estimate == 42 for t in tables)
        assert all(schema == "dbo" and db_type == "mssql" for _, schema, db_type in captured)
        assert {name for name, _, _ in captured} == {"orders", "customers"}


class TestSchemaFingerprint:
    def test_same_tables_produce_same_hash(self):
        tables = [
            TableSchemaInfo(table_name="a", columns=(), foreign_keys=(), ddl="CREATE TABLE a ();")
        ]
        assert get_schema_fingerprint(tables) == get_schema_fingerprint(tables)

    def test_different_ddl_produces_different_hash(self):
        t1 = [
            TableSchemaInfo(
                table_name="a", columns=(), foreign_keys=(), ddl="CREATE TABLE a (x INT);"
            )
        ]
        t2 = [
            TableSchemaInfo(
                table_name="a", columns=(), foreign_keys=(), ddl="CREATE TABLE a (x INT, y INT);"
            )
        ]
        assert get_schema_fingerprint(t1) != get_schema_fingerprint(t2)

    def test_fingerprint_is_order_independent(self):
        a = TableSchemaInfo(table_name="a", columns=(), foreign_keys=(), ddl="CREATE TABLE a ();")
        b = TableSchemaInfo(table_name="b", columns=(), foreign_keys=(), ddl="CREATE TABLE b ();")
        assert get_schema_fingerprint([a, b]) == get_schema_fingerprint([b, a])
