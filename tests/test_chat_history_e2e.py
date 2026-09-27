"""End-to-end HTTP regression tests for the universal conversation-history
fix (2026-09-27): `POST /ask` -> `POST /execute` -> `GET /conversations/{id}
/messages`, for every source type this app actually supports, against a
real in-memory-SQLite identity database (same convention as
tests/test_api_chat_history.py) and a real TestClient.

`agent.orchestrator.graph.run_orchestrated` is the only agent-side thing
mocked (exactly like tests/test_api_ask.py) -- no real Ollama/DB/Chroma call
is ever made. `execute_readonly_sql`/`get_read_only_engine` are mocked the
same way tests/test_api_execute.py already does for the one scenario that
needs a real "Confirm and Run" round trip.

Each test class name states which source type it verifies -- see this
file's own docstring history in api/chat_persistence.py for why this
matters: the implementation prompt this closes explicitly asks "report
exactly which source types you verified," so this file's class list *is*
that report, kept executable rather than just claimed.
"""

from __future__ import annotations

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import agent.rate_limit as rate_limit_module
import api.auth as api_auth_mod
import api.chat_history as chat_history_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.rate_limit as api_rate_limit_mod
from config.settings import Settings
from security.secrets import SecretStr

_SETTINGS = Settings(
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
    max_result_rows=1000,
    query_timeout_seconds=15,
    log_level="INFO",
    log_redaction_level="standard",
    local_auth_enabled=True,
    auth_database_url=SecretStr("postgresql://placeholder/unused"),
    jwt_secret_key=SecretStr("s" * 40),
    jwt_issuer="text-to-sql-agent",
    jwt_audience="text-to-sql-web",
    allow_public_registration=True,
    cookie_secure=False,
    password_min_length=12,
    login_rate_limit_per_minute=1000,
    register_rate_limit_per_hour=1000,
    api_action_rate_limit_per_minute=1000,
)


@pytest.fixture(autouse=True)
def _identity_test_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    for module in (api_auth_mod, identity_auth_mod, identity_authz_mod, chat_history_mod, api_main):
        monkeypatch.setattr(module, "get_settings", lambda: _SETTINGS)

    from identity.db import get_identity_session

    session = get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()
    api_main._ip_limiters.clear()
    api_rate_limit_mod._limiters.clear()
    rate_limit_module._ask_concurrency_limiter = None
    rate_limit_module._per_caller_ask_concurrency_limiters.clear()

    yield engine

    api_main._ip_limiters.clear()
    api_rate_limit_mod._limiters.clear()
    rate_limit_module._ask_concurrency_limiter = None
    rate_limit_module._per_caller_ask_concurrency_limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _register_and_login(client: TestClient, email: str, display_name: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "correcthorse battery staple 1",
            "display_name": display_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth_headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _ask(client: TestClient, headers: dict, question: str, **extra) -> dict:
    response = client.post("/ask", json={"question": question, **extra}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _messages(client: TestClient, headers: dict, conversation_id: str) -> list[dict]:
    response = client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def _assistant_metadata(messages: list[dict]) -> dict:
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["metadata"] is not None
    return assistant["metadata"]


class TestSqlSource:
    """SQL text-to-SQL path: no multi-source router involved
    (`sources_used` absent from the raw state, matching this app's own
    "empty/absent means SQL" convention). Covers the full save -> serialize
    -> load -> would-hydrate pipeline for a Confirm-and-Run turn, including
    that reopening never re-executes anything."""

    def test_full_round_trip_including_confirmed_execution_result(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)

        final_state = {
            "status": "succeeded",
            "selected_database": "default",
            "selected_model": "llama3.1:8b",
            "sql": "SELECT COUNT(*) AS cnt FROM orders",
            "row_count": None,
            "retry_count": 0,
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "How many orders are there?")
        assert ask_body["conversation_id"] is not None
        assert ask_body["message_id"] is not None
        assert ask_body["sql"] == "SELECT COUNT(*) AS cnt FROM orders"

        monkeypatch.setattr("api.main.get_read_only_engine", lambda config: object())
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["cnt"], [(42,)]),
        )
        execute_response = client.post(
            "/execute",
            json={
                "sql": ask_body["sql"],
                "conversation_id": ask_body["conversation_id"],
                "message_id": ask_body["message_id"],
            },
            headers=headers,
        )
        assert execute_response.status_code == 200, execute_response.text
        assert execute_response.json()["status"] == "succeeded"

        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["schema_version"] == 2
        assert metadata["sources_used"] == []
        assert metadata["sql"].startswith("SELECT")
        snapshot = metadata["result_snapshot"]
        assert snapshot is not None
        assert snapshot["columns"] == ["cnt"]
        assert snapshot["rows"] == [[42]]
        assert snapshot["row_count"] == 1

    def test_database_question_with_no_generated_sql_has_no_sql_or_result_snapshot(
        self, monkeypatch, client
    ):
        """Regression for the "empty SQL panel with Confirm and Run" bug --
        a turn that never produced SQL at all must not persist a truthy
        `sql`/`result_snapshot` a client could mistake for something
        runnable."""
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "failed",
            "error_history": ["Could not retrieve schema for this question."],
            "failure_explanation": "Could not retrieve schema for this question.",
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "asdkjaslkdj")
        assert ask_body["sql"] is None

        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["sql"] is None
        assert metadata["result_snapshot"] is None
        assert metadata["failure_explanation"] == "Could not retrieve schema for this question."


class TestWebSource:
    def test_web_only_answer_persists_answer_and_citations_not_sql(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sources_used": ["web"],
            "web_result": {
                "answer": "According to a live web search: the sky is blue.",
                "citations": [
                    {
                        "filename": "https://example.com/sky",
                        "chunk_index": 0,
                        "page_number": None,
                        "document_id": "web-1",
                        "has_pdf_bytes": False,
                    }
                ],
                "status": "succeeded",
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "Why is the sky blue?")
        assert ask_body["sql"] is None
        assert ask_body["sources_used"] == ["web"]

        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["sources_used"] == ["web"]
        assert metadata["sql"] is None
        assert metadata["web_result"]["answer"].startswith("According to a live web search")
        assert metadata["web_result"]["citations"][0]["filename"] == "https://example.com/sky"


class TestDocumentsSource:
    def test_documents_only_answer_persists_citations(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sources_used": ["documents"],
            "document_result": {
                "answer": "The onboarding guide says day one starts at 9am.",
                "citations": [
                    {
                        "filename": "onboarding.pdf",
                        "chunk_index": 2,
                        "page_number": 1,
                        "document_id": "doc-1",
                        "has_pdf_bytes": True,
                    }
                ],
                "status": "succeeded",
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "What time does onboarding start?")
        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["sources_used"] == ["documents"]
        assert metadata["document_result"]["citations"][0]["document_id"] == "doc-1"
        assert metadata["sql"] is None


class TestPolicySource:
    def test_policy_only_answer_persists_answer(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sources_used": ["policy"],
            "policy_result": {
                "answer": "Employees accrue 1.5 days of leave per month.",
                "citations": [],
                "status": "succeeded",
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "What's the leave accrual policy?")
        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["sources_used"] == ["policy"]
        assert metadata["policy_result"]["answer"].startswith("Employees accrue")


class TestAttachmentSource:
    def test_attachment_answer_persists_used_ids_and_vision_flag(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sources_used": ["attachments"],
            "attachment_result": {
                "answer": "The attached image shows a dashboard screenshot.",
                "status": "succeeded",
                "used_attachment_ids": ["att-1"],
                "vision_unavailable": False,
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "What does this image show?")
        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["sources_used"] == ["attachments"]
        assert metadata["attachment_result"]["used_attachment_ids"] == ["att-1"]
        assert metadata["attachment_result"]["vision_unavailable"] is False


class TestGenerationSource:
    def test_generation_answer_persists_media_id(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sources_used": ["generation"],
            "generation_result": {
                "answer": "Here's the generated image.",
                "status": "succeeded",
                "media_id": "media-1",
                "media_type": "image",
                "model": "seedance",
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "Generate a picture of a cat.")
        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["generation_result"]["media_id"] == "media-1"


class TestMediaSearchSource:
    def test_media_search_answer_persists_hits(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sources_used": ["media_search"],
            "media_search_result": {
                "answer": "Found 1 result(s).",
                "status": "succeeded",
                "hits": [
                    {
                        "media_id": "m1",
                        "media_type": "image",
                        "caption": "A cat on a rug.",
                        "timestamp_start": None,
                        "timestamp_end": None,
                    }
                ],
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "Find a picture of a cat.")
        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["media_search_result"]["hits"][0]["caption"] == "A cat on a rug."


class TestMixedSources:
    def test_mixed_sql_and_web_answer_restores_both(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "succeeded",
            "sql": "SELECT * FROM sales",
            "sources_used": ["sql", "web"],
            "synthesized_answer": "Combining database sales figures with a live web search.",
            "web_result": {"answer": "Web context.", "citations": [], "status": "succeeded"},
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "Compare our sales with industry trends.")
        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert set(metadata["sources_used"]) == {"sql", "web"}
        assert metadata["sql"] == "SELECT * FROM sales"
        assert metadata["synthesized_answer"].startswith("Combining database")
        assert metadata["web_result"]["answer"] == "Web context."


class TestFailedRequest:
    def test_failed_request_persists_a_structured_readable_error(self, monkeypatch, client):
        tokens = _register_and_login(client, "alice@example.com", "Alice")
        headers = _auth_headers(tokens)
        final_state = {
            "status": "rejected",
            "rejection_reason": "off_topic",
            "rejection_message": "That question isn't about your data.",
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        ask_body = _ask(client, headers, "What's the weather like on Mars?")
        assert ask_body["status"] == "rejected"
        assert isinstance(ask_body["rejection_message"], str)

        messages = _messages(client, headers, ask_body["conversation_id"])
        metadata = _assistant_metadata(messages)
        assert metadata["status"] == "rejected"
        assert metadata["rejection_message"] == "That question isn't about your data."


class TestCrossUserIsolationOnConfirm:
    """A locally-authenticated user must never be able to attach a
    confirmed result to *another* user's saved turn, even by guessing/
    reusing a real message_id -- `POST /execute` silently no-ops instead of
    erroring or corrupting the other user's data (see
    api/chat_persistence.py::persist_execute_result's own ownership check)."""

    def test_bob_cannot_attach_a_result_to_alices_message(self, monkeypatch, client):
        alice_tokens = _register_and_login(client, "alice@example.com", "Alice")
        bob_client = TestClient(api_main.app)
        bob_tokens = _register_and_login(bob_client, "bob@example.com", "Bob")

        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "sql": "SELECT 1"},
        )
        alice_ask = _ask(client, _auth_headers(alice_tokens), "Alice's question")

        monkeypatch.setattr("api.main.get_read_only_engine", lambda config: object())
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["x"], [(1,)]),
        )
        response = bob_client.post(
            "/execute",
            json={
                "sql": "SELECT 1",
                "conversation_id": alice_ask["conversation_id"],
                "message_id": alice_ask["message_id"],
            },
            headers=_auth_headers(bob_tokens),
        )
        # Execution itself is unaffected by an unowned message_id -- only
        # the best-effort history attach is skipped.
        assert response.status_code == 200

        alice_messages = _messages(
            client, _auth_headers(alice_tokens), alice_ask["conversation_id"]
        )
        metadata = _assistant_metadata(alice_messages)
        assert metadata["result_snapshot"] is None


class TestGetMessagesRequiresOwnership:
    def test_conversation_id_must_belong_to_the_caller(self, monkeypatch, client):
        alice_tokens = _register_and_login(client, "alice@example.com", "Alice")
        bob_client = TestClient(api_main.app)
        bob_tokens = _register_and_login(bob_client, "bob@example.com", "Bob")

        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "sql": "SELECT 1"},
        )
        alice_ask = _ask(client, _auth_headers(alice_tokens), "Alice's question")

        response = bob_client.get(
            f"/conversations/{alice_ask['conversation_id']}/messages",
            headers=_auth_headers(bob_tokens),
        )
        assert response.status_code == 404
