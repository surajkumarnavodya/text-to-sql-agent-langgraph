"""Unit tests for embeddings/schema_indexer.py.

`TestRefreshSchemaIndex` is fully mocked -- no real database, ChromaDB, or
embedding model is ever contacted. `refresh_schema_index` is a thin
composition of three already independently-owned steps
(`introspect_schema`, `attach_sample_values`, `build_index`); these tests
verify it wires them together in the right order with the right arguments,
since that wiring used to be duplicated (and untested as a unit) across
multiple call sites.

The remaining classes (added for Prompt 06,
`06_DATABASE_DISCOVERY_CONTRACT.md`) are `build_index`'s first-ever direct
unit tests: the Chroma client/collection is mocked (matching every other
Chroma-backed module's own test convention in this project -- see
`embeddings/golden_examples.py`/`feedback/store.py`'s test files), but the
discovery-manifest JSON file is real file I/O against `tmp_path`, since
that's the actual persisted state this prompt's incrementality logic
reads and writes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from config.settings import Settings
from db.schema_introspection import TableSchemaInfo
from embeddings.schema_indexer import build_index, get_last_discovery_diff, refresh_schema_index


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


def _table(name: str, ddl_body: str = "") -> TableSchemaInfo:
    return TableSchemaInfo(
        table_name=name, columns=(), foreign_keys=(), ddl=f"CREATE TABLE {name} ({ddl_body});"
    )


def _mock_chroma(monkeypatch) -> MagicMock:
    """Mocks the Chroma client/collection layer -- build_index's own tests
    never contact a real ChromaDB, matching every other Chroma-backed
    module's test convention in this project."""
    collection = MagicMock()
    monkeypatch.setattr("embeddings.schema_indexer.get_chroma_client", lambda settings: MagicMock())
    monkeypatch.setattr(
        "embeddings.schema_indexer.get_collection",
        lambda client, settings, db_name: collection,
    )
    return collection


class TestBuildIndexFirstBuild:
    def test_first_build_upserts_every_table_and_writes_a_manifest(self, tmp_path, monkeypatch):
        collection = _mock_chroma(monkeypatch)
        settings = Settings(chroma_persist_dir=tmp_path)

        tables = [_table("orders"), _table("customers")]
        count = build_index(tables, "salesdb", settings=settings)

        assert count == 2
        collection.upsert.assert_called_once()
        ids = collection.upsert.call_args.kwargs["ids"]
        assert set(ids) == {"orders", "customers"}
        assert (tmp_path / ".schema_manifest__salesdb.json").exists()

    def test_first_build_diff_reports_every_table_as_added(self, tmp_path, monkeypatch):
        """A genuine first build has no previous state to diff against, so
        every discovered table legitimately counts as "added" -- the
        useful signal for an onboarding UI (see `SchemaDiscoveryDiff`'s own
        docstring), not a quirk to suppress."""
        _mock_chroma(monkeypatch)
        settings = Settings(chroma_persist_dir=tmp_path)

        build_index([_table("orders"), _table("customers")], "salesdb", settings=settings)
        diff = get_last_discovery_diff("salesdb", settings=settings)

        assert diff.added_tables == ("customers", "orders")
        assert diff.removed_tables == ()
        assert diff.changed_tables == ()
        assert diff.last_discovered_at is not None


class TestBuildIndexSkipsWhenUnchanged:
    def test_second_call_with_identical_tables_does_not_re_embed(self, tmp_path, monkeypatch):
        collection = _mock_chroma(monkeypatch)
        settings = Settings(chroma_persist_dir=tmp_path)
        tables = [_table("orders"), _table("customers")]

        build_index(tables, "salesdb", settings=settings)
        collection.upsert.reset_mock()

        count = build_index(tables, "salesdb", settings=settings)

        assert count == 2
        collection.upsert.assert_not_called()
        collection.delete.assert_not_called()


class TestBuildIndexIncrementalChange:
    def test_only_added_and_changed_tables_are_upserted(self, tmp_path, monkeypatch):
        collection = _mock_chroma(monkeypatch)
        settings = Settings(chroma_persist_dir=tmp_path)

        build_index(
            [_table("orders"), _table("customers"), _table("products")],
            "salesdb",
            settings=settings,
        )
        collection.upsert.reset_mock()

        # "orders" changed (different ddl body), "customers" unchanged,
        # "products" removed, "invoices" newly added.
        new_tables = [
            _table("orders", ddl_body="id INT, total DECIMAL"),
            _table("customers"),
            _table("invoices"),
        ]
        build_index(new_tables, "salesdb", settings=settings)

        collection.upsert.assert_called_once()
        upserted_ids = set(collection.upsert.call_args.kwargs["ids"])
        assert upserted_ids == {"orders", "invoices"}

    def test_removed_table_is_deleted_not_left_behind(self, tmp_path, monkeypatch):
        collection = _mock_chroma(monkeypatch)
        settings = Settings(chroma_persist_dir=tmp_path)

        build_index([_table("orders"), _table("products")], "salesdb", settings=settings)
        build_index([_table("orders")], "salesdb", settings=settings)

        collection.delete.assert_called_once_with(ids=["products"])

    def test_incremental_path_never_deletes_the_whole_collection(self, tmp_path, monkeypatch):
        """The real point of this prompt's change: a small change must not
        pay for a full collection delete+recreate."""
        _mock_chroma(monkeypatch)
        client = MagicMock()
        monkeypatch.setattr("embeddings.schema_indexer.get_chroma_client", lambda settings: client)
        settings = Settings(chroma_persist_dir=tmp_path)

        build_index([_table("orders")], "salesdb", settings=settings)
        client.delete_collection.reset_mock()

        build_index([_table("orders"), _table("customers")], "salesdb", settings=settings)

        client.delete_collection.assert_not_called()

    def test_diff_reflects_added_removed_changed_after_an_incremental_build(
        self, tmp_path, monkeypatch
    ):
        _mock_chroma(monkeypatch)
        settings = Settings(chroma_persist_dir=tmp_path)

        build_index([_table("orders"), _table("products")], "salesdb", settings=settings)
        build_index(
            [_table("orders", ddl_body="changed"), _table("invoices")],
            "salesdb",
            settings=settings,
        )

        diff = get_last_discovery_diff("salesdb", settings=settings)
        assert diff.added_tables == ("invoices",)
        assert diff.removed_tables == ("products",)
        assert diff.changed_tables == ("orders",)


class TestBuildIndexForced:
    def test_force_true_does_a_full_rebuild_even_when_nothing_changed(self, tmp_path, monkeypatch):
        collection = _mock_chroma(monkeypatch)
        client = MagicMock()
        monkeypatch.setattr("embeddings.schema_indexer.get_chroma_client", lambda settings: client)
        settings = Settings(chroma_persist_dir=tmp_path)
        tables = [_table("orders"), _table("customers")]

        build_index(tables, "salesdb", settings=settings)
        collection.upsert.reset_mock()
        client.delete_collection.reset_mock()

        build_index(tables, "salesdb", settings=settings, force=True)

        client.delete_collection.assert_called_once()
        collection.upsert.assert_called_once()
        assert set(collection.upsert.call_args.kwargs["ids"]) == {"orders", "customers"}


class TestGetLastDiscoveryDiff:
    def test_returns_none_when_database_was_never_indexed(self, tmp_path):
        settings = Settings(chroma_persist_dir=tmp_path)
        assert get_last_discovery_diff("neverindexed", settings=settings) is None

    def test_returns_none_on_a_corrupt_manifest_file(self, tmp_path):
        settings = Settings(chroma_persist_dir=tmp_path)
        (tmp_path / ".schema_manifest__salesdb.json").write_text("not json", encoding="utf-8")
        assert get_last_discovery_diff("salesdb", settings=settings) is None


class TestBuildIndexMultiDatabaseIsolation:
    def test_two_databases_never_share_manifests_or_collections(self, tmp_path, monkeypatch):
        collections: dict[str, MagicMock] = {}
        monkeypatch.setattr(
            "embeddings.schema_indexer.get_chroma_client", lambda settings: MagicMock()
        )

        def _get_collection(client, settings, db_name):
            return collections.setdefault(db_name, MagicMock())

        monkeypatch.setattr("embeddings.schema_indexer.get_collection", _get_collection)
        settings = Settings(chroma_persist_dir=tmp_path)

        build_index([_table("orders")], "dbA", settings=settings)
        build_index([_table("products")], "dbB", settings=settings)

        collections["dbA"].upsert.assert_called_once()
        assert set(collections["dbA"].upsert.call_args.kwargs["ids"]) == {"orders"}
        collections["dbB"].upsert.assert_called_once()
        assert set(collections["dbB"].upsert.call_args.kwargs["ids"]) == {"products"}
        assert (tmp_path / ".schema_manifest__dbA.json").exists()
        assert (tmp_path / ".schema_manifest__dbB.json").exists()

        # Changing dbA must never affect dbB's own manifest/collection.
        build_index([_table("orders"), _table("new_table")], "dbA", settings=settings)
        collections["dbB"].upsert.assert_called_once()  # still just the one call from before
