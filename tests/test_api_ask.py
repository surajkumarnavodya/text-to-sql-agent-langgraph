"""Unit tests for POST /ask and GET /schema/tables (api/main.py).

Fully mocked -- `agent.orchestrator.graph.run_orchestrated` (what `api.main`
actually calls -- a pass-through to `agent.graph.run_agent` when
`ENABLE_MULTI_SOURCE_ROUTER` is unset, its default in these tests) and the
DB/Chroma calls are patched at the `api.main` module they're looked up from,
exactly like `tests/test_agent_nodes.py` mocks `agent.nodes`'s own
dependencies. No real LLM call, database connection, or Chroma index is ever
touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.datastructures import Headers

import agent.rate_limit as rate_limit_module
import api.main as api_main
from agent.exceptions import SchemaRetrievalError
from agent.orchestrator.state import OrchestratorState
from agent.state import AgentState
from config.settings import Settings
from db.schema_introspection import ColumnInfo, TableSchemaInfo
from security.oidc import AuthIdentity
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


def _settings(**overrides: object) -> Settings:
    """See `tests/test_connection.py::_settings` for why this rebuilds via
    `Settings(**{**_BASE_SETTINGS.__dict__, **overrides})` rather than
    `BaseModel.model_copy(update=...)`."""
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
    return _BASE_SETTINGS


@pytest.fixture(autouse=True)
def _reset_ip_limiters():
    """`api.main._ip_limiters` is a process-wide singleton dict by design
    (mirrors `agent.rate_limit.get_llm_call_limiter`'s own module-level
    singleton, per that module's docstring) -- reset before every test so
    one test's requests can't trip another's rate limit purely by test
    order/count."""
    api_main._ip_limiters.clear()
    yield
    api_main._ip_limiters.clear()


@pytest.fixture(autouse=True)
def _reset_ask_concurrency_limiters():
    """`agent.rate_limit`'s ask-concurrency limiters (added in the
    scale-out hardening pass) are process-wide singletons for the exact
    same reason `_ip_limiters` above is -- reset before/after every test so
    one test's un-released acquisitions (a deliberately-not-completed
    background task, e.g.) can never leak into another."""
    rate_limit_module._ask_concurrency_limiter = None
    rate_limit_module._per_caller_ask_concurrency_limiters.clear()
    yield
    rate_limit_module._ask_concurrency_limiter = None
    rate_limit_module._per_caller_ask_concurrency_limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestAsk:
    def test_successful_answer_is_returned(self, monkeypatch, client):
        final_state: AgentState = {
            "status": "succeeded",
            "sql": "SELECT COUNT(*) FROM t",
            "result_columns": ["cnt"],
            "result_rows": [(5,)],
            "row_count": 1,
            "retry_count": 0,
            "attempt_history": [
                {
                    "attempt": 1,
                    "sql": "SELECT COUNT(*) FROM t",
                    "outcome": "succeeded",
                    "error": None,
                    "will_retry": False,
                }
            ],
            "insight": "There are 5 rows.",
            "error_history": [],
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["sql"] == "SELECT COUNT(*) FROM t"
        assert body["result_rows"] == [[5]]
        assert body["attempt_history"][0]["outcome"] == "succeeded"
        assert "X-Correlation-ID" in response.headers

    def test_configured_secret_in_llm_response_text_is_redacted(self, monkeypatch, client):
        """Defense-in-depth regression: if a real configured secret ever
        ended up in LLM-generated response text (insight/failure_explanation/
        error_history/...), it must never reach the caller verbatim -- see
        `api.main._redact_text`/`security.redaction.redact_configured_secrets`.
        `_BASE_SETTINGS.db_password` is `SecretStr("secret")` (see this
        file's fixtures), so the literal word "secret" here stands in for a
        real leaked value.
        """
        final_state: AgentState = {
            "status": "succeeded",
            "sql": "SELECT COUNT(*) FROM t",
            "result_columns": ["cnt"],
            "result_rows": [(5,)],
            "row_count": 1,
            "retry_count": 0,
            "attempt_history": [],
            "insight": "There are 5 rows. (debug: db password is secret)",
            "error_history": ["connection failed: password=secret;host=db"],
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        body = response.json()
        assert "secret" not in body["insight"]
        assert "***REDACTED***" in body["insight"]
        assert "secret" not in body["error_history"][0]

    def test_conversation_history_is_forwarded_to_run_agent(self, monkeypatch, client):
        captured = {}

        def _capture(
            question,
            conversation_history=None,
            enable_insight=True,
            session_id=None,
            caller_roles=(),
            caller_subject=None,
            attachment_ids=None,
            model=None,
            tenant_id=None,
        ):
            captured["question"] = question
            captured["conversation_history"] = conversation_history
            captured["enable_insight"] = enable_insight
            captured["session_id"] = session_id
            captured["caller_roles"] = caller_roles
            captured["caller_subject"] = caller_subject
            captured["attachment_ids"] = attachment_ids
            captured["model"] = model
            captured["tenant_id"] = tenant_id
            return {"status": "succeeded", "error_history": []}

        monkeypatch.setattr("api.main.run_orchestrated", _capture)

        response = client.post(
            "/ask",
            json={
                "question": "And last year?",
                "conversation_history": [
                    {
                        "question": "Total sales in 2012?",
                        "sql": "SELECT SUM(x) FROM t",
                        "tables": ["t"],
                        "status": "succeeded",
                    }
                ],
                "enable_insight": False,
            },
        )

        assert response.status_code == 200
        assert captured["question"] == "And last year?"
        assert captured["conversation_history"][0]["question"] == "Total sales in 2012?"
        assert captured["enable_insight"] is False

    def test_schema_retrieval_error_becomes_a_failed_status_not_a_500(self, monkeypatch, client):
        def _raise(*a, **k):
            raise SchemaRetrievalError("Chroma index is empty -- run scripts/build_embeddings.py.")

        monkeypatch.setattr("api.main.run_orchestrated", _raise)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        # The raw internal detail ("Chroma index is empty -- run scripts/
        # build_embeddings.py.") must NOT reach the response body -- only
        # SchemaRetrievalError.safe_message may. See agent/exceptions.py's
        # module docstring and api/main.py's /ask handler.
        assert body["error_history"][0] == SchemaRetrievalError("x").safe_message
        assert "Chroma index is empty" not in body["error_history"][0]

    def test_global_concurrency_limit_returns_429_before_any_work_starts(self, monkeypatch, client):
        """Scale-out hardening pass (docs/SCALE_OUT_PROMPT.md bottleneck
        #1/#4): once `Settings.max_concurrent_ask_requests` in-flight
        requests are already admitted, a new one is rejected immediately,
        with a Retry-After header -- and `run_orchestrated` must never even
        be called for the rejected request."""
        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: _settings(
                max_concurrent_ask_requests=1, max_concurrent_ask_requests_per_caller=5
            ),
        )
        # Simulates one already-admitted, still-running request occupying
        # the only global slot.
        rate_limit_module.get_ask_concurrency_limiter(1).try_acquire()

        def _fail_if_called(*a, **k):
            raise AssertionError("run_orchestrated must not be called once the limit is exhausted")

        monkeypatch.setattr("api.main.run_orchestrated", _fail_if_called)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "2"
        assert "capacity" in response.json()["detail"].lower()

    def test_per_caller_concurrency_limit_returns_429_even_with_global_room(
        self, monkeypatch, client
    ):
        """A caller already at their own concurrency cap is rejected even
        though the global budget still has room -- and the global slot
        this request provisionally acquired is released again (not
        leaked) when the per-caller check then fails."""
        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: _settings(
                max_concurrent_ask_requests=10, max_concurrent_ask_requests_per_caller=1
            ),
        )
        monkeypatch.setattr(
            "api.main._rate_limit_key", lambda identity, request, settings: "test-caller"
        )
        rate_limit_module.get_per_caller_ask_concurrency_limiter("test-caller", 1).try_acquire()

        def _fail_if_called(*a, **k):
            raise AssertionError("run_orchestrated must not be called once the limit is exhausted")

        monkeypatch.setattr("api.main.run_orchestrated", _fail_if_called)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "2"
        # The global slot this rejected request provisionally acquired
        # must be released, not leaked -- proven by a *second* caller
        # (a different key) still being able to acquire the global budget.
        global_limiter = rate_limit_module.get_ask_concurrency_limiter(10)
        assert global_limiter.try_acquire() is True

    def test_successful_requests_release_their_concurrency_slots(self, monkeypatch, client):
        """Two sequential (not concurrent) successful /ask calls from the
        same caller both succeed with a per-caller limit of 1 -- proving
        the slot acquired by the first is released once it actually
        finishes, not held forever."""
        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: _settings(
                max_concurrent_ask_requests=5, max_concurrent_ask_requests_per_caller=1
            ),
        )
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        first = client.post("/ask", json={"question": "How many rows are there?"})
        second = client.post("/ask", json={"question": "How many rows are there?"})

        assert first.status_code == 200
        assert second.status_code == 200

    def test_concurrency_slots_are_not_released_until_the_abandoned_work_actually_finishes(
        self, monkeypatch, client
    ):
        """The core release-semantics regression test: a timed-out request's
        concurrency slot must stay held (not released early just because
        the HTTP caller stopped waiting) until the abandoned background
        work actually completes -- see `_run_orchestrated_with_timeout`'s
        own docstring for why releasing early would defeat the whole point
        of bounding concurrency. A second request from the same caller,
        issued immediately after the first times out, must be rejected;
        only once the background work has had time to actually finish does
        a third request succeed again."""
        import time as time_module

        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: _settings(
                request_timeout_seconds=1,
                max_concurrent_ask_requests=5,
                max_concurrent_ask_requests_per_caller=1,
            ),
        )
        monkeypatch.setattr(
            "api.main._rate_limit_key", lambda identity, request, settings: "slow-caller"
        )

        def _slow(*a, **k):
            time_module.sleep(2)
            return {"status": "succeeded", "error_history": []}

        monkeypatch.setattr("api.main.run_orchestrated", _slow)

        first = client.post("/ask", json={"question": "How many rows are there?"})
        assert first.status_code == 200
        assert first.json()["status"] == "failed"  # timed out at the API layer

        # The abandoned background call is still running (it sleeps 2s,
        # the request timed out after 1s) -- this caller's one concurrency
        # slot is still held.
        second = client.post("/ask", json={"question": "How many rows are there?"})
        assert second.status_code == 429

        # Give the abandoned background call time to actually finish and
        # fire its release callback.
        time_module.sleep(2)

        third = client.post("/ask", json={"question": "How many rows are there?"})
        assert third.status_code == 200

    def test_request_timeout_returns_a_failed_status_not_a_hang(self, monkeypatch, client):
        """2026 Phase 3 reliability fix: `POST /ask` no longer blocks
        indefinitely if `run_orchestrated` never returns within
        `Settings.request_timeout_seconds` -- see that setting's docstring
        and `api.main._run_orchestrated_with_timeout`. A tiny timeout plus a
        `run_orchestrated` that sleeps past it exercises the real
        background-thread-join path, not just a mocked exception."""
        import time as time_module

        monkeypatch.setattr("api.main.get_settings", lambda: _settings(request_timeout_seconds=1))

        def _slow(*a, **k):
            time_module.sleep(5)
            return {"status": "succeeded", "error_history": []}

        monkeypatch.setattr("api.main.run_orchestrated", _slow)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "exceeded the maximum processing time" in body["error_history"][0]

    def test_empty_question_is_rejected_by_request_validation(self, client):
        response = client.post("/ask", json={"question": ""})
        assert response.status_code == 422

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.get_settings", lambda: _settings(question_rate_limit_per_minute=1)
        )
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        first = client.post("/ask", json={"question": "q1"})
        second = client.post("/ask", json={"question": "q2"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_multi_source_fields_are_surfaced(self, monkeypatch, client):
        """Regression guard for the gap the pre-React-migration API audit
        found: sources_used/citations/synthesized_answer/schema_tables/
        query_plan/followup fields must reach the client, not just the
        SQL-only subset AskResponse originally covered."""
        final_state: OrchestratorState = {
            "status": "succeeded",
            "sql": "SELECT 1",
            "error_history": [],
            "sources_used": ["sql", "policy"],
            "synthesized_answer": "**Database**: 1 row(s) returned.\n\n**Policy**: Leave policy is 20 days.",
            "policy_result": {
                "answer": "Leave policy is 20 days.",
                "citations": [
                    {
                        "filename": "leave.pdf",
                        "chunk_index": 0,
                        "page_number": 1,
                        "document_id": "doc-1",
                        "has_pdf_bytes": True,
                    }
                ],
                "status": "succeeded",
            },
            "query_plan": ["Group by region", "Sum revenue"],
            "schema_tables": [
                {"table_name": "Sales", "ddl": "CREATE TABLE Sales (...)", "similarity_score": 0.9}
            ],
            "followup_classification": "followup",
            "followup_resolved_against": {
                "question": "Total sales in 2012?",
                "sql": "SELECT SUM(x) FROM t",
                "tables": ["t"],
                "status": "succeeded",
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "And the leave policy?"})

        assert response.status_code == 200
        body = response.json()
        assert body["sources_used"] == ["sql", "policy"]
        assert "Leave policy" in body["synthesized_answer"]
        assert body["policy_result"]["citations"][0]["document_id"] == "doc-1"
        assert body["policy_result"]["citations"][0]["has_pdf_bytes"] is True
        assert body["query_plan"] == ["Group by region", "Sum revenue"]
        assert body["schema_tables"][0]["table_name"] == "Sales"
        assert body["followup_classification"] == "followup"
        assert body["followup_resolved_against"]["question"] == "Total sales in 2012?"

    def test_real_sqlalchemy_row_objects_in_result_rows_serialize_correctly(
        self, monkeypatch, client
    ):
        """Regression guard: `agent.nodes.execute_sql_node` populates
        `result_rows` from `db.execution.execute_readonly_sql`, which
        returns `sqlalchemy.engine.row.Row` objects, not plain tuples --
        `Row` isn't recognized by `fastapi.encoders.jsonable_encoder`'s
        isinstance checks and previously raised ValueError('object is not
        iterable', ...) the first time /ask ran against a real database
        (mocked-`run_orchestrated` tests never exercised a real Row)."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            real_rows = conn.execute(text("SELECT 1 AS cnt")).fetchall()
        assert type(real_rows[0]).__name__ == "Row"

        final_state: AgentState = {
            "status": "succeeded",
            "sql": "SELECT COUNT(*) AS cnt FROM t",
            "result_columns": ["cnt"],
            # Real Row objects, not tuples -- see docstring above.
            "result_rows": real_rows,  # type: ignore[typeddict-item]
            "row_count": 1,
            "error_history": [],
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "How many rows?"})

        assert response.status_code == 200
        assert response.json()["result_rows"] == [[1]]

    def test_auth_required_when_token_configured(self, monkeypatch, client):
        auth_settings = _settings(api_auth_token=SecretStr("s3cret"))
        monkeypatch.setattr("api.main.get_settings", lambda: auth_settings)
        # verify_api_key (api/auth.py) reads settings via its own imported
        # get_settings, not api.main's -- both must be patched.
        monkeypatch.setattr("api.auth.get_settings", lambda: auth_settings)
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        no_header = client.post("/ask", json={"question": "q"})
        wrong_token = client.post(
            "/ask", json={"question": "q"}, headers={"Authorization": "Bearer wrong"}
        )
        right_token = client.post(
            "/ask", json={"question": "q"}, headers={"Authorization": "Bearer s3cret"}
        )

        assert no_header.status_code == 401
        assert wrong_token.status_code == 401
        assert right_token.status_code == 200


def _fake_request(host: str | None, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    """A minimal duck-typed stand-in for `fastapi.Request` -- `_rate_limit_key`
    only ever reads `request.client.host` (and, since the trusted-proxy
    assessment pass, `request.headers` via `security.client_ip
    .resolve_client_ip`), so a real `Request` (which needs a full ASGI
    scope) would be pure ceremony here for the common (no proxy) case.
    `headers` is only exercised by `TestRateLimitKeyTrustedProxy` below."""
    client = SimpleNamespace(host=host) if host is not None else None
    return cast(Request, SimpleNamespace(client=client, headers=Headers(raw=headers or [])))


class TestRateLimitKey:
    """Unit tests for `api.main._rate_limit_key` -- the scale-out
    hardening pass's fix for bottleneck #3 (rate/concurrency limits keyed
    by raw client IP, meaningless behind a load balancer or carrier NAT)."""

    def test_local_auth_mode_keys_by_subject_not_ip(self):
        identity = AuthIdentity(subject="user-123", roles=("user",), mode="local")

        assert (
            api_main._rate_limit_key(identity, _fake_request("10.0.0.5"), _BASE_SETTINGS)
            == "user:user-123"
        )

    def test_oidc_auth_mode_keys_by_subject_not_ip(self):
        identity = AuthIdentity(subject="oidc-sub-456", roles=("user",), mode="oidc")

        assert (
            api_main._rate_limit_key(identity, _fake_request("10.0.0.5"), _BASE_SETTINGS)
            == "user:oidc-sub-456"
        )

    def test_none_auth_mode_falls_back_to_client_ip(self):
        """`AuthIdentity.subject` is a fixed shared sentinel in "none" mode
        (every caller looks identical) -- keying by it would put every
        caller in one bucket, so this mode must fall back to IP."""
        identity = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")

        assert (
            api_main._rate_limit_key(identity, _fake_request("203.0.113.7"), _BASE_SETTINGS)
            == "ip:203.0.113.7"
        )

    def test_static_token_auth_mode_falls_back_to_client_ip(self):
        identity = AuthIdentity(subject="static-token", roles=("admin",), mode="static_token")

        assert (
            api_main._rate_limit_key(identity, _fake_request("203.0.113.7"), _BASE_SETTINGS)
            == "ip:203.0.113.7"
        )

    def test_missing_client_falls_back_to_unknown(self):
        identity = AuthIdentity(subject="static-token", roles=("admin",), mode="static_token")

        assert (
            api_main._rate_limit_key(identity, _fake_request(None), _BASE_SETTINGS) == "ip:unknown"
        )

    def test_two_different_subjects_get_different_keys(self):
        request = _fake_request("10.0.0.5")
        a = AuthIdentity(subject="user-a", roles=("user",), mode="local")
        b = AuthIdentity(subject="user-b", roles=("user",), mode="local")

        assert api_main._rate_limit_key(a, request, _BASE_SETTINGS) != api_main._rate_limit_key(
            b, request, _BASE_SETTINGS
        )


class TestRateLimitKeyTrustedProxy:
    """`Settings.trusted_proxy_count` -- the enterprise scalability
    assessment's opt-in fix for bottleneck #3's IP-fallback half: behind a
    real load balancer, every "none"/"static_token"-mode caller must not
    collapse into the LB's own single IP bucket."""

    def test_default_ignores_x_forwarded_for_even_when_present(self):
        """trusted_proxy_count=0 (the default): the header must never be
        consulted, matching tests/security/test_rate_limit_header_spoofing.py's
        existing guarantee -- a request from the LB (peer 10.0.0.1) with an
        attacker-supplied X-Forwarded-For must still key on the peer."""
        identity = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")
        request = _fake_request("10.0.0.1", [(b"x-forwarded-for", b"203.0.113.9")])

        assert api_main._rate_limit_key(identity, request, _BASE_SETTINGS) == "ip:10.0.0.1"

    def test_one_trusted_proxy_reads_the_real_client_from_the_header(self):
        identity = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request("10.0.0.1", [(b"x-forwarded-for", b"203.0.113.9")])

        assert api_main._rate_limit_key(identity, request, settings) == "ip:203.0.113.9"

    def test_two_distinct_real_clients_behind_one_lb_get_different_keys(self):
        """The actual bug this fixes: without trusted-proxy support, both
        of these would collapse to the LB's own single IP."""
        identity = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")
        settings = _settings(trusted_proxy_count=1)
        request_a = _fake_request("10.0.0.1", [(b"x-forwarded-for", b"203.0.113.9")])
        request_b = _fake_request("10.0.0.1", [(b"x-forwarded-for", b"198.51.100.4")])

        assert api_main._rate_limit_key(identity, request_a, settings) != api_main._rate_limit_key(
            identity, request_b, settings
        )

    def test_client_prepended_spoofed_hops_beyond_the_trusted_count_are_ignored(self):
        """The core XFF-spoofing defense: only the Nth entry from the
        *right* is ever trusted -- entries the client could have prepended
        before ever reaching the (one, trusted) proxy must not be read."""
        identity = AuthIdentity(subject="dev-mode", roles=("admin",), mode="none")
        settings = _settings(trusted_proxy_count=1)
        request = _fake_request(
            "10.0.0.1", [(b"x-forwarded-for", b"9.9.9.9, 8.8.8.8, 203.0.113.9")]
        )

        # Only one hop is trusted -> only the rightmost entry is used, never
        # the attacker-controlled entries further left.
        assert api_main._rate_limit_key(identity, request, settings) == "ip:203.0.113.9"


class TestSchemaTables:
    def test_returns_introspected_tables(self, monkeypatch, client):
        tables = [
            TableSchemaInfo(
                table_name="DimCustomer",
                columns=(
                    ColumnInfo(name="CustomerKey", type="INT", nullable=False, is_primary_key=True),
                    ColumnInfo(
                        name="EmailAddress",
                        type="NVARCHAR(50)",
                        nullable=True,
                        is_primary_key=False,
                    ),
                ),
                foreign_keys=(),
                ddl="CREATE TABLE DimCustomer (...)",
            )
        ]
        monkeypatch.setattr("api.main.get_read_only_engine", lambda settings: object())
        monkeypatch.setattr("api.main.introspect_schema", lambda engine, schema: tables)

        response = client.get("/schema/tables")

        assert response.status_code == 200
        body = response.json()
        assert len(body["tables"]) == 1
        assert body["tables"][0]["table_name"] == "DimCustomer"
        assert body["tables"][0]["columns"][0]["is_primary_key"] is True

    def test_requires_auth_when_token_configured(self, monkeypatch, client):
        auth_settings = _settings(api_auth_token=SecretStr("s3cret"))
        monkeypatch.setattr("api.main.get_settings", lambda: auth_settings)
        monkeypatch.setattr("api.auth.get_settings", lambda: auth_settings)
        monkeypatch.setattr("api.main.get_read_only_engine", lambda settings: object())
        monkeypatch.setattr("api.main.introspect_schema", lambda engine, schema: [])

        response = client.get("/schema/tables")

        assert response.status_code == 401


class TestAskModelSelection:
    """`AskRequest.model` / `AskResponse.model` -- configurable Ollama model
    selection. `_BASE_SETTINGS` has no explicit `OLLAMA_ALLOWED_MODELS`, so
    it falls back to the default starter set (config/settings.py's
    `_DEFAULT_OLLAMA_ALLOWED_MODELS`), which includes both `llama3.1:8b`
    (the configured default) and `qwen2.5:7b` -- used below as "the
    default" and "a valid, non-default alternative" respectively."""

    def test_omitting_model_preserves_existing_behavior(self, monkeypatch, client):
        """The critical backward-compatibility guarantee: an existing
        caller's request body (no `model` field at all) must keep working
        exactly as before this feature existed."""
        captured = {}

        def _capture(question, conversation_history=None, **kwargs):
            captured.update(kwargs)
            return {"status": "succeeded", "error_history": [], "selected_model": "llama3.1:8b"}

        monkeypatch.setattr("api.main.run_orchestrated", _capture)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        assert captured["model"] == "llama3.1:8b"  # resolved to Settings.ollama_model
        assert response.json()["model"] == "llama3.1:8b"

    def test_valid_alternate_model_is_forwarded_and_validated(self, monkeypatch, client):
        captured = {}

        def _capture(question, conversation_history=None, **kwargs):
            captured.update(kwargs)
            return {"status": "succeeded", "error_history": [], "selected_model": "qwen2.5:7b"}

        monkeypatch.setattr("api.main.run_orchestrated", _capture)

        response = client.post(
            "/ask", json={"question": "How many rows are there?", "model": "qwen2.5:7b"}
        )

        assert response.status_code == 200
        assert captured["model"] == "qwen2.5:7b"
        assert response.json()["model"] == "qwen2.5:7b"

    def test_disallowed_model_returns_400_and_never_calls_run_orchestrated(
        self, monkeypatch, client
    ):
        def _fail_if_called(*args, **kwargs):
            raise AssertionError("run_orchestrated must not be called for a disallowed model")

        monkeypatch.setattr("api.main.run_orchestrated", _fail_if_called)

        response = client.post(
            "/ask",
            json={"question": "How many rows are there?", "model": "not-a-real-model:1b"},
        )

        assert response.status_code == 400
        assert "not-a-real-model:1b" in response.json()["detail"]

    def test_disallowed_model_does_not_consume_a_concurrency_slot(self, monkeypatch, client):
        """A malformed/disallowed model request must be rejected before any
        admission-control slot is acquired -- proven by a concurrency limit
        of 1 still allowing a second, valid request through immediately
        after the rejected one."""
        monkeypatch.setattr(
            "api.main.get_settings",
            lambda: _settings(
                max_concurrent_ask_requests=1, max_concurrent_ask_requests_per_caller=1
            ),
        )
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        rejected = client.post("/ask", json={"question": "q1", "model": "not-allowed:1b"})
        assert rejected.status_code == 400

        accepted = client.post("/ask", json={"question": "q2"})
        assert accepted.status_code == 200

    def test_selection_disabled_rejects_any_non_default_model(self, monkeypatch, client):
        disabled_settings = _settings(
            ollama_model="llama3.1:8b", ollama_model_selection_enabled=False
        )
        monkeypatch.setattr("api.main.get_settings", lambda: disabled_settings)

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("must not be called when the model is rejected")

        monkeypatch.setattr("api.main.run_orchestrated", _fail_if_called)

        response = client.post("/ask", json={"question": "How many rows?", "model": "qwen2.5:7b"})

        assert response.status_code == 400

    def test_extra_long_model_string_is_rejected_by_request_validation(self, client):
        response = client.post("/ask", json={"question": "How many rows?", "model": "x" * 200})
        assert response.status_code == 422

    def test_disallowed_model_is_audit_logged(self, monkeypatch, client):
        events = []
        monkeypatch.setattr(
            "api.main.log_security_event",
            lambda event_type, severity, message, **kwargs: events.append(
                (event_type, severity, kwargs)
            ),
        )

        response = client.post("/ask", json={"question": "q", "model": "not-allowed:1b"})

        assert response.status_code == 400
        assert any(e[0] == "invalid_model_selection" for e in events)
        logged_kwargs = next(e[2] for e in events if e[0] == "invalid_model_selection")
        assert logged_kwargs["requested_model"] == "not-allowed:1b"
