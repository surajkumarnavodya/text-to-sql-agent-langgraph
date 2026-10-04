"""Unit tests for api/chat_persistence.py -- the universal conversation-
history write path (2026-09-27 pass).

Same real-in-memory-SQLite-identity-DB convention as
tests/test_identity_repository_history.py: `identity.db.get_identity_engine`
is monkeypatched to a throwaway `sqlite:///:memory:` engine, so
`persist_ask_turn`/`persist_execute_result` exercise their real DB writes,
not a mock. No real Ollama/DB/network call is made anywhere here -- both
functions only ever persist an already-computed `AskResponse`/
`ExecuteResponse`.
"""

from __future__ import annotations

import uuid

import identity.db as identity_db_mod
import pytest
from identity.models import Base, User
from identity.repositories.history import get_conversation, list_messages
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.chat_persistence import (
    _answer_text_for_history,
    _build_history_metadata,
    persist_ask_turn,
    persist_execute_result,
)
from api.schemas import (
    AskResponse,
    AttachmentResultOut,
    ChartRecommendationOut,
    ExecuteResponse,
    MediaGenerationResultOut,
    MediaSearchResultOut,
    SchemaTableOut,
    SourceAnswerOut,
)
from config.settings import Settings
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
    log_level="INFO",
    log_redaction_level="standard",
    local_auth_enabled=True,
    auth_database_url=SecretStr("postgresql://placeholder/unused"),
    jwt_secret_key=SecretStr("s" * 40),
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


def _ask_response(**overrides: object) -> AskResponse:
    base: dict[str, object] = {
        "session_id": "s1",
        "conversation_id": None,
        "message_id": None,
        "status": "succeeded",
        "database": "default",
        "model": "llama3.1:8b",
        "sql": None,
        "result_columns": None,
        "result_rows": None,
        "row_count": None,
        "retry_count": 0,
        "attempt_history": [],
        "insight": None,
        "cost_notice": None,
        "low_confidence_notice": None,
        "rejection_reason": None,
        "rejection_message": None,
        "rate_limit_message": None,
        "clarification_message": None,
        "failure_explanation": None,
        "error_history": [],
        "sources_used": [],
        "synthesized_answer": None,
        "document_result": None,
        "policy_result": None,
        "web_result": None,
        "generation_result": None,
        "media_search_result": None,
        "attachment_result": None,
        "query_plan": None,
        "schema_tables": [],
        "followup_classification": None,
        "followup_resolved_against": None,
        "permission_denied_notice": None,
    }
    base.update(overrides)
    return AskResponse(**base)


@pytest.fixture
def identity_engine(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    return engine


def _make_user(engine, email: str = "alice@example.com") -> uuid.UUID:
    factory = sessionmaker(bind=engine)
    session: Session = factory()
    user = User(email=email, password_hash="x", display_name="Alice")
    session.add(user)
    session.commit()
    session.refresh(user)
    user_id = user.id
    session.close()
    return user_id


def _local_identity(user_id: uuid.UUID) -> AuthIdentity:
    return AuthIdentity(subject=str(user_id), roles=("user",), mode="local")


class TestAnswerTextForHistory:
    def test_prefers_insight(self):
        response = _ask_response(insight="42 rows", synthesized_answer="fallback")
        assert _answer_text_for_history(response) == "42 rows"

    def test_falls_back_to_synthesized_answer(self):
        response = _ask_response(synthesized_answer="Combined answer")
        assert _answer_text_for_history(response) == "Combined answer"

    def test_falls_back_to_document_result(self):
        response = _ask_response(
            document_result=SourceAnswerOut(answer="From docs", citations=[], status="succeeded")
        )
        assert _answer_text_for_history(response) == "From docs"

    def test_falls_back_to_generation_result(self):
        response = _ask_response(
            generation_result=MediaGenerationResultOut(
                answer="Generated an image.", status="succeeded"
            )
        )
        assert _answer_text_for_history(response) == "Generated an image."

    def test_falls_back_to_media_search_result(self):
        response = _ask_response(
            media_search_result=MediaSearchResultOut(
                answer="Found 2 result(s).", status="succeeded"
            )
        )
        assert _answer_text_for_history(response) == "Found 2 result(s)."

    def test_falls_back_to_attachment_result(self):
        response = _ask_response(
            attachment_result=AttachmentResultOut(answer="It's an invoice.", status="succeeded")
        )
        assert _answer_text_for_history(response) == "It's an invoice."

    def test_succeeded_sql_with_no_narrative_falls_back_to_row_count(self):
        response = _ask_response(row_count=5)
        assert _answer_text_for_history(response) == "Query succeeded. 5 row(s) returned."

    def test_succeeded_sql_with_no_row_count_at_all(self):
        response = _ask_response(row_count=None)
        assert _answer_text_for_history(response) == "Query succeeded."

    def test_failed_uses_failure_explanation(self):
        response = _ask_response(status="failed", failure_explanation="Could not generate SQL.")
        assert _answer_text_for_history(response) == "Could not generate SQL."

    def test_rejected_uses_rejection_message(self):
        response = _ask_response(status="rejected", rejection_message="Off-topic question.")
        assert _answer_text_for_history(response) == "Off-topic question."

    def test_generic_fallback_when_nothing_else_is_set(self):
        response = _ask_response(status="failed")
        assert _answer_text_for_history(response) == "The request could not be completed."


class TestBuildHistoryMetadata:
    def test_saves_the_answer_duration_so_a_reopened_turn_can_show_it(self):
        response = _ask_response(answer_duration_ms=4321.5)
        metadata = _build_history_metadata(response, _BASE_SETTINGS, attachment_refs=[])
        assert metadata["answer_duration_ms"] == 4321.5

    def test_missing_duration_is_saved_as_null_not_zero(self):
        metadata = _build_history_metadata(_ask_response(), _BASE_SETTINGS, attachment_refs=[])
        assert metadata["answer_duration_ms"] is None

    def test_captures_real_sources_used_never_fabricated_empty(self):
        response = _ask_response(sources_used=["documents", "web"], synthesized_answer="Combined")
        metadata = _build_history_metadata(response, _BASE_SETTINGS, attachment_refs=[])
        assert metadata["schema_version"] == 2
        assert metadata["sources_used"] == ["documents", "web"]
        assert metadata["synthesized_answer"] == "Combined"

    def test_never_includes_internal_only_preview_rows(self):
        response = _ask_response(result_columns=["x"], result_rows=[[1]])
        metadata = _build_history_metadata(response, _BASE_SETTINGS, attachment_refs=[])
        assert "result_columns" not in metadata
        assert "result_rows" not in metadata
        # result_snapshot starts unset -- only persist_execute_result ever
        # populates it, once the user actually confirms and runs the SQL.
        assert metadata["result_snapshot"] is None

    def test_truncates_long_text_fields(self):
        settings = _settings(chat_history_max_text_chars=10)
        response = _ask_response(insight="x" * 50)
        metadata = _build_history_metadata(response, settings, attachment_refs=[])
        assert metadata["insight"] == "x" * 10 + "…"

    def test_schema_tables_drop_ddl_and_cap_count(self):
        tables = [
            SchemaTableOut(table_name=f"t{i}", ddl="CREATE TABLE ...", similarity_score=0.9)
            for i in range(20)
        ]
        response = _ask_response(schema_tables=tables)
        metadata = _build_history_metadata(response, _BASE_SETTINGS, attachment_refs=[])
        assert len(metadata["schema_tables"]) == 8
        assert all("ddl" not in table for table in metadata["schema_tables"])

    def test_carries_attachment_refs_through_unchanged(self):
        refs = [{"attachment_id": "a1", "filename": "invoice.pdf", "media_type": "application/pdf"}]
        response = _ask_response()
        metadata = _build_history_metadata(response, _BASE_SETTINGS, attachment_refs=refs)
        assert metadata["attachment_refs"] == refs


class TestPersistAskTurn:
    def test_returns_none_none_when_identity_is_not_local(self, identity_engine):
        response = _ask_response()
        identity = AuthIdentity(subject="whoever", roles=("user",), mode="oidc")
        result = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="Q",
            ask_response=response,
            settings=_BASE_SETTINGS,
        )
        assert result == (None, None)

    def test_returns_none_none_when_local_auth_disabled(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        result = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="Q",
            ask_response=_ask_response(),
            settings=_settings(local_auth_enabled=False),
        )
        assert result == (None, None)

    def test_creates_conversation_and_message_and_returns_real_ids(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        response = _ask_response(sql="SELECT 1", sources_used=[], row_count=1)

        conversation_id, message_id = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="How many rows?",
            ask_response=response,
            settings=_BASE_SETTINGS,
        )
        assert conversation_id is not None
        assert message_id is not None

        factory = sessionmaker(bind=identity_engine)
        session: Session = factory()
        conversation = get_conversation(
            session, conversation_id=uuid.UUID(conversation_id), user_id=user_id
        )
        assert conversation is not None
        rows, _ = list_messages(
            session, conversation_id=conversation.id, user_id=user_id, limit=10, offset=0
        )
        session.close()
        assistant_row = next(r for r in rows if r.role == "assistant")
        assert str(assistant_row.id) == message_id
        assert assistant_row.metadata["sql"] == "SELECT 1"
        assert assistant_row.metadata["sources_used"] == []

    def test_persists_real_sources_used_for_a_web_only_turn(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        response = _ask_response(
            sources_used=["web"],
            web_result=SourceAnswerOut(answer="From the web", citations=[], status="succeeded"),
            synthesized_answer=None,
        )
        conversation_id, message_id = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="What's new?",
            ask_response=response,
            settings=_BASE_SETTINGS,
        )
        assert conversation_id is not None and message_id is not None

        factory = sessionmaker(bind=identity_engine)
        session: Session = factory()
        rows, _ = list_messages(
            session, conversation_id=uuid.UUID(conversation_id), user_id=user_id, limit=10, offset=0
        )
        session.close()
        assistant_row = next(r for r in rows if r.role == "assistant")
        assert assistant_row.metadata["sources_used"] == ["web"]
        assert assistant_row.metadata["web_result"]["answer"] == "From the web"
        # A web-only turn has no SQL -- the reconstruction must never invent one.
        assert assistant_row.metadata["sql"] is None


class TestPersistExecuteResult:
    def test_no_op_without_a_message_id(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        ok = persist_execute_result(
            identity=identity,
            conversation_id=None,
            message_id=None,
            sql="SELECT 1",
            execute_response=ExecuteResponse(status="succeeded", database="default"),
            settings=_BASE_SETTINGS,
        )
        assert ok is False

    def test_no_op_for_unowned_message_id(self, identity_engine):
        owner_id = _make_user(identity_engine, "owner@example.com")
        stranger_id = _make_user(identity_engine, "stranger@example.com")
        owner_identity = _local_identity(owner_id)
        _, message_id = persist_ask_turn(
            identity=owner_identity,
            conversation_id=None,
            question="Q",
            ask_response=_ask_response(sql="SELECT 1"),
            settings=_BASE_SETTINGS,
        )
        assert message_id is not None

        stranger_identity = _local_identity(stranger_id)
        ok = persist_execute_result(
            identity=stranger_identity,
            conversation_id=None,
            message_id=message_id,
            sql="SELECT 1",
            execute_response=ExecuteResponse(
                status="succeeded",
                database="default",
                normalized_sql="SELECT 1",
                result_columns=["x"],
                result_rows=[[1]],
                row_count=1,
            ),
            settings=_BASE_SETTINGS,
        )
        assert ok is False

    def test_attaches_bounded_result_snapshot_to_the_right_turn(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        conversation_id, message_id = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="How many rows?",
            ask_response=_ask_response(sql="SELECT * FROM t"),
            settings=_BASE_SETTINGS,
        )
        assert message_id is not None

        execute_response = ExecuteResponse(
            status="succeeded",
            database="default",
            normalized_sql="SELECT TOP 100 * FROM t",
            result_columns=["a", "b"],
            result_rows=[[i, i * 2] for i in range(5)],
            row_count=5,
            duration_ms=12.5,
            column_types={"a": "numeric", "b": "numeric"},
            chart_recommendation=ChartRecommendationOut(chart_type="bar", reason="numeric column"),
            truncated=False,
        )
        ok = persist_execute_result(
            identity=identity,
            conversation_id=conversation_id,
            message_id=message_id,
            sql="SELECT * FROM t",
            execute_response=execute_response,
            settings=_BASE_SETTINGS,
        )
        assert ok is True

        factory = sessionmaker(bind=identity_engine)
        session: Session = factory()
        rows, _ = list_messages(
            session, conversation_id=uuid.UUID(conversation_id), user_id=user_id, limit=10, offset=0
        )
        session.close()
        assistant_row = next(r for r in rows if r.role == "assistant")
        snapshot = assistant_row.metadata["result_snapshot"]
        assert snapshot["columns"] == ["a", "b"]
        assert snapshot["row_count"] == 5
        assert snapshot["returned_rows"] == 5
        # The confirmed, actually-executed SQL replaces the draft one --
        # same "save what was actually run" principle golden examples use.
        assert assistant_row.metadata["sql"] == "SELECT TOP 100 * FROM t"

    def test_bounds_stored_rows_to_the_configured_cap(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        settings = _settings(chat_history_max_result_rows=2)
        conversation_id, message_id = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="Q",
            ask_response=_ask_response(sql="SELECT * FROM t"),
            settings=settings,
        )
        execute_response = ExecuteResponse(
            status="succeeded",
            database="default",
            normalized_sql="SELECT * FROM t",
            result_columns=["a"],
            result_rows=[[i] for i in range(10)],
            row_count=10,
        )
        ok = persist_execute_result(
            identity=identity,
            conversation_id=conversation_id,
            message_id=message_id,
            sql="SELECT * FROM t",
            execute_response=execute_response,
            settings=settings,
        )
        assert ok is True

        factory = sessionmaker(bind=identity_engine)
        session: Session = factory()
        rows, _ = list_messages(
            session, conversation_id=uuid.UUID(conversation_id), user_id=user_id, limit=10, offset=0
        )
        session.close()
        assistant_row = next(r for r in rows if r.role == "assistant")
        snapshot = assistant_row.metadata["result_snapshot"]
        assert snapshot["returned_rows"] == 2
        assert snapshot["truncated"] is True

    def test_mismatched_conversation_id_is_rejected(self, identity_engine):
        user_id = _make_user(identity_engine)
        identity = _local_identity(user_id)
        _, message_id = persist_ask_turn(
            identity=identity,
            conversation_id=None,
            question="Q",
            ask_response=_ask_response(sql="SELECT 1"),
            settings=_BASE_SETTINGS,
        )
        ok = persist_execute_result(
            identity=identity,
            conversation_id=str(uuid.uuid4()),  # a real uuid, but not this turn's own
            message_id=message_id,
            sql="SELECT 1",
            execute_response=ExecuteResponse(
                status="succeeded", database="default", normalized_sql="SELECT 1", row_count=0
            ),
            settings=_BASE_SETTINGS,
        )
        assert ok is False
