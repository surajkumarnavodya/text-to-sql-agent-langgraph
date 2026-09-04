"""Unit tests for proactive query cost estimation (db/query_cost.py).

The per-engine plan-fetch functions (`_run_postgresql_explain`,
`_run_mssql_showplan`, ...) are mocked at the `estimate_query_cost`
dispatch level (`db.query_cost._STRATEGIES`) rather than mocking a real
SQLAlchemy engine/connection -- these tests are about severity
classification and the fail-open contract, not connection plumbing (that's
what the live SHOWPLAN verification against AdventureWorksDW2025 covered
during development). Threshold values (50,000 / 1,000,000 estimated rows)
match the calibration against real SHOWPLAN output described in
SECURITY.md.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import cast

from sqlalchemy import Engine

import db.query_cost as query_cost
from config.settings import Settings
from db.query_cost import (
    MODERATE_COST_NOTICE,
    CostEstimate,
    _classify_severity,
    _max_mysql_scan_rows,
    estimate_query_cost,
    high_cost_error_message,
)
from security.secrets import SecretStr

# These tests only ever exercise estimate_query_cost's dispatch/severity
# logic (mocked at the _STRATEGIES level, or short-circuited before the
# engine is ever touched -- e.g. cost_estimation_enabled=False) -- never a
# real Engine. `cast` documents that "engine" is an intentionally-untyped
# placeholder for these call sites, rather than a real connection.
_UNUSED_ENGINE = cast(Engine, object())


def _mock_mssql_strategy(monkeypatch, strategy) -> None:
    """Replaces the mssql entry in db.query_cost._STRATEGIES for one test."""
    monkeypatch.setitem(query_cost._STRATEGIES, "mssql", strategy)


_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="mssql",
    db_host="db.example.com",
    db_port=None,
    db_name="AdventureWorksDW2025",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="ODBC Driver 17 for SQL Server",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
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


def _settings(**overrides: object) -> Settings:
    """A `_BASE_SETTINGS` copy with `overrides` applied -- see
    `tests/test_connection.py::_settings` for why `dataclasses.replace` is
    used here instead of spreading a dict into `Settings(**...)` directly."""
    return dataclasses.replace(_BASE_SETTINGS, **overrides)  # type: ignore[arg-type]


class TestClassifySeverity:
    def test_below_moderate_threshold_is_low(self):
        assert _classify_severity(1.0, _settings()) == "low"
        assert _classify_severity(49_999.0, _settings()) == "low"

    def test_at_or_above_moderate_threshold_is_moderate(self):
        assert _classify_severity(50_000.0, _settings()) == "moderate"
        assert _classify_severity(60_398.0, _settings()) == "moderate"  # real full-fact-scan value

    def test_at_or_above_high_threshold_is_high(self):
        assert _classify_severity(1_000_000.0, _settings()) == "high"
        assert _classify_severity(1_116_400_000.0, _settings()) == "high"  # real cross-join value

    def test_unknown_row_estimate_fails_toward_low(self):
        """None means the plan didn't yield a row estimate -- must not
        block, same fail-open principle as a total estimation failure."""
        assert _classify_severity(None, _settings()) == "low"

    def test_thresholds_are_configurable(self):
        settings = _settings(cost_moderate_row_threshold=10, cost_high_row_threshold=100)
        assert _classify_severity(5.0, settings) == "low"
        assert _classify_severity(50.0, settings) == "moderate"
        assert _classify_severity(500.0, settings) == "high"


class TestEstimateQueryCost:
    def test_disabled_returns_none_without_calling_any_strategy(self, monkeypatch):
        called = []
        _mock_mssql_strategy(monkeypatch, lambda *a: called.append(1))
        settings = _settings(cost_estimation_enabled=False)

        result = estimate_query_cost(
            engine=_UNUSED_ENGINE, sql="SELECT 1", db_type=settings.db_type, settings=settings
        )

        assert result is None
        assert called == []

    def test_unsupported_db_type_returns_none(self):
        settings = _settings(db_type="not_a_real_engine")
        result = estimate_query_cost(
            engine=_UNUSED_ENGINE, sql="SELECT 1", db_type=settings.db_type, settings=settings
        )
        assert result is None

    def test_moderate_estimate_from_mocked_strategy(self, monkeypatch):
        from db.query_cost import _RawPlanInfo

        _mock_mssql_strategy(
            monkeypatch,
            lambda engine, sql: _RawPlanInfo(
                estimated_rows=60_398.0,
                estimated_cost=0.98,
                plan_summary="Clustered Index Scan",
            ),
        )
        settings = _settings()

        result = estimate_query_cost(
            engine=_UNUSED_ENGINE,
            sql="SELECT * FROM FactInternetSales",
            db_type=settings.db_type,
            settings=settings,
        )

        assert result is not None
        assert result.severity == "moderate"
        assert result.estimated_rows == 60_398.0
        assert result.plan_summary == "Clustered Index Scan"

    def test_high_estimate_from_mocked_strategy(self, monkeypatch):
        from db.query_cost import _RawPlanInfo

        _mock_mssql_strategy(
            monkeypatch,
            lambda engine, sql: _RawPlanInfo(
                estimated_rows=1_116_400_000.0, estimated_cost=4871.08, plan_summary="Nested Loops"
            ),
        )
        settings = _settings()

        result = estimate_query_cost(
            engine=_UNUSED_ENGINE,
            sql="SELECT * FROM a, b",
            db_type=settings.db_type,
            settings=settings,
        )

        assert result is not None
        assert result.severity == "high"

    def test_unexpected_strategy_exception_fails_open(self, monkeypatch):
        """The except clause in estimate_query_cost is deliberately broad
        (`Exception`, not a specific list) -- an unanticipated driver bug
        must fail open exactly like an anticipated one (SQLAlchemyError, a
        timeout, malformed plan JSON/XML), never crash the whole agent run
        over what's an optional optimization. This is the literal
        "never let a cost-estimation bug become the reason a legitimate
        query can't run" constraint."""

        def _raise(engine, sql):
            raise RuntimeError("driver exploded")

        _mock_mssql_strategy(monkeypatch, _raise)
        settings = _settings()

        result = estimate_query_cost(
            engine=_UNUSED_ENGINE, sql="SELECT 1", db_type=settings.db_type, settings=settings
        )

        assert result is None

    def test_strategy_returning_none_fails_open(self, monkeypatch):
        _mock_mssql_strategy(monkeypatch, lambda engine, sql: None)
        settings = _settings()

        result = estimate_query_cost(
            engine=_UNUSED_ENGINE, sql="SELECT 1", db_type=settings.db_type, settings=settings
        )

        assert result is None


class TestHighCostErrorMessage:
    def test_includes_formatted_row_estimate(self):
        estimate = CostEstimate(
            estimated_rows=1_116_400_000.0,
            estimated_cost=4871.08,
            severity="high",
            plan_summary="Nested Loops",
        )
        message = high_cost_error_message(estimate)
        assert "1,116,400,000 rows" in message
        assert "filter" in message.lower()

    def test_falls_back_gracefully_with_no_row_estimate(self):
        estimate = CostEstimate(
            estimated_rows=None, estimated_cost=None, severity="high", plan_summary="unknown"
        )
        message = high_cost_error_message(estimate)
        assert "large amount of data" in message


class TestModerateCostNotice:
    def test_is_short_and_non_alarming(self):
        assert len(MODERATE_COST_NOTICE) < 120
        assert "error" not in MODERATE_COST_NOTICE.lower()


class TestMaxMysqlScanRows:
    def test_finds_top_level_rows_examined(self):
        plan = {"table": {"rows_examined_per_scan": 500}}
        assert _max_mysql_scan_rows(plan) == 500.0

    def test_finds_the_largest_value_across_nested_joins(self):
        plan = {
            "nested_loop": [
                {"table": {"rows_examined_per_scan": 100}},
                {"table": {"rows_examined_per_scan": 900_000}},
            ]
        }
        assert _max_mysql_scan_rows(plan) == 900_000.0

    def test_returns_none_when_no_row_fields_present(self):
        assert _max_mysql_scan_rows({"query_block": {"select_id": 1}}) is None
