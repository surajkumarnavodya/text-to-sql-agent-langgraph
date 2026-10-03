"""Unit tests for POST /execute (api/main.py) -- the API's "Confirm and Run" route.

Fully mocked, same style as `tests/test_api_ask.py`: `execute_readonly_sql`
and `get_read_only_engine` are patched at the `api.main` module they're
looked up from. `validate_sql`/`enforce_row_limit`/`qualify_table_schema`
are the real functions (not mocked) so this also exercises the actual SQL
allowlist against real SQL text, exactly like the live route's own flow does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

import api.main as api_main
from config.settings import Settings
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    schema_top_k=4,
    max_retries=3,
    complex_query_max_retry_bonus=2,
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


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
    monkeypatch.setattr("api.main.get_read_only_engine", lambda config: object())
    return _BASE_SETTINGS


@pytest.fixture(autouse=True)
def _reset_api_action_limiters():
    """`api.rate_limit._limiters` is a process-wide singleton dict --
    reset before/after every test so one test's requests can't trip
    another's rate limit purely by test order/count (same reasoning as
    `test_api_ask.py`'s `_reset_ip_limiters`)."""
    import api.rate_limit as api_rate_limit

    api_rate_limit._limiters.clear()
    yield
    api_rate_limit._limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestExecute:
    def test_valid_select_is_executed_and_returns_a_chart(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (
                ["region", "revenue"],
                [("East", 100), ("West", 200)],
            ),
        )

        response = client.post(
            "/execute", json={"sql": "SELECT region, revenue FROM sales", "database": "default"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["database"] == "default"
        assert body["result_columns"] == ["region", "revenue"]
        assert body["row_count"] == 2
        assert body["duration_ms"] is not None
        # Two categories + one numeric column -> classify_columns/recommend_chart
        # (agent/result_charting.py) should suggest a bar chart, and never
        # claim the result was truncated (2 rows, well under max_result_rows).
        assert body["column_types"] == {"region": "text", "revenue": "numeric"}
        assert body["chart_recommendation"] == {
            "chart_type": "bar",
            "reason": "A category column with a numeric measure compares cleanly as a bar chart.",
            "x_column": "region",
            "y_column": "revenue",
        }
        assert body["truncated"] is False

    def test_two_numeric_columns_recommend_scatter_not_a_kpi(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["a", "b"], [(1, 2)]),
        )

        response = client.post("/execute", json={"sql": "SELECT a, b FROM t"})

        assert response.status_code == 200
        body = response.json()
        # Two numeric columns -> a scatter recommendation (a real, valid shape
        # for one) -- the single-row/single-numeric-column KPI case doesn't
        # apply here since there are two numeric columns, not one.
        assert body["column_types"] == {"a": "numeric", "b": "numeric"}
        assert body["chart_recommendation"]["chart_type"] == "scatter"

    def test_no_chart_recommendation_for_an_all_text_result(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["name"], [("Alice",), ("Bob",)]),
        )

        response = client.post("/execute", json={"sql": "SELECT name FROM t"})

        assert response.status_code == 200
        body = response.json()
        assert body["column_types"] == {"name": "text"}
        assert body["chart_recommendation"] is None

    def test_truncated_flag_set_when_row_count_hits_the_configured_cap(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (
                ["a"],
                [(i,) for i in range(_BASE_SETTINGS.max_result_rows)],
            ),
        )

        response = client.post("/execute", json={"sql": "SELECT a FROM t"})

        assert response.status_code == 200
        assert response.json()["truncated"] is True

    def test_destructive_sql_is_rejected_not_executed(self, monkeypatch, client):
        called = False

        def _fail(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("must not execute a rejected statement")

        monkeypatch.setattr("api.main.execute_readonly_sql", _fail)

        response = client.post("/execute", json={"sql": "DROP TABLE users"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["error"].startswith("Rejected:")
        assert not called

    def test_execution_failure_is_redacted_not_raw(self, monkeypatch, client):
        def _raise(sql, timeout, max_rows, engine=None):
            raise SQLAlchemyError(
                "connection to server failed: password authentication failed for user "
                "'reader' password='hunter2'"
            )

        monkeypatch.setattr("api.main.execute_readonly_sql", _raise)

        response = client.post("/execute", json={"sql": "SELECT 1"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "hunter2" not in body["error"]

    def test_real_sqlalchemy_row_objects_serialize_correctly(self, monkeypatch, client):
        """Regression guard: `execute_readonly_sql` returns
        `sqlalchemy.engine.row.Row` objects (via `CursorResult.fetchmany`),
        not plain tuples -- `Row` isn't recognized by `fastapi.encoders
        .jsonable_encoder`'s isinstance checks and previously raised
        ValueError('object is not iterable', ...) the first time this
        endpoint ran against a real database. Uses a real (in-memory
        SQLite) engine, not a mock, so this actually exercises real `Row`
        objects rather than a mock that happens to look like one."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            real_columns = list(conn.execute(text("SELECT 1 AS a, 'x' AS b")).keys())
            real_rows = conn.execute(text("SELECT 1 AS a, 'x' AS b")).fetchall()
        assert type(real_rows[0]).__name__ == "Row"

        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (real_columns, real_rows),
        )

        response = client.post("/execute", json={"sql": "SELECT 1 AS a, 'x' AS b"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["result_rows"] == [[1, "x"]]

    def test_unknown_database_returns_404(self, client):
        response = client.post("/execute", json={"sql": "SELECT 1", "database": "nope"})
        assert response.status_code == 404

    def test_empty_sql_is_rejected_by_request_validation(self, client):
        response = client.post("/execute", json={"sql": ""})
        assert response.status_code == 422

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        """SEC-08: /execute previously had no rate limit of its own at
        all -- confirm it's actually enforced, not just constructed."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["a"], [(1,)]),
        )

        first = client.post("/execute", json={"sql": "SELECT 1"})
        second = client.post("/execute", json={"sql": "SELECT 1"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers


class TestRowsToJsonBinaryColumns:
    """A real crash, found via live use, not a hypothetical: a SELECT
    touching a genuinely binary column (e.g. AdventureWorks'
    `Production.ProductPhoto.LargePhoto`, a `varbinary(max)` storing real
    JPEG bytes) used to crash the *entire* `/ask`/`/execute` response with
    an unhandled `UnicodeDecodeError` -- `fastapi.encoders.jsonable_encoder`'s
    default `bytes` handling is a bare `.decode()` (UTF-8), which raises on
    non-text binary data. `_json_safe_cell` (api/main.py) now replaces any
    `bytes`/`bytearray`/`memoryview` value with a short, readable
    placeholder before the row ever reaches `jsonable_encoder`."""

    def test_binary_column_becomes_a_placeholder_not_a_crash(self):
        jpeg_like_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10])
        rows = [(1, "cow.jpg", jpeg_like_bytes)]

        result = api_main._rows_to_json(rows)

        assert result == [[1, "cow.jpg", f"<binary data, {len(jpeg_like_bytes)} bytes>"]]

    def test_bytearray_and_memoryview_are_also_handled(self):
        rows = [(bytearray(b"\xff\x00"), memoryview(b"\xfe\x01"))]

        result = api_main._rows_to_json(rows)

        assert result == [["<binary data, 2 bytes>", "<binary data, 2 bytes>"]]

    def test_ordinary_rows_are_unaffected(self):
        rows = [(1, "Alice", 12.5, None)]

        result = api_main._rows_to_json(rows)

        assert result == [[1, "Alice", 12.5, None]]

    def test_none_input_returns_none(self):
        assert api_main._rows_to_json(None) is None


class _AlwaysBusyLimiter:
    """Stub for `agent.rate_limit.ConcurrencyLimiter` that always reports
    the database as saturated -- used to test `/execute`'s rejection path
    without needing to actually exhaust a real limiter's capacity."""

    def try_acquire(self) -> bool:
        return False

    def release(self) -> None:  # pragma: no cover - never reached if try_acquire() is honored
        raise AssertionError("release() must not be called when try_acquire() returned False")


@pytest.fixture(autouse=True)
def _reset_result_cache_and_database_limiters():
    """`db.result_cache._result_cache` and `agent.rate_limit
    ._database_execution_limiters` are both process-wide, first-call-wins
    singletons (Prompt 22) -- reset before/after every test in this module
    so one test's cached result or acquired slot can't leak into another's,
    the same convention `test_api_ask.py::_reset_ask_concurrency_limiters`
    already established for the sibling `/ask`-level limiters."""
    import agent.rate_limit as rate_limit_module
    import db.result_cache as result_cache_module

    result_cache_module._result_cache = None
    rate_limit_module._database_execution_limiters.clear()
    yield
    result_cache_module._result_cache = None
    rate_limit_module._database_execution_limiters.clear()


class TestDatabaseConcurrencyLimit:
    """Prompt 22 (scale/performance hardening): `/execute` fast-fails with
    a 429 once `agent.rate_limit.get_database_execution_limiter` reports
    the target database's own connection pool as already saturated,
    instead of letting the request block on `QueuePool` checkout."""

    def test_rejected_with_429_when_database_is_saturated(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.get_database_execution_limiter",
            lambda name, max_concurrent: _AlwaysBusyLimiter(),
        )

        def _fail(*a, **k):
            raise AssertionError("must not execute once the concurrency limiter rejects")

        monkeypatch.setattr("api.main.execute_readonly_sql", _fail)

        response = client.post("/execute", json={"sql": "SELECT 1"})

        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_disabled_flag_never_consults_the_limiter(self, monkeypatch, client):
        settings = Settings(
            **{**_BASE_SETTINGS.__dict__, "enable_database_concurrency_limit": False}
        )
        monkeypatch.setattr("api.main.get_settings", lambda: settings)

        def _fail_if_called(name, max_concurrent):
            raise AssertionError("limiter must not even be constructed when the flag is off")

        monkeypatch.setattr("api.main.get_database_execution_limiter", _fail_if_called)
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["a"], [(1,)]),
        )

        response = client.post("/execute", json={"sql": "SELECT 1"})

        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"


class TestResultCache:
    """Prompt 22 (scale/performance hardening): `/execute` may serve a
    cached `(columns, rows)` result for exact-text-identical SQL against
    the same database when `Settings.enable_result_cache` is on. Off by
    default -- see `db/result_cache.py`'s module docstring."""

    def test_disabled_by_default_never_caches_and_sets_no_cache_header(self, monkeypatch, client):
        calls = 0

        def _count(sql, timeout, max_rows, engine=None):
            nonlocal calls
            calls += 1
            return ["a"], [(1,)]

        monkeypatch.setattr("api.main.execute_readonly_sql", _count)

        first = client.post("/execute", json={"sql": "SELECT a FROM t"})
        second = client.post("/execute", json={"sql": "SELECT a FROM t"})

        assert first.status_code == 200 and second.status_code == 200
        assert calls == 2
        assert "X-Cache" not in first.headers
        assert "X-Cache" not in second.headers

    def test_enabled_flag_misses_once_then_hits_on_identical_sql(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "enable_result_cache": True})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr("db.result_cache.load_sensitive_columns", lambda: {})

        calls = 0

        def _count(sql, timeout, max_rows, engine=None):
            nonlocal calls
            calls += 1
            return ["a"], [(1,)]

        monkeypatch.setattr("api.main.execute_readonly_sql", _count)

        first = client.post("/execute", json={"sql": "SELECT a FROM t"})
        second = client.post("/execute", json={"sql": "SELECT a FROM t"})

        assert first.status_code == 200 and second.status_code == 200
        assert calls == 1, "second identical call should be served from the cache, not re-executed"
        assert first.headers["X-Cache"] == "MISS"
        assert second.headers["X-Cache"] == "HIT"
        assert second.json()["result_rows"] == first.json()["result_rows"]

    def test_restricted_column_sql_is_never_cached_even_when_enabled(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "enable_result_cache": True})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr(
            "db.result_cache.load_sensitive_columns",
            lambda: {("customers", "ssn"): "restricted"},
        )

        calls = 0

        def _count(sql, timeout, max_rows, engine=None):
            nonlocal calls
            calls += 1
            return ["ssn"], [("123-45-6789",)]

        monkeypatch.setattr("api.main.execute_readonly_sql", _count)

        first = client.post("/execute", json={"sql": "SELECT ssn FROM customers"})
        second = client.post("/execute", json={"sql": "SELECT ssn FROM customers"})

        assert first.status_code == 200 and second.status_code == 200
        assert calls == 2, "SQL referencing a restricted column must never be served from cache"
        assert first.headers["X-Cache"] == "MISS"
        assert second.headers["X-Cache"] == "MISS"
