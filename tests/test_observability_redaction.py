"""Unit tests for observability/redaction.py's summarize_result_for_log."""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings
from observability.redaction import summarize_result_for_log


def _settings(**overrides) -> Settings:
    defaults = dict(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=5432,
        db_name="mydb",
        db_user="reader",
        db_password="S3cr3t!",
        db_connection_string=None,
        db_schema=None,
        db_odbc_driver="x",
        chroma_persist_dir=Path("/tmp/chroma"),
        chroma_collection_name="x",
        embedding_model_name="x",
        schema_top_k=4,
        max_retries=3,
        max_result_rows=1000,
        query_timeout_seconds=15,
        llm_max_tokens=1024,
        insight_max_tokens=120,
        max_question_length=500,
        question_rate_limit_per_minute=10,
        llm_call_rate_limit_per_minute=20,
        cost_estimation_enabled=True,
        cost_estimation_timeout_seconds=3,
        cost_moderate_row_threshold=50_000,
        cost_high_row_threshold=1_000_000,
        log_level="INFO",
        log_redaction_level="standard",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestSummarizeResultForLog:
    def test_standard_level_includes_column_names_but_never_cell_values(self):
        summary = summarize_result_for_log(
            columns=["customer_id", "email"],
            rows=[(1, "a@example.com"), (2, "b@example.com")],
            settings=_settings(log_redaction_level="standard"),
        )
        assert summary["row_count"] == 2
        assert summary["column_count"] == 2
        assert summary["columns"] == ["customer_id", "email"]
        assert "a@example.com" not in repr(summary)

    def test_strict_level_drops_column_names(self):
        summary = summarize_result_for_log(
            columns=["ssn", "salary"],
            rows=[(1, 2), (3, 4), (5, 6)],
            settings=_settings(log_redaction_level="strict"),
        )
        assert summary["row_count"] == 3
        assert summary["column_count"] == 2
        assert summary["columns"] is None

    def test_none_settings_defaults_to_standard(self):
        summary = summarize_result_for_log(columns=["a", "b"], rows=[(1, 2)], settings=None)
        assert summary["columns"] == ["a", "b"]

    def test_no_result_yields_zeroed_summary_regardless_of_level(self):
        summary = summarize_result_for_log(
            columns=None, rows=None, settings=_settings(log_redaction_level="standard")
        )
        assert summary == {"row_count": 0, "column_count": 0, "columns": None}

    def test_empty_result_set_with_known_columns(self):
        """A validated, executed query that legitimately returned zero rows
        still has known columns -- distinct from `columns=None` (no result
        at all, e.g. before execution)."""
        summary = summarize_result_for_log(
            columns=["id"], rows=[], settings=_settings(log_redaction_level="standard")
        )
        assert summary["row_count"] == 0
        assert summary["column_count"] == 1
        assert summary["columns"] == ["id"]

    def test_never_includes_raw_row_data_in_returned_summary(self):
        rows = [(1, "top-secret-value")]
        summary = summarize_result_for_log(columns=["id", "note"], rows=rows, settings=_settings())
        assert "top-secret-value" not in repr(summary)
        assert set(summary.keys()) == {"row_count", "column_count", "columns"}
