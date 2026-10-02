"""Unit tests for api/recommendation_persistence.py -- Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`)'s `/ask` -> governed-
recommendation-store write path.

Same real-in-memory-SQLite-identity-DB convention as
tests/test_chat_persistence.py: `identity.db.get_identity_engine` is
monkeypatched to a throwaway `sqlite:///:memory:` engine, so
`persist_ask_recommendations` exercises its real DB writes, not a mock.
"""

from __future__ import annotations

import uuid

import identity.db as identity_db_mod
import pytest
from identity.models import Base, User
from identity.repositories.recommendation_governance import list_records
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.recommendation_persistence import persist_ask_recommendations
from api.schemas import AskResponse
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


_ONE_RECOMMENDATION = {
    "kind": "action",
    "claim": {
        "value": "Investigate the anomalous spike.",
        "level": "ai_inference",
        "grounded_in": [],
        "source": "recommendation.engine.AnomalyRule",
    },
    "rationale": None,
    "category": "anomaly",
    "evidence": [
        {
            "value": "Value at 2024 flagged anomalous.",
            "level": "database_fact",
            "grounded_in": [],
            "source": "analytics.engine",
        }
    ],
    "affected_entity": "2024",
    "action": "Investigate.",
    "measurable_impact": None,
    "confidence": 0.8,
    "rule_or_model": "recommendation.engine.AnomalyRule",
    "limitations": [],
    "generated_at": "2026-10-02T00:00:00+00:00",
    "engine_version": "1.0.0",
}


def _ask_response(**overrides: object) -> AskResponse:
    base: dict[str, object] = {
        "session_id": "s1",
        "conversation_id": None,
        "message_id": None,
        "status": "succeeded",
        "database": "hr",
        "model": "llama3.1:8b",
        "sql": "SELECT Year, SUM(Sales) FROM t GROUP BY Year",
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
        "recommendations": [],
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


class TestPersistAskRecommendations:
    def test_persists_each_recommendation_as_a_record(self, identity_engine):
        user_id = _make_user(identity_engine)
        response = _ask_response(recommendations=[dict(_ONE_RECOMMENDATION)])
        count = persist_ask_recommendations(
            identity=_local_identity(user_id),
            question="why did sales spike?",
            ask_response=response,
            settings=_settings(),
        )
        assert count == 1

        factory = sessionmaker(bind=identity_engine)
        session = factory()
        records = list_records(session, "default")
        session.close()
        assert len(records) == 1
        assert records[0].category == "anomaly"
        assert records[0].database_id == "hr"
        assert records[0].source_question == "why did sales spike?"
        assert records[0].source_sql == response.sql

    def test_no_recommendations_persists_nothing(self, identity_engine):
        user_id = _make_user(identity_engine)
        response = _ask_response(recommendations=[])
        count = persist_ask_recommendations(
            identity=_local_identity(user_id),
            question="q",
            ask_response=response,
            settings=_settings(),
        )
        assert count == 0

    def test_oidc_identity_is_skipped(self, identity_engine):
        response = _ask_response(recommendations=[dict(_ONE_RECOMMENDATION)])
        identity = AuthIdentity(subject="sub-123", roles=("user",), mode="oidc")
        count = persist_ask_recommendations(
            identity=identity, question="q", ask_response=response, settings=_settings()
        )
        assert count == 0

    def test_local_auth_disabled_skips_persistence(self, identity_engine):
        user_id = _make_user(identity_engine)
        response = _ask_response(recommendations=[dict(_ONE_RECOMMENDATION)])
        count = persist_ask_recommendations(
            identity=_local_identity(user_id),
            question="q",
            ask_response=response,
            settings=_settings(local_auth_enabled=False),
        )
        assert count == 0

    def test_feature_flag_off_skips_persistence(self, identity_engine):
        user_id = _make_user(identity_engine)
        response = _ask_response(recommendations=[dict(_ONE_RECOMMENDATION)])
        count = persist_ask_recommendations(
            identity=_local_identity(user_id),
            question="q",
            ask_response=response,
            settings=_settings(enable_recommendation_persistence=False),
        )
        assert count == 0

    def test_malformed_recommendation_dict_is_skipped_not_fatal(self, identity_engine):
        user_id = _make_user(identity_engine)
        response = _ask_response(
            recommendations=[{"not": "a valid recommendation"}, dict(_ONE_RECOMMENDATION)]
        )
        count = persist_ask_recommendations(
            identity=_local_identity(user_id),
            question="q",
            ask_response=response,
            settings=_settings(),
        )
        assert count == 1

    def test_unreachable_identity_db_fails_open(self, monkeypatch):
        def _raise(settings=None):
            raise RuntimeError("db unreachable")

        monkeypatch.setattr(identity_db_mod, "get_identity_session", _raise)
        response = _ask_response(recommendations=[dict(_ONE_RECOMMENDATION)])
        count = persist_ask_recommendations(
            identity=_local_identity(uuid.uuid4()),
            question="q",
            ask_response=response,
            settings=_settings(),
        )
        assert count == 0

    def test_non_uuid_subject_fails_open(self, identity_engine):
        identity = AuthIdentity(subject="not-a-uuid", roles=("user",), mode="local")
        response = _ask_response(recommendations=[dict(_ONE_RECOMMENDATION)])
        count = persist_ask_recommendations(
            identity=identity, question="q", ask_response=response, settings=_settings()
        )
        assert count == 0

    def test_no_database_defaults_to_default_database_id(self, identity_engine):
        user_id = _make_user(identity_engine)
        response = _ask_response(database=None, recommendations=[dict(_ONE_RECOMMENDATION)])
        persist_ask_recommendations(
            identity=_local_identity(user_id),
            question="q",
            ask_response=response,
            settings=_settings(),
        )
        factory = sessionmaker(bind=identity_engine)
        session = factory()
        records = list_records(session, "default")
        session.close()
        assert records[0].database_id == "default"
