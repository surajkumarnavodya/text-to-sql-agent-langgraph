"""Unit tests for embeddings/schema_indexer.py's `refresh_schema_index`.

Fully mocked -- no real database, ChromaDB, or embedding model is ever
contacted. `refresh_schema_index` is a thin composition of three already
independently-owned steps (`introspect_schema`, `attach_sample_values`,
`build_index`); these tests verify it wires them together in the right order
with the right arguments, since that wiring used to be duplicated (and
untested as a unit) in both `scripts/build_embeddings.py` and `ui/app.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from embeddings.schema_indexer import refresh_schema_index


class TestRefreshSchemaIndex:
    def test_wires_introspect_sample_and_index_in_order(self, monkeypatch):
        call_order: list[str] = []

        introspected = [MagicMock(table_name="orders")]
        sampled = [MagicMock(table_name="orders")]

        def _introspect(engine, schema=None):
            call_order.append("introspect")
            assert schema == "sales"
            return introspected

        def _sample(engine, tables):
            call_order.append("sample")
            assert tables is introspected
            return sampled

        def _build(tables, db_name, settings=None, force=False, fingerprint_tables=None):
            call_order.append("build")
            assert tables is sampled
            assert fingerprint_tables is introspected
            assert db_name == "salesdb"
            return len(tables)

        monkeypatch.setattr("embeddings.schema_indexer.introspect_schema", _introspect)
        monkeypatch.setattr("embeddings.schema_indexer.attach_sample_values", _sample)
        monkeypatch.setattr("embeddings.schema_indexer.build_index", _build)
        monkeypatch.setattr(
            "embeddings.schema_indexer.get_connection",
            lambda settings, db_name: MagicMock(db_schema="sales"),
        )

        settings = MagicMock()
        engine = MagicMock()

        result = refresh_schema_index(engine, "salesdb", settings=settings)

        assert call_order == ["introspect", "sample", "build"]
        assert result is sampled

    def test_passes_force_through_to_build_index(self, monkeypatch):
        monkeypatch.setattr(
            "embeddings.schema_indexer.introspect_schema", lambda engine, schema=None: []
        )
        monkeypatch.setattr(
            "embeddings.schema_indexer.attach_sample_values", lambda engine, tables: tables
        )
        monkeypatch.setattr(
            "embeddings.schema_indexer.get_connection",
            lambda settings, db_name: MagicMock(db_schema=None),
        )
        captured: dict = {}

        def _build(tables, db_name, settings=None, force=False, fingerprint_tables=None):
            captured["force"] = force
            return 0

        monkeypatch.setattr("embeddings.schema_indexer.build_index", _build)

        settings = MagicMock()
        refresh_schema_index(MagicMock(), "salesdb", settings=settings, force=True)

        assert captured["force"] is True

    def test_defaults_settings_when_not_passed(self, monkeypatch):
        fake_settings = MagicMock()
        monkeypatch.setattr("embeddings.schema_indexer.get_settings", lambda: fake_settings)
        monkeypatch.setattr(
            "embeddings.schema_indexer.introspect_schema", lambda engine, schema=None: []
        )
        monkeypatch.setattr(
            "embeddings.schema_indexer.attach_sample_values", lambda engine, tables: tables
        )
        monkeypatch.setattr(
            "embeddings.schema_indexer.get_connection",
            lambda settings, db_name: MagicMock(db_schema=None),
        )
        used_settings: dict = {}

        def _build(tables, db_name, settings=None, force=False, fingerprint_tables=None):
            used_settings["settings"] = settings
            return 0

        monkeypatch.setattr("embeddings.schema_indexer.build_index", _build)

        refresh_schema_index(MagicMock(), "salesdb")

        assert used_settings["settings"] is fake_settings
