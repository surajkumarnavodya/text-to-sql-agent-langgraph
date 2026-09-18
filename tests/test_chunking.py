"""Unit tests for retrieval/models.py and retrieval/chunking.py.

Fully offline -- pure data transformation, no vector store, no embedding
model, no database connection (see retrieval/chunking.py's own docstring).
"""

from __future__ import annotations

from pathlib import Path

from retrieval.chunking import (
    column_chunks_from_schema,
    documentation_chunks_from_text,
    glossary_chunks_from_yaml,
    load_documentation_files,
    metric_chunks_from_yaml,
    relationship_chunks_from_schema,
    sql_example_chunks_from_yaml,
    table_chunks_from_schema,
)
from retrieval.models import ChunkType, Sensitivity, compute_content_hash, make_chunk_id

from db.schema_introspection import ColumnInfo, ForeignKeyInfo, TableSchemaInfo


def _sample_tables() -> list[TableSchemaInfo]:
    product = TableSchemaInfo(
        table_name="DimProduct",
        columns=(
            ColumnInfo(name="ProductKey", type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="ProductName", type="NVARCHAR", nullable=False, is_primary_key=False),
            ColumnInfo(name="ListPrice", type="DECIMAL", nullable=True, is_primary_key=False),
        ),
        foreign_keys=(),
        ddl="CREATE TABLE DimProduct (...)",
    )
    sales = TableSchemaInfo(
        table_name="FactSales",
        columns=(
            ColumnInfo(name="SalesKey", type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="ProductKey", type="INTEGER", nullable=False, is_primary_key=False),
            ColumnInfo(name="SalesAmount", type="DECIMAL", nullable=False, is_primary_key=False),
        ),
        foreign_keys=(
            ForeignKeyInfo(
                constrained_columns=("ProductKey",),
                referred_table="DimProduct",
                referred_columns=("ProductKey",),
            ),
        ),
        ddl="CREATE TABLE FactSales (...)",
    )
    return [product, sales]


class TestMakeChunkId:
    def test_deterministic_for_identical_identity(self):
        id1 = make_chunk_id("default", None, "DimProduct", ChunkType.TABLE)
        id2 = make_chunk_id("default", None, "DimProduct", ChunkType.TABLE)
        assert id1 == id2

    def test_differs_across_identity_fields(self):
        base = make_chunk_id("default", None, "DimProduct", ChunkType.TABLE)
        assert base != make_chunk_id("other_db", None, "DimProduct", ChunkType.TABLE)
        assert base != make_chunk_id("default", "sales_schema", "DimProduct", ChunkType.TABLE)
        assert base != make_chunk_id("default", None, "DimCustomer", ChunkType.TABLE)
        assert base != make_chunk_id("default", None, "DimProduct", ChunkType.COLUMN)
        assert base != make_chunk_id(
            "default", None, "DimProduct", ChunkType.TABLE, column_name="ListPrice"
        )
        assert base != make_chunk_id("default", None, "DimProduct", ChunkType.TABLE, version=2)

    def test_not_random_across_process_runs(self):
        """A chunk_id must be reproducible without any prior state -- this is
        what makes re-ingestion idempotent (see retrieval/models.py's docstring)."""
        ids = {make_chunk_id("default", None, "DimProduct", ChunkType.TABLE) for _ in range(5)}
        assert len(ids) == 1


class TestContentHash:
    def test_deterministic_for_identical_content(self):
        h1 = compute_content_hash("some text", {"a": 1})
        h2 = compute_content_hash("some text", {"a": 1})
        assert h1 == h2

    def test_differs_when_text_or_extra_changes(self):
        base = compute_content_hash("some text", {"a": 1})
        assert base != compute_content_hash("different text", {"a": 1})
        assert base != compute_content_hash("some text", {"a": 2})

    def test_insensitive_to_dict_key_order(self):
        h1 = compute_content_hash("t", {"a": 1, "b": 2})
        h2 = compute_content_hash("t", {"b": 2, "a": 1})
        assert h1 == h2


class TestTableChunks:
    def test_one_chunk_per_table(self):
        chunks = table_chunks_from_schema(_sample_tables(), "default", "fake-model", 32)
        assert len(chunks) == 2
        assert {c.table_name for c in chunks} == {"DimProduct", "FactSales"}
        assert all(c.chunk_type == ChunkType.TABLE for c in chunks)

    def test_includes_primary_key_and_foreign_keys(self):
        chunks = table_chunks_from_schema(_sample_tables(), "default", "fake-model", 32)
        sales_chunk = next(c for c in chunks if c.table_name == "FactSales")
        assert "SalesKey" in sales_chunk.text
        assert "DimProduct" in sales_chunk.text
        assert sales_chunk.extra["primary_key"] == ["SalesKey"]
        assert len(sales_chunk.extra["foreign_keys"]) == 1

    def test_metadata_fields_populated(self):
        chunks = table_chunks_from_schema(_sample_tables(), "default", "fake-model", 32)
        chunk = chunks[0]
        assert chunk.database_id == "default"
        assert chunk.embedding_model == "fake-model"
        assert chunk.embedding_dimensions == 32
        assert chunk.source_id.startswith("table:")
        assert chunk.sensitivity == Sensitivity.NORMAL
        assert chunk.content_hash


class TestColumnChunks:
    def test_one_chunk_per_column(self):
        tables = _sample_tables()
        total_columns = sum(len(t.columns) for t in tables)
        chunks = column_chunks_from_schema(tables, "default", "fake-model", 32)
        assert len(chunks) == total_columns
        assert all(c.chunk_type == ChunkType.COLUMN for c in chunks)

    def test_sensitivity_from_classification(self):
        from config.sensitive_columns import SensitivityTier

        tables = _sample_tables()
        classifications: dict[tuple[str, str], SensitivityTier] = {
            ("DimProduct", "ListPrice"): "restricted"
        }
        chunks = column_chunks_from_schema(
            tables, "default", "fake-model", 32, sensitive_columns=classifications
        )
        list_price_chunk = next(
            c for c in chunks if c.table_name == "DimProduct" and c.column_name == "ListPrice"
        )
        assert list_price_chunk.sensitivity == Sensitivity.RESTRICTED
        other_chunk = next(
            c for c in chunks if c.table_name == "DimProduct" and c.column_name == "ProductName"
        )
        assert other_chunk.sensitivity == Sensitivity.NORMAL

    def test_column_notes_included_in_text(self):
        tables = _sample_tables()
        notes = {"DimProduct": {"ListPrice": "MSRP in USD"}}
        chunks = column_chunks_from_schema(tables, "default", "fake-model", 32, column_notes=notes)
        chunk = next(
            c for c in chunks if c.table_name == "DimProduct" and c.column_name == "ListPrice"
        )
        assert "MSRP in USD" in chunk.text


class TestRelationshipChunks:
    def test_one_chunk_per_foreign_key(self):
        chunks = relationship_chunks_from_schema(_sample_tables(), "default", "fake-model", 32)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_type == ChunkType.RELATIONSHIP
        assert chunk.extra["source_table"] == "FactSales"
        assert chunk.extra["target_table"] == "DimProduct"
        assert "FactSales.ProductKey = DimProduct.ProductKey" in chunk.extra["join_condition"]

    def test_never_split_across_chunks(self):
        """One FK's full join description must stay in a single chunk's text."""
        chunks = relationship_chunks_from_schema(_sample_tables(), "default", "fake-model", 32)
        chunk = chunks[0]
        assert "Join condition" in chunk.text
        assert "Recommended direction" in chunk.text


class TestGlossaryChunks:
    def test_loads_from_yaml(self, tmp_path: Path):
        path = tmp_path / "glossary.yaml"
        path.write_text(
            """
terms:
  - term: reseller
    synonyms: ["dealer"]
    definition: "A business partner who resells products."
    mapped_tables: ["DimReseller"]
    common_filters: ["BusinessType"]
""",
            encoding="utf-8",
        )
        chunks = glossary_chunks_from_yaml(path, "default", "fake-model", 32)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_type == ChunkType.GLOSSARY
        assert "reseller" in chunk.text.lower()
        assert "dealer" in chunk.text.lower()
        assert chunk.extra["mapped_tables"] == ["DimReseller"]

    def test_missing_file_returns_empty_not_an_error(self, tmp_path: Path):
        chunks = glossary_chunks_from_yaml(tmp_path / "does_not_exist.yaml", "default", "m", 32)
        assert chunks == []

    def test_entries_missing_term_are_skipped(self, tmp_path: Path):
        path = tmp_path / "glossary.yaml"
        path.write_text("terms:\n  - definition: 'no term key here'\n", encoding="utf-8")
        assert glossary_chunks_from_yaml(path, "default", "m", 32) == []


class TestMetricChunks:
    def test_loads_from_yaml(self, tmp_path: Path):
        path = tmp_path / "metrics.yaml"
        path.write_text(
            """
metrics:
  - name: gross margin
    definition: "Sales minus cost."
    formula: "SUM(SalesAmount) - SUM(Cost)"
    aggregation: SUM
    grain: "per order line"
    source_tables: ["FactSales"]
""",
            encoding="utf-8",
        )
        chunks = metric_chunks_from_yaml(path, "default", "fake-model", 32)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_type == ChunkType.METRIC
        assert "gross margin" in chunk.text.lower()
        assert chunk.extra["formula"] == "SUM(SalesAmount) - SUM(Cost)"


class TestSqlExampleChunks:
    def test_loads_from_yaml(self, tmp_path: Path):
        path = tmp_path / "sql_examples.yaml"
        path.write_text(
            """
examples:
  - question: "How many sales?"
    sql: "SELECT COUNT(*) FROM FactSales"
    tables_used: ["FactSales"]
    validation_status: reviewed
""",
            encoding="utf-8",
        )
        chunks = sql_example_chunks_from_yaml(path, "default", "fake-model", 32)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_type == ChunkType.SQL_EXAMPLE
        assert "SELECT COUNT(*) FROM FactSales" in chunk.text
        assert chunk.extra["validation_status"] == "reviewed"

    def test_entries_missing_sql_are_skipped(self, tmp_path: Path):
        path = tmp_path / "sql_examples.yaml"
        path.write_text("examples:\n  - question: 'no sql key here'\n", encoding="utf-8")
        assert sql_example_chunks_from_yaml(path, "default", "m", 32) == []


class TestDocumentationChunking:
    def test_short_document_is_a_single_chunk(self):
        chunks = documentation_chunks_from_text(
            parent_document_id="doc1",
            title="Short Doc",
            content="This is a short document.",
            source_path="doc1.md",
            database_id="default",
            embedding_model="fake-model",
            embedding_dimensions=32,
            chunk_chars=1000,
            overlap_chars=100,
        )
        assert len(chunks) == 1
        assert chunks[0].chunk_type == ChunkType.DOCUMENTATION
        assert chunks[0].extra["parent_document_id"] == "doc1"

    def test_long_document_splits_into_multiple_chunks_with_shared_parent(self):
        long_content = "\n\n".join(f"Paragraph {i} " + "word " * 50 for i in range(20))
        chunks = documentation_chunks_from_text(
            parent_document_id="doc1",
            title="Long Doc",
            content=long_content,
            source_path="doc1.md",
            database_id="default",
            embedding_model="fake-model",
            embedding_dimensions=32,
            chunk_chars=500,
            overlap_chars=50,
        )
        assert len(chunks) > 1
        assert all(c.extra["parent_document_id"] == "doc1" for c in chunks)
        # section_count metadata is consistent across every chunk from this document
        assert len({c.extra["section_count"] for c in chunks}) == 1
        assert [c.extra["section_index"] for c in chunks] == list(range(1, len(chunks) + 1))

    def test_overlap_preserves_some_shared_content_between_adjacent_chunks(self):
        long_content = " ".join(f"word{i}" for i in range(400))
        chunks = documentation_chunks_from_text(
            parent_document_id="doc1",
            title="Doc",
            content=long_content,
            source_path="doc1.md",
            database_id="default",
            embedding_model="fake-model",
            embedding_dimensions=32,
            chunk_chars=400,
            overlap_chars=80,
        )
        assert len(chunks) >= 2
        first_tail = chunks[0].text[-40:]
        second_text = chunks[1].text
        # At least some trailing words of chunk 1 reappear near the start of chunk 2.
        overlap_words = set(first_tail.split()) & set(second_text[:120].split())
        assert overlap_words

    def test_load_documentation_files_extracts_markdown_title(self, tmp_path: Path):
        doc_dir = tmp_path / "documentation"
        doc_dir.mkdir()
        (doc_dir / "guide.md").write_text("# My Guide\n\nSome content.", encoding="utf-8")
        (doc_dir / "notes.txt").write_text("Plain notes, no heading.", encoding="utf-8")

        results = load_documentation_files(doc_dir)
        titles = {r[1] for r in results}
        assert "My Guide" in titles
        assert "notes" in titles  # falls back to filename stem

    def test_load_documentation_files_missing_dir_returns_empty(self, tmp_path: Path):
        assert load_documentation_files(tmp_path / "does_not_exist") == []
