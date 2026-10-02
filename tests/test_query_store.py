"""Unit tests for db/query_store.py -- Prompt 19
(`19_QUERY_STORE_PERFORMANCE_CONTRACT.md`)'s SQL Server Query Store
performance intelligence.

Mocked at the `db.query_store.execute_readonly_sql` dispatch level --
same convention `tests/test_query_cost.py` already establishes for
`db.query_cost._STRATEGIES` -- these tests are about availability
classification, literal masking, aggregation, and the fail-open
contract, never a real SQLAlchemy engine/connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import db.query_store as query_store
from config.settings import Settings
from db.query_store import (
    QueryStoreFindings,
    _mask_literals,
    check_query_store_availability,
    clear_query_store_cache,
    get_cached_query_store_findings,
    get_query_store_findings,
    get_regressions,
    get_top_queries,
)
from security.secrets import SecretStr

_UNUSED_ENGINE = cast(Engine, object())


def _settings(**overrides) -> Settings:
    base = Settings(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        db_type="mssql",
        db_host="db.example.com",
        db_name="AdventureWorksDW2025",
        db_user="reader",
        db_password=SecretStr("secret"),
        db_connection_string=None,
        db_schema=None,
        chroma_persist_dir=Path("/tmp/chroma"),
    )
    return Settings(**{**base.__dict__, **overrides})


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_query_store_cache()
    yield
    clear_query_store_cache()


class TestMaskLiterals:
    def test_masks_string_literal(self):
        preview = _mask_literals(
            "SELECT * FROM Customers WHERE Email = 'jane@example.com'", _settings()
        )
        assert "jane@example.com" not in preview
        assert "?" in preview

    def test_masks_numeric_literal(self):
        preview = _mask_literals("SELECT * FROM T WHERE Age > 42", _settings())
        assert "42" not in preview

    def test_masks_multiple_literals(self):
        preview = _mask_literals("SELECT * FROM T WHERE a = 'x' AND b = 'y' AND c = 1", _settings())
        assert "'x'" not in preview
        assert "'y'" not in preview

    def test_none_input_returns_generic_placeholder(self):
        preview = _mask_literals(None, _settings())
        assert "example.com" not in preview
        assert preview

    def test_empty_input_returns_generic_placeholder(self):
        preview = _mask_literals("", _settings())
        assert preview

    def test_unparseable_sql_never_leaks_raw_text(self):
        garbage = "this is not valid SQL at all ((("
        preview = _mask_literals(garbage, _settings())
        assert garbage not in preview

    def test_result_is_truncated(self):
        long_sql = "SELECT " + ", ".join(f"col{i}" for i in range(500)) + " FROM T"
        preview = _mask_literals(long_sql, _settings(query_store_preview_max_chars=50))
        assert len(preview) <= 51  # 50 chars + the truncation marker


class TestCheckQueryStoreAvailability:
    def test_non_mssql_is_unavailable(self):
        result = check_query_store_availability(_UNUSED_ENGINE, "postgresql", _settings())
        assert result.available is False
        assert "SQL Server" in result.reason

    def test_feature_flag_off_is_unavailable(self):
        result = check_query_store_availability(
            _UNUSED_ENGINE, "mssql", _settings(enable_query_store_insights=False)
        )
        assert result.available is False

    def test_available_when_query_store_is_read_write(self, monkeypatch):
        monkeypatch.setattr(
            query_store,
            "execute_readonly_sql",
            lambda *a, **k: (["actual_state_desc"], [("READ_WRITE",)]),
        )
        result = check_query_store_availability(_UNUSED_ENGINE, "mssql", _settings())
        assert result.available is True

    def test_not_enabled_state_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            query_store,
            "execute_readonly_sql",
            lambda *a, **k: (["actual_state_desc"], [("OFF",)]),
        )
        result = check_query_store_availability(_UNUSED_ENGINE, "mssql", _settings())
        assert result.available is False
        assert "OFF" in result.reason

    def test_never_configured_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(query_store, "execute_readonly_sql", lambda *a, **k: ([], []))
        result = check_query_store_availability(_UNUSED_ENGINE, "mssql", _settings())
        assert result.available is False

    def test_permission_error_fails_open(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise SQLAlchemyError("VIEW DATABASE STATE permission was denied")

        monkeypatch.setattr(query_store, "execute_readonly_sql", _raise)
        result = check_query_store_availability(_UNUSED_ENGINE, "mssql", _settings())
        assert result.available is False
        assert "VIEW DATABASE STATE" not in result.reason  # no raw driver text leaked

    def test_timeout_fails_open(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise TimeoutError("exceeded timeout")

        monkeypatch.setattr(query_store, "execute_readonly_sql", _raise)
        result = check_query_store_availability(_UNUSED_ENGINE, "mssql", _settings())
        assert result.available is False


class TestGetTopQueries:
    def test_returns_masked_stats(self, monkeypatch):
        columns = [
            "query_id",
            "query_hash",
            "query_sql_text",
            "execution_count",
            "avg_duration_ms",
            "avg_cpu_ms",
            "avg_logical_reads",
            "plan_count",
            "has_forced_plan",
        ]
        rows = [
            (1, b"\x01\x02", "SELECT * FROM T WHERE x = 'secret'", 10, 2500.0, 1200.0, 500.0, 1, 0)
        ]
        monkeypatch.setattr(query_store, "execute_readonly_sql", lambda *a, **k: (columns, rows))
        results = get_top_queries(_UNUSED_ENGINE, _settings())
        assert len(results) == 1
        assert results[0].query_fingerprint == "0102"
        assert "secret" not in results[0].normalized_sql_preview
        assert results[0].execution_count == 10
        assert results[0].avg_duration_ms == 2500.0

    def test_query_failure_fails_open(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise SQLAlchemyError("boom")

        monkeypatch.setattr(query_store, "execute_readonly_sql", _raise)
        assert get_top_queries(_UNUSED_ENGINE, _settings()) == ()

    def test_no_rows_returns_empty(self, monkeypatch):
        monkeypatch.setattr(query_store, "execute_readonly_sql", lambda *a, **k: ([], []))
        assert get_top_queries(_UNUSED_ENGINE, _settings()) == ()


class TestGetRegressions:
    def test_returns_regression_with_computed_factor(self, monkeypatch):
        columns = [
            "query_id",
            "query_hash",
            "query_sql_text",
            "baseline_avg_duration_ms",
            "recent_avg_duration_ms",
            "recent_execution_count",
            "baseline_execution_count",
        ]
        rows = [(2, b"\xff", "SELECT * FROM U WHERE y = 1", 100.0, 400.0, 20, 30)]
        monkeypatch.setattr(query_store, "execute_readonly_sql", lambda *a, **k: (columns, rows))
        results = get_regressions(_UNUSED_ENGINE, _settings())
        assert len(results) == 1
        assert results[0].regression_factor == pytest.approx(4.0)
        assert results[0].query_fingerprint == "ff"

    def test_query_failure_fails_open(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise SQLAlchemyError("boom")

        monkeypatch.setattr(query_store, "execute_readonly_sql", _raise)
        assert get_regressions(_UNUSED_ENGINE, _settings()) == ()


class TestGetQueryStoreFindings:
    def test_unavailable_skips_dmv_reads_entirely(self, monkeypatch):
        calls = {"count": 0}

        def _fail_if_called(*args, **kwargs):
            calls["count"] += 1
            raise AssertionError("should not query DMVs when unavailable")

        monkeypatch.setattr(query_store, "execute_readonly_sql", _fail_if_called)
        findings = get_query_store_findings(_UNUSED_ENGINE, "postgresql", _settings())
        assert findings.availability.available is False
        assert findings.top_queries == ()
        assert findings.regressions == ()
        assert calls["count"] == 0

    def test_available_runs_both_reads(self, monkeypatch):
        def _fake(sql, *args, **kwargs):
            if "database_query_store_options" in sql:
                return (["actual_state_desc"], [("READ_WRITE",)])
            if "WITH recent AS" in sql:
                return ([], [])
            return (
                [
                    "query_id",
                    "query_hash",
                    "query_sql_text",
                    "execution_count",
                    "avg_duration_ms",
                    "avg_cpu_ms",
                    "avg_logical_reads",
                    "plan_count",
                    "has_forced_plan",
                ],
                [(1, b"\x01", "SELECT 1", 10, 500.0, 100.0, 50.0, 1, 0)],
            )

        monkeypatch.setattr(query_store, "execute_readonly_sql", _fake)
        findings = get_query_store_findings(_UNUSED_ENGINE, "mssql", _settings())
        assert findings.availability.available is True
        assert len(findings.top_queries) == 1
        assert findings.regressions == ()


def _fake_available_no_rows(sql: str, *args, **kwargs):
    if "database_query_store_options" in sql:
        return (["actual_state_desc"], [("READ_WRITE",)])
    return ([], [])


class TestCachedFindings:
    def test_second_call_within_ttl_reuses_cached_result(self, monkeypatch):
        calls = {"count": 0}

        def _fake(sql, *args, **kwargs):
            calls["count"] += 1
            return _fake_available_no_rows(sql, *args, **kwargs)

        monkeypatch.setattr(query_store, "execute_readonly_sql", _fake)
        settings = _settings(query_store_refresh_interval_seconds=300.0)
        first = get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "hr", settings)
        second = get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "hr", settings)
        assert first == second
        # availability + (top_queries empty query + regressions empty query) == 3 calls
        # on the first invocation only; the second must not have re-queried at all.
        count_after_first_only = calls["count"]
        third = get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "hr", settings)
        assert calls["count"] == count_after_first_only
        assert third == first

    def test_different_cache_keys_are_independent(self, monkeypatch):
        monkeypatch.setattr(
            query_store,
            "execute_readonly_sql",
            lambda *a, **k: (["actual_state_desc"], [("OFF",)]),
        )
        settings = _settings()
        a = get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "db-a", settings)
        b = get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "db-b", settings)
        assert a.availability.available is False
        assert b.availability.available is False

    def test_expired_ttl_triggers_a_fresh_query(self, monkeypatch):
        calls = {"count": 0}

        def _fake(sql, *args, **kwargs):
            calls["count"] += 1
            return _fake_available_no_rows(sql, *args, **kwargs)

        monkeypatch.setattr(query_store, "execute_readonly_sql", _fake)
        settings = _settings(query_store_refresh_interval_seconds=0.01)
        get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "hr2", settings)
        first_count = calls["count"]
        import time

        time.sleep(0.05)
        get_cached_query_store_findings(_UNUSED_ENGINE, "mssql", "hr2", settings)
        assert calls["count"] > first_count


class TestFindingsConstruction:
    def test_findings_is_always_constructible_even_with_no_data(self):
        findings = QueryStoreFindings(
            availability=check_query_store_availability(_UNUSED_ENGINE, "postgresql", _settings()),
            lookback_hours=24.0,
        )
        assert findings.top_queries == ()
        assert findings.regressions == ()
