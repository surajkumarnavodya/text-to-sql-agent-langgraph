"""Unit tests for identity/repositories/history.py -- against a real
in-memory SQLite database, same convention as
tests/test_identity_repository_users.py (see that module's own docstring).
"""

from __future__ import annotations

import uuid

import pytest
from identity.models import Base, User
from identity.repositories.history import (
    append_single_message,
    append_turn,
    create_conversation,
    get_ai_output,
    get_conversation,
    list_conversations,
    list_messages,
    search_history,
    soft_delete_conversation,
    update_ai_output_metadata,
    update_conversation,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _make_user(session: Session, email: str = "alice@example.com") -> User:
    user = User(email=email, password_hash="x", display_name="Alice")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


class TestCreateAndGetConversation:
    def test_create_then_get_round_trips(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id, title="My chat")
        fetched = get_conversation(db_session, conversation_id=conversation.id, user_id=user.id)
        assert fetched is not None
        assert fetched.title == "My chat"

    def test_get_returns_none_for_wrong_user(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        stranger = _make_user(db_session, "stranger@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        assert (
            get_conversation(db_session, conversation_id=conversation.id, user_id=stranger.id)
            is None
        )

    def test_get_returns_none_for_unknown_id(self, db_session: Session):
        user = _make_user(db_session)
        assert get_conversation(db_session, conversation_id=uuid.uuid4(), user_id=user.id) is None

    def test_deleted_conversation_is_not_returned(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        soft_delete_conversation(db_session, conversation=conversation)
        assert (
            get_conversation(db_session, conversation_id=conversation.id, user_id=user.id) is None
        )


class TestListConversations:
    def test_only_lists_the_caller_own_conversations(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        stranger = _make_user(db_session, "stranger@example.com")
        create_conversation(db_session, user_id=owner.id, title="Owner's chat")
        create_conversation(db_session, user_id=stranger.id, title="Stranger's chat")

        page, total = list_conversations(db_session, user_id=owner.id, limit=10, offset=0)
        assert total == 1
        assert page[0].title == "Owner's chat"

    def test_pagination_limit_and_offset(self, db_session: Session):
        user = _make_user(db_session)
        for i in range(5):
            create_conversation(db_session, user_id=user.id, title=f"Chat {i}")

        page1, total = list_conversations(db_session, user_id=user.id, limit=2, offset=0)
        page2, _ = list_conversations(db_session, user_id=user.id, limit=2, offset=2)
        assert total == 5
        assert len(page1) == 2
        assert len(page2) == 2
        assert {c.id for c in page1}.isdisjoint({c.id for c in page2})

    def test_archived_excluded_by_default(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        update_conversation(db_session, conversation=conversation, archived=True)

        active_only, total_active = list_conversations(
            db_session, user_id=user.id, limit=10, offset=0
        )
        with_archived, total_all = list_conversations(
            db_session, user_id=user.id, limit=10, offset=0, include_archived=True
        )
        assert total_active == 0
        assert active_only == []
        assert total_all == 1
        assert with_archived[0].id == conversation.id

    def test_deleted_never_listed_even_with_include_archived(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        soft_delete_conversation(db_session, conversation=conversation)
        page, total = list_conversations(
            db_session, user_id=user.id, limit=10, offset=0, include_archived=True
        )
        assert total == 0
        assert page == []

    def test_most_recent_activity_first(self, db_session: Session):
        user = _make_user(db_session)
        older = create_conversation(db_session, user_id=user.id, title="Older")
        newer = create_conversation(db_session, user_id=user.id, title="Newer")
        # Give "older" an explicit, earlier last_message_at via a real turn,
        # then "newer" a later one, so ordering is driven by activity, not
        # just creation order.
        append_turn(
            db_session, conversation=older, user_id=user.id, question="q1", answer_text="a1"
        )
        append_turn(
            db_session, conversation=newer, user_id=user.id, question="q2", answer_text="a2"
        )

        page, _ = list_conversations(db_session, user_id=user.id, limit=10, offset=0)
        assert [c.title for c in page] == ["Newer", "Older"]


class TestUpdateConversation:
    def test_rename_updates_title(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id, title="Old title")
        updated = update_conversation(db_session, conversation=conversation, title="New title")
        assert updated.title == "New title"

    def test_archive_and_unarchive(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        archived = update_conversation(db_session, conversation=conversation, archived=True)
        assert archived.archived_at is not None
        unarchived = update_conversation(db_session, conversation=conversation, archived=False)
        assert unarchived.archived_at is None


class TestSoftDelete:
    def test_soft_delete_sets_deleted_at_and_preserves_row(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        soft_delete_conversation(db_session, conversation=conversation)
        db_session.refresh(conversation)
        assert conversation.deleted_at is not None


class TestAppendTurnAndMessageOrdering:
    def test_append_turn_creates_prompt_and_ai_output_with_adjacent_sequence_numbers(
        self, db_session: Session
    ):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        prompt, ai_output = append_turn(
            db_session, conversation=conversation, user_id=user.id, question="Q1", answer_text="A1"
        )
        assert ai_output.sequence_number == prompt.sequence_number + 1

    def test_first_turn_derives_conversation_title(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)  # no title
        append_turn(
            db_session,
            conversation=conversation,
            user_id=user.id,
            question="What is the meaning of life?",
            answer_text="42",
        )
        db_session.refresh(conversation)
        assert conversation.title == "What is the meaning of life?"

    def test_existing_title_is_not_overwritten(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id, title="Custom title")
        append_turn(
            db_session, conversation=conversation, user_id=user.id, question="Q", answer_text="A"
        )
        db_session.refresh(conversation)
        assert conversation.title == "Custom title"

    def test_multiple_turns_are_ordered_by_sequence_number(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        for i in range(3):
            append_turn(
                db_session,
                conversation=conversation,
                user_id=user.id,
                question=f"Q{i}",
                answer_text=f"A{i}",
            )

        rows, total_turns = list_messages(
            db_session, conversation_id=conversation.id, user_id=user.id, limit=10, offset=0
        )
        assert total_turns == 3
        contents = [r.content for r in rows]
        assert contents == ["Q0", "A0", "Q1", "A1", "Q2", "A2"]
        roles = [r.role for r in rows]
        assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]

    def test_list_messages_is_ownership_scoped(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        stranger = _make_user(db_session, "stranger@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        append_turn(
            db_session, conversation=conversation, user_id=owner.id, question="Q", answer_text="A"
        )
        rows, total = list_messages(
            db_session, conversation_id=conversation.id, user_id=stranger.id, limit=10, offset=0
        )
        assert rows == []
        assert total == 0

    def test_append_single_message_shares_the_same_sequence_counter(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        append_turn(
            db_session, conversation=conversation, user_id=user.id, question="Q1", answer_text="A1"
        )
        extra = append_single_message(
            db_session, conversation=conversation, user_id=user.id, role="user", content="Q2 manual"
        )
        assert extra.sequence_number == 3  # after prompt(1)/ai_output(2) from the first turn


class TestGetAndUpdateAiOutput:
    """Universal conversation history (2026-09-27): `get_ai_output`/
    `update_ai_output_metadata` back `api.chat_persistence
    .persist_execute_result` -- the "attach a confirmed Confirm-and-Run
    result to the exact turn it belongs to" write path."""

    def test_get_returns_the_owned_output(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        _, ai_output = append_turn(
            db_session, conversation=conversation, user_id=user.id, question="Q", answer_text="A"
        )
        fetched = get_ai_output(db_session, ai_output_id=ai_output.id, user_id=user.id)
        assert fetched is not None
        assert fetched.id == ai_output.id

    def test_get_returns_none_for_wrong_user(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        stranger = _make_user(db_session, "stranger@example.com")
        conversation = create_conversation(db_session, user_id=owner.id)
        _, ai_output = append_turn(
            db_session, conversation=conversation, user_id=owner.id, question="Q", answer_text="A"
        )
        assert get_ai_output(db_session, ai_output_id=ai_output.id, user_id=stranger.id) is None

    def test_get_returns_none_for_unknown_id(self, db_session: Session):
        user = _make_user(db_session)
        assert get_ai_output(db_session, ai_output_id=uuid.uuid4(), user_id=user.id) is None

    def test_update_replaces_metadata_and_persists(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        _, ai_output = append_turn(
            db_session,
            conversation=conversation,
            user_id=user.id,
            question="Q",
            answer_text="A",
            output_metadata={"schema_version": 2, "sql": "SELECT 1"},
        )
        updated = update_ai_output_metadata(
            db_session,
            ai_output=ai_output,
            metadata={
                "schema_version": 2,
                "sql": "SELECT 1",
                "result_snapshot": {"columns": ["x"]},
            },
        )
        assert updated.metadata_json["result_snapshot"] == {"columns": ["x"]}

        refetched = get_ai_output(db_session, ai_output_id=ai_output.id, user_id=user.id)
        assert refetched is not None
        assert refetched.metadata_json["result_snapshot"] == {"columns": ["x"]}


class TestSearchHistory:
    def test_finds_match_in_conversation_title(self, db_session: Session):
        user = _make_user(db_session)
        create_conversation(db_session, user_id=user.id, title="Quarterly sales review")
        hits, total = search_history(db_session, user_id=user.id, query="sales", limit=10, offset=0)
        assert total == 1
        assert hits[0].matched_in == "title"

    def test_finds_match_in_user_message_content(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        append_turn(
            db_session,
            conversation=conversation,
            user_id=user.id,
            question="What were total widget sales in 2024?",
            answer_text="1200 units",
        )
        hits, total = search_history(
            db_session, user_id=user.id, query="widget", limit=10, offset=0
        )
        assert total == 1
        assert hits[0].matched_in == "message"
        assert "widget" in hits[0].snippet.lower()

    def test_finds_match_in_assistant_message_content(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id)
        append_turn(
            db_session,
            conversation=conversation,
            user_id=user.id,
            question="Summarize",
            answer_text="Revenue grew by twelve percent year over year.",
        )
        hits, total = search_history(
            db_session, user_id=user.id, query="twelve percent", limit=10, offset=0
        )
        assert total == 1

    def test_case_insensitive(self, db_session: Session):
        user = _make_user(db_session)
        create_conversation(db_session, user_id=user.id, title="Widget Report")
        hits, total = search_history(
            db_session, user_id=user.id, query="WIDGET", limit=10, offset=0
        )
        assert total == 1

    def test_partial_match(self, db_session: Session):
        user = _make_user(db_session)
        create_conversation(db_session, user_id=user.id, title="Quarterly sales review")
        hits, total = search_history(db_session, user_id=user.id, query="quart", limit=10, offset=0)
        assert total == 1

    def test_empty_query_returns_no_results(self, db_session: Session):
        user = _make_user(db_session)
        create_conversation(db_session, user_id=user.id, title="Anything")
        hits, total = search_history(db_session, user_id=user.id, query="   ", limit=10, offset=0)
        assert hits == []
        assert total == 0

    def test_special_characters_do_not_error(self, db_session: Session):
        user = _make_user(db_session)
        create_conversation(db_session, user_id=user.id, title="Report 100% done")
        hits, total = search_history(
            db_session, user_id=user.id, query="100%_test", limit=10, offset=0
        )
        assert hits == []  # no match, but must not raise

    def test_never_returns_another_users_results(self, db_session: Session):
        owner = _make_user(db_session, "owner@example.com")
        stranger = _make_user(db_session, "stranger@example.com")
        create_conversation(db_session, user_id=owner.id, title="Owner's secret project")
        hits, total = search_history(
            db_session, user_id=stranger.id, query="secret", limit=10, offset=0
        )
        assert hits == []
        assert total == 0

    def test_pagination(self, db_session: Session):
        user = _make_user(db_session)
        for i in range(5):
            create_conversation(db_session, user_id=user.id, title=f"Report number {i}")
        page1, total = search_history(
            db_session, user_id=user.id, query="report", limit=2, offset=0
        )
        page2, _ = search_history(db_session, user_id=user.id, query="report", limit=2, offset=2)
        assert total == 5
        assert len(page1) == 2
        assert len(page2) == 2

    def test_deleted_conversations_excluded_from_search(self, db_session: Session):
        user = _make_user(db_session)
        conversation = create_conversation(db_session, user_id=user.id, title="Deleted chat")
        soft_delete_conversation(db_session, conversation=conversation)
        hits, total = search_history(
            db_session, user_id=user.id, query="deleted", limit=10, offset=0
        )
        assert total == 0
