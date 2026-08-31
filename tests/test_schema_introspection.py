"""Unit tests for db/schema_introspection.py.

Mocks `sqlalchemy.inspect()`'s return value (an Inspector-like object) so
these tests never touch a real database -- they check that
`introspect_schema` correctly turns Inspector output into `TableSchemaInfo`
objects (including PK/FK rendering into the synthesized DDL), and that
`get_schema_fingerprint` is deterministic and order-independent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
