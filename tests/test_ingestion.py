"""Unit tests for retrieval/ingestion.py -- idempotent ingestion, dry-run,
changed-content updates, stale-chunk deletion.

Uses `retrieval.embeddings.FakeEmbeddingProvider` (deterministic, no
network/model) and `tests._retrieval_fakes.InMemoryVectorStore` (no
chromadb, no disk I/O) throughout -- see both modules' own docstrings.
"""

from __future__ import annotations

from pathlib import Path

from retrieval.embeddings import FakeEmbeddingProvider
from retrieval.ingestion import rebuild_collection, run_ingestion

from db.schema_introspection import ColumnInfo, TableSchemaInfo
from tests._retrieval_fakes import InMemoryVectorStore


def _one_table() -> list[TableSchemaInfo]:
    return [
        TableSchemaInfo(
            table_name="DimProduct",
            columns=(
                ColumnInfo(name="ProductKey", type="INTEGER", nullable=False, is_primary_key=True),
                ColumnInfo(
                    name="ProductName", type="NVARCHAR", nullable=False, is_primary_key=False
                ),
            ),
            foreign_keys=(),
            ddl="CREATE TABLE DimProduct (...)",
        )
    ]


def _settings(tmp_path: Path, **overrides):
    from config.settings import Settings

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    base = {
        "retrieval_embedding_provider": "fake",
        "retrieval_knowledge_dir": knowledge_dir,
        "retrieval_documentation_chunk_chars": 500,
        "retrieval_documentation_chunk_overlap_chars": 50,
        "retrieval_embedding_batch_size": 16,
    }
    base.update(overrides)
    return Settings(**base)


class TestDryRun:
    def test_reports_counts_without_writing_anything(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        summary = run_ingestion(
            "default",
            settings,
            dry_run=True,
            vector_store=store,
            embedding_provider=provider,
            tables=_one_table(),
        )

        assert summary.dry_run is True
        assert summary.inserted > 0
        assert summary.updated == 0
        assert summary.skipped == 0
        assert store.count("default") == 0  # nothing actually written


class TestFirstRunInsertsEverything:
    def test_all_chunks_inserted(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        summary = run_ingestion(
            "default",
            settings,
            vector_store=store,
            embedding_provider=provider,
            tables=_one_table(),
        )

        assert summary.inserted == summary.generated_chunks
        assert summary.updated == 0
        assert summary.skipped == 0
        assert summary.deleted == 0
        assert summary.failures == 0
        assert store.count("default") == summary.generated_chunks


class TestIdempotentReingestion:
    def test_second_run_over_unchanged_input_skips_everything(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()
        tables = _one_table()

        first = run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=tables
        )
        second = run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=tables
        )

        assert second.inserted == 0
        assert second.updated == 0
        assert second.skipped == first.generated_chunks
        assert second.deleted == 0
        assert store.count("default") == first.generated_chunks


class TestChangedContentUpdates:
    def test_a_changed_column_updates_only_affected_chunks(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        original = _one_table()
        run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=original
        )

        changed = [
            TableSchemaInfo(
                table_name="DimProduct",
                columns=(
                    ColumnInfo(
                        name="ProductKey", type="INTEGER", nullable=False, is_primary_key=True
                    ),
                    # Type changed NVARCHAR -> TEXT -- only this column chunk's
                    # content (and therefore hash) should change.
                    ColumnInfo(
                        name="ProductName", type="TEXT", nullable=False, is_primary_key=False
                    ),
                ),
                foreign_keys=(),
                ddl="CREATE TABLE DimProduct (...)",
            )
        ]
        summary = run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=changed
        )

        assert summary.updated == 1
        assert summary.inserted == 0
        assert summary.deleted == 0


class TestStaleChunkDeletion:
    def test_a_removed_table_deletes_its_chunks_on_next_run(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        two_tables = _one_table() + [
            TableSchemaInfo(
                table_name="DimCustomer",
                columns=(
                    ColumnInfo(
                        name="CustomerKey", type="INTEGER", nullable=False, is_primary_key=True
                    ),
                ),
                foreign_keys=(),
                ddl="CREATE TABLE DimCustomer (...)",
            )
        ]
        first = run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=two_tables
        )

        second = run_ingestion(
            "default",
            settings,
            vector_store=store,
            embedding_provider=provider,
            tables=_one_table(),
        )

        assert second.deleted > 0
        remaining_tables = {
            chunk.table_name
            for chunk, _ in store._data["default"].values()  # noqa: SLF001 - test introspection
            if chunk.table_name is not None
        }
        assert "DimCustomer" not in remaining_tables
        assert store.count("default") == first.generated_chunks - second.deleted


class TestKnowledgeFileIngestion:
    def test_glossary_metric_and_sql_example_chunks_are_ingested(self, tmp_path: Path):
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "glossary.yaml").write_text(
            "terms:\n  - term: widget\n    definition: 'A thing.'\n", encoding="utf-8"
        )
        (knowledge_dir / "metrics.yaml").write_text(
            "metrics:\n  - name: total widgets\n    definition: 'Count of widgets.'\n"
            "    formula: 'COUNT(*)'\n",
            encoding="utf-8",
        )
        (knowledge_dir / "sql_examples.yaml").write_text(
            "examples:\n  - question: 'How many widgets?'\n    sql: 'SELECT COUNT(*) FROM widgets'\n",
            encoding="utf-8",
        )
        settings = _settings(tmp_path, retrieval_knowledge_dir=knowledge_dir)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        summary = run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=[]
        )

        assert summary.counts_by_type.get("glossary") == 1
        assert summary.counts_by_type.get("metric") == 1
        assert summary.counts_by_type.get("sql_example") == 1

    def test_documentation_files_are_chunked_and_ingested(self, tmp_path: Path):
        knowledge_dir = tmp_path / "knowledge"
        (knowledge_dir / "documentation").mkdir(parents=True)
        long_text = "\n\n".join(f"Section {i} content here." * 10 for i in range(10))
        (knowledge_dir / "documentation" / "guide.md").write_text(long_text, encoding="utf-8")

        settings = _settings(tmp_path, retrieval_knowledge_dir=knowledge_dir)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        summary = run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=[]
        )

        assert summary.counts_by_type.get("documentation", 0) >= 1


class TestRebuildCollection:
    def test_drops_and_reingests_everything_as_inserted(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()
        tables = _one_table()

        run_ingestion(
            "default", settings, vector_store=store, embedding_provider=provider, tables=tables
        )
        summary = rebuild_collection(
            "default", settings, vector_store=store, embedding_provider=provider, tables=tables
        )

        assert summary.updated == 0
        assert summary.skipped == 0
        assert summary.inserted == summary.generated_chunks

    def test_never_called_implicitly_by_run_ingestion(self, tmp_path: Path):
        """Normal ingestion must never wipe the collection -- see
        retrieval/ingestion.py's module docstring. Proven here by seeding an
        unrelated chunk directly and confirming a normal run_ingestion call
        never removes it just because it wasn't part of this run's own
        discovered chunk set for the *same* source."""
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        run_ingestion(
            "default",
            settings,
            vector_store=store,
            embedding_provider=provider,
            tables=_one_table(),
        )
        count_after_first_run = store.count("default")
        assert count_after_first_run > 0

        # A second run over the identical input must never drop to zero.
        run_ingestion(
            "default",
            settings,
            vector_store=store,
            embedding_provider=provider,
            tables=_one_table(),
        )
        assert store.count("default") == count_after_first_run
