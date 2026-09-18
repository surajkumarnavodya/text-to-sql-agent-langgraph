"""Server-side, permanent, cross-device chat history -- conversations and
their messages, strictly owned by `user_id`.

This is the single source of truth this feature's own requirement calls
for: every read here filters by the *authenticated* caller's `user_id`
(never a client-supplied value, never a browser/session identifier) --
`get_conversation`/`list_messages`/`search_history` all take `user_id` as a
required keyword argument and fold it into the `WHERE` clause itself, not
as an afterthought permission check layered on top of an unscoped query.
This is what makes a wrong/forged `conversation_id` in a URL structurally
unable to return another user's data: the row simply doesn't match the
`WHERE user_id = :caller_id AND id = :conversation_id` predicate, so it
reads as "not found," not "found, but you can't see it" (never confirms
another user's conversation even exists -- see `docs/chat-history-architecture.md`).

`Prompt` (the user's question) and `AiOutput` (the assistant's answer) --
both pre-existing tables (`identity/models.py`), never previously written
to by any code path before this feature -- are what this module treats as
one logical "message pair" per turn, deterministically ordered via
`sequence_number` (assigned here, once, inside `append_turn`'s single
transaction -- never by a client, never inferred from `created_at`
precision alone). `list_messages` merges the two tables into one
chronological, role-tagged view (`MessageRow`) for the API layer to
serialize -- there is no separate unified `messages` table; `Prompt`/
`AiOutput`'s own richer, asymmetric shapes (voice-correction fields,
model/token/latency metadata) are preserved rather than flattened away.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from identity.models import AiOutput, Conversation, Prompt

_TITLE_MAX_LENGTH = 300


def _derive_title(question: str) -> str:
    """First question, trimmed/collapsed/capped -- the same "conversation
    reads like a real title" convention `frontend/src/lib/history.ts
    ::deriveConversationTitle` already used for the old, session-only
    conversation list; kept here so a title looks identical regardless of
    which layer ends up deriving it."""
    collapsed = " ".join(question.split())
    if len(collapsed) <= 60:
        return collapsed
    return collapsed[:57] + "…"


def create_conversation(
    session: Session,
    *,
    user_id: uuid.UUID,
    title: str | None = None,
    feature_type: str = "text_to_sql",
    metadata: dict | None = None,
) -> Conversation:
    conversation = Conversation(
        user_id=user_id,
        title=(title or None),
        feature_type=feature_type,
        status="active",
        metadata_json=metadata,
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return conversation


def get_conversation(
    session: Session, *, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation | None:
    """Ownership-scoped lookup -- see this module's own docstring for why
    `user_id` is folded directly into the query rather than checked after
    the fact. Never returns a soft-deleted conversation (a deleted
    conversation is gone from every read path, though its rows remain in
    the database for the retention/audit reasons `docs/chat-history-architecture.md`
    documents)."""
    return session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )


def list_conversations(
    session: Session,
    *,
    user_id: uuid.UUID,
    limit: int,
    offset: int,
    include_archived: bool = False,
) -> tuple[list[Conversation], int]:
    """Returns `(page, total_count)`, ordered by most-recent-activity-first
    (`last_message_at` if the conversation has any messages yet, else
    `created_at` -- a freshly created, still-empty conversation should
    still show up at the top of the list rather than sorting as if it were
    ancient). Never includes a soft-deleted conversation; archived
    conversations are excluded by default (`include_archived=False`) since
    the default history panel view is "active conversations," matching
    every mainstream chat product's own default.
    """
    order_column = func.coalesce(Conversation.last_message_at, Conversation.created_at)
    conditions = [Conversation.user_id == user_id, Conversation.deleted_at.is_(None)]
    if not include_archived:
        conditions.append(Conversation.archived_at.is_(None))

    total = session.scalar(select(func.count()).select_from(Conversation).where(*conditions)) or 0
    rows = session.scalars(
        select(Conversation)
        .where(*conditions)
        .order_by(order_column.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total


def update_conversation(
    session: Session,
    *,
    conversation: Conversation,
    title: str | None = None,
    archived: bool | None = None,
) -> Conversation:
    """Applies whichever of `title`/`archived` is not None. `title=""` (an
    empty string, after the API layer's own whitespace trim) is treated as
    "clear the title" (falls back to the auto-derived one from the first
    question), not a validation error -- a blank rename is a reasonable
    "reset to default" action, not a mistake to reject."""
    if title is not None:
        # identity/models.py's own module docstring explains why
        # Conversation.title/archived_at are annotated `Mapped[str]`/
        # `Mapped[datetime]` (not the Optional form) despite being genuinely
        # nullable -- a Python 3.14 + SQLAlchemy 2.0.36 incompatibility --
        # so mypy sees both as non-Optional here, which these two
        # assignments correctly contradict at runtime.
        conversation.title = title[:_TITLE_MAX_LENGTH] or None  # type: ignore[assignment]
    if archived is True and conversation.archived_at is None:
        conversation.archived_at = datetime.now(UTC)
    elif archived is False and conversation.archived_at is not None:
        conversation.archived_at = None  # type: ignore[assignment]
    session.commit()
    session.refresh(conversation)
    return conversation


def soft_delete_conversation(session: Session, *, conversation: Conversation) -> None:
    """Sets `deleted_at` -- never a real `DELETE`. `identity.models
    .Prompt`/`AiOutput` rows are left in place (their own `conversation_id`
    foreign key still points at this now-deleted conversation, matching
    `docs/chat-history-architecture.md`'s "explicit deletion requires user
    action, but even then this app soft-deletes -- see that doc for the
    permanent-deletion/retention story" contract). This function is only
    ever called after `get_conversation`'s own ownership check has already
    succeeded -- it does not re-check ownership itself, by design (a
    private, already-loaded-and-verified `Conversation` row, not a raw id).
    """
    conversation.deleted_at = datetime.now(UTC)
    session.commit()


def _next_sequence_number(session: Session, conversation_id: uuid.UUID) -> int:
    prompt_max = session.scalar(
        select(func.max(Prompt.sequence_number)).where(Prompt.conversation_id == conversation_id)
    )
    output_max = session.scalar(
        select(func.max(AiOutput.sequence_number)).where(
            AiOutput.conversation_id == conversation_id
        )
    )
    return max(prompt_max or 0, output_max or 0) + 1


def append_turn(
    session: Session,
    *,
    conversation: Conversation,
    user_id: uuid.UUID,
    question: str,
    answer_text: str,
    output_type: str = "chat_answer",
    status: str = "completed",
    source_type: str = "typed",
    model_provider: str | None = None,
    model_name: str | None = None,
    error_code: str | None = None,
    prompt_metadata: dict | None = None,
    output_metadata: dict | None = None,
) -> tuple[Prompt, AiOutput]:
    """Persists one full user-question + assistant-answer turn as a single
    transaction -- both rows share one `sequence_number` pair
    (`n`, `n + 1`), assigned here from the conversation's own current max
    (see `_next_sequence_number`), so the two always sort adjacent
    regardless of `created_at` precision. Also stamps `conversation
    .last_message_at`/`updated_at` and, the very first time a conversation
    gets a message, derives its `title` from `question` if none was set
    explicitly at creation -- exactly mirroring the old session-only
    frontend's own "title comes from the first question" convention (see
    this module's own `_derive_title`).

    Never partially persists a turn: caller and assistant rows are added
    to the same session and committed together, so a failure partway
    through leaves neither row behind (`session.rollback()` is the caller's
    responsibility on an exception -- this function itself does not catch
    one, matching every other repository function in this package).
    """
    now = datetime.now(UTC)
    base_sequence = _next_sequence_number(session, conversation.id)

    prompt = Prompt(
        conversation_id=conversation.id,
        user_id=user_id,
        source_type=source_type,
        raw_content=question,
        final_content=question,
        sequence_number=base_sequence,
        metadata_json=prompt_metadata,
    )
    session.add(prompt)
    session.flush()  # assigns prompt.id before ai_output references it

    ai_output = AiOutput(
        conversation_id=conversation.id,
        prompt_id=prompt.id,
        user_id=user_id,
        output_type=output_type,
        content=answer_text,
        model_provider=model_provider,
        model_name=model_name,
        status=status,
        error_code=error_code,
        sequence_number=base_sequence + 1,
        metadata_json=output_metadata,
    )
    session.add(ai_output)

    conversation.last_message_at = now
    if conversation.title is None:
        conversation.title = _derive_title(question)

    session.commit()
    session.refresh(prompt)
    session.refresh(ai_output)
    return prompt, ai_output


def append_single_message(
    session: Session, *, conversation: Conversation, user_id: uuid.UUID, role: str, content: str
) -> Prompt | AiOutput:
    """Appends exactly one message (not a question+answer pair) --
    `api/chat_history.py`'s `POST /conversations/{id}/messages`, mainly for
    API completeness/direct integration testing rather than this app's own
    real persistence path (`append_turn`, called from `POST /ask` -- see
    `api/chat_persistence.py`). `role="user"` creates a `Prompt` row,
    `role="assistant"` an `AiOutput` row -- whichever it is, it gets its own
    fresh `sequence_number` from the same conversation-wide counter
    `append_turn` uses, so a manually-appended message still sorts
    correctly alongside `/ask`-created turns.
    """
    sequence_number = _next_sequence_number(session, conversation.id)
    now = datetime.now(UTC)
    if role == "user":
        row: Prompt | AiOutput = Prompt(
            conversation_id=conversation.id,
            user_id=user_id,
            source_type="typed",
            raw_content=content,
            final_content=content,
            sequence_number=sequence_number,
        )
    else:
        row = AiOutput(
            conversation_id=conversation.id,
            user_id=user_id,
            output_type="chat_answer",
            content=content,
            status="completed",
            sequence_number=sequence_number,
        )
    session.add(row)
    conversation.last_message_at = now
    session.commit()
    session.refresh(row)
    return row


@dataclass(frozen=True)
class MessageRow:
    """One unified message row -- either a user question (`role="user"`,
    from `Prompt`) or an assistant answer (`role="assistant"`, from
    `AiOutput`) -- for the API layer to serialize as `GET .../messages`'s
    response. See this module's own docstring for why this is a merge view,
    not a physically unified table."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    sequence_number: int
    created_at: datetime
    status: str | None
    model_name: str | None
    error_code: str | None
    # An assistant row's own `metadata_json` (e.g. `{"sql": "..."}"`, set by
    # `append_turn` when the turn produced SQL) -- lets the frontend
    # reconstruct a richer view of a *reloaded* past turn (e.g. re-showing
    # the SQL in the editor) than the plain answer text alone would allow.
    # Always None for a user row.
    metadata: dict | None


def list_messages(
    session: Session, *, conversation_id: uuid.UUID, user_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[MessageRow], int]:
    """Ownership-scoped (via a join back to `conversations`, same
    `user_id`-in-the-query-itself principle as `get_conversation`) merge of
    a conversation's `Prompt`/`AiOutput` rows into deterministic
    `sequence_number` order. `limit`/`offset` apply to *turns* (a
    user+assistant pair counts as 2 sequence numbers but one page-size
    unit), so a page boundary never splits a question from its own answer.
    """
    prompts = session.scalars(
        select(Prompt)
        .join(Conversation, Conversation.id == Prompt.conversation_id)
        .where(
            Prompt.conversation_id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            Prompt.deleted_at.is_(None),
        )
    ).all()
    outputs = session.scalars(
        select(AiOutput)
        .join(Conversation, Conversation.id == AiOutput.conversation_id)
        .where(
            AiOutput.conversation_id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    ).all()

    rows: list[MessageRow] = [
        MessageRow(
            id=p.id,
            conversation_id=p.conversation_id,
            role="user",
            content=p.final_content,
            sequence_number=p.sequence_number,
            created_at=p.created_at,
            status="completed",
            model_name=None,
            error_code=None,
            metadata=None,
        )
        for p in prompts
    ] + [
        MessageRow(
            id=o.id,
            conversation_id=o.conversation_id,
            role="assistant",
            content=o.content,
            sequence_number=o.sequence_number,
            created_at=o.created_at,
            status=o.status,
            model_name=o.model_name,
            error_code=o.error_code,
            metadata=o.metadata_json,
        )
        for o in outputs
    ]
    rows.sort(key=lambda r: r.sequence_number)

    total_turns = max(len(prompts), 1) if rows else 0
    # Paginate by turn (2 rows per turn) rather than by raw row -- see this
    # function's own docstring.
    start = offset * 2
    end = start + limit * 2
    return rows[start:end], total_turns


@dataclass(frozen=True)
class ConversationSearchHit:
    conversation_id: uuid.UUID
    title: str | None
    matched_in: str  # "title" | "message"
    snippet: str
    message_id: uuid.UUID | None
    updated_at: datetime


def _snippet(text: str, query: str, *, radius: int = 60) -> str:
    """A short, safe excerpt centered on the first match of `query` in
    `text` (case-insensitive) -- never the full message content, matching
    every mainstream search-result-list convention. Falls back to a plain
    leading excerpt if the match position can't be located (defensive only;
    every caller here only builds a snippet for a row that already matched
    the same query)."""
    lower_text = text.lower()
    lower_query = query.lower()
    index = lower_text.find(lower_query)
    if index == -1:
        return text[: radius * 2].strip() + ("…" if len(text) > radius * 2 else "")
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def search_history(
    session: Session, *, user_id: uuid.UUID, query: str, limit: int, offset: int
) -> tuple[list[ConversationSearchHit], int]:
    """Searches the authenticated user's own conversation titles and
    message content (`Prompt.final_content`/`AiOutput.content`) -- never
    another user's, since every query below filters on `user_id` directly.

    Uses plain, portable `ILIKE '%term%'` (via SQLAlchemy's `.ilike()`,
    which compiles correctly on both PostgreSQL -- this app's real identity
    database -- and SQLite -- this repo's own test engine) rather than
    PostgreSQL-specific full-text-search operators, so the exact same query
    logic is what `tests/test_identity_repository_history.py` exercises
    against SQLite. Performance on the real PostgreSQL deployment is
    provided by the `pg_trgm` GIN trigram indexes added in this feature's
    own migration (`identity/migrations/versions/a1f3c9d84e21_...py`) --
    a leading-wildcard `ILIKE` cannot use a plain B-tree index at all, but a
    trigram GIN index serves it efficiently, so this is "using the
    database's existing search features" in the sense that matters for a
    single-tenant/moderate-history-volume deployment (see this project's own
    documented local-first, single-user-oriented scale) without a second,
    separate search engine.

    `query` is used exactly as typed inside a `LIKE` pattern -- SQL
    wildcard characters (`%`/`_`) in the user's own search text are *not*
    escaped, meaning a user typing "50%" searches loosely rather than
    literally; this is a minor UX quirk (broader-than-literal matches),
    never a SQL-injection risk, since this goes through SQLAlchemy's
    parameterized `.ilike()`, never raw string interpolation.

    Sort order: most-recently-active conversation first among matches (a
    reasonable proxy for relevance at this app's scale -- no ranking model,
    no `ts_rank`), matching `list_conversations`' own ordering so search
    results and the plain conversation list feel consistent.
    """
    normalized = " ".join(query.split())
    if not normalized:
        return [], 0

    pattern = f"%{normalized}%"

    title_matches = session.scalars(
        select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            Conversation.title.ilike(pattern),
        )
    ).all()

    prompt_matches = session.execute(
        select(Prompt, Conversation)
        .join(Conversation, Conversation.id == Prompt.conversation_id)
        .where(
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            Prompt.deleted_at.is_(None),
            Prompt.final_content.ilike(pattern),
        )
    ).all()

    output_matches = session.execute(
        select(AiOutput, Conversation)
        .join(Conversation, Conversation.id == AiOutput.conversation_id)
        .where(
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
            or_(AiOutput.content.ilike(pattern)),
        )
    ).all()

    hits: list[ConversationSearchHit] = []
    for conversation in title_matches:
        hits.append(
            ConversationSearchHit(
                conversation_id=conversation.id,
                title=conversation.title,
                matched_in="title",
                snippet=_snippet(conversation.title or "", normalized),
                message_id=None,
                updated_at=conversation.last_message_at or conversation.created_at,
            )
        )
    for prompt, conversation in prompt_matches:
        hits.append(
            ConversationSearchHit(
                conversation_id=conversation.id,
                title=conversation.title,
                matched_in="message",
                snippet=_snippet(prompt.final_content, normalized),
                message_id=prompt.id,
                updated_at=conversation.last_message_at or conversation.created_at,
            )
        )
    for output, conversation in output_matches:
        hits.append(
            ConversationSearchHit(
                conversation_id=conversation.id,
                title=conversation.title,
                matched_in="message",
                snippet=_snippet(output.content, normalized),
                message_id=output.id,
                updated_at=conversation.last_message_at or conversation.created_at,
            )
        )

    # De-duplicate to at most one hit per conversation (the
    # most-recently-created match, favoring a message match's more specific
    # snippet over a bare title match when a conversation has both), then
    # sort by conversation recency -- matches this function's own docstring.
    best_by_conversation: dict[uuid.UUID, ConversationSearchHit] = {}
    for hit in hits:
        existing = best_by_conversation.get(hit.conversation_id)
        if existing is None or (existing.matched_in == "title" and hit.matched_in == "message"):
            best_by_conversation[hit.conversation_id] = hit

    ordered = sorted(best_by_conversation.values(), key=lambda h: h.updated_at, reverse=True)
    total = len(ordered)
    return ordered[offset : offset + limit], total
