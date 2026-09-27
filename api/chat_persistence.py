"""Wires `POST /ask`/`POST /execute` (`api/main.py`) into the server-side
chat-history store (`identity/repositories/history.py`) -- the one place
this app turns an already-computed agent answer into a permanent,
cross-device record.

**Persistence failure must never fail an otherwise-successful `/ask`/
`/execute` response.** The SQL has already been generated, validated, and
(if applicable) executed by the time either function here runs -- a
database hiccup writing the *history* record is a completely separate
concern from whether the user's question was answered, and treating it as
fatal would make a transient identity-DB blip take down the core
Text-to-SQL feature for no reason. Every call site wraps this in a broad
`except Exception`, logs a structured warning, and continues -- the same
fail-open contract this codebase already applies to
`plan_query_node`/`retrieve_golden_examples_node`/
`retrieve_business_context_node`, applied here to a different kind of
non-critical-path failure.

Only ever engages for a **locally-authenticated** caller
(`AuthIdentity.mode == "local"`) -- an OIDC-authenticated or
unauthenticated caller has no corresponding row in `identity.users` at
all, so there is nothing to attach a conversation to (see
`docs/chat-history-architecture.md`'s "Known limitations" for why this
isn't extended to OIDC callers in this pass).

**Universal conversation history (2026-09-27).** Before this pass, only
`{"sql": "..."}` (or nothing at all) was ever persisted into
`ai_outputs.metadata` -- every other route's contribution (web/document/
policy/generation/media-search/attachment answers, citations, the
confirmed execution result, which sources actually fired) was silently
dropped, which is what made a reopened conversation show no assistant
answer, an empty SQL editor with "Confirm and Run", or (via a separate,
unrelated bug in `frontend/src/lib/api.ts`'s error-detail parsing)
`[object Object]`. `_build_history_metadata` now snapshots a bounded,
redacted superset of `AskResponse` into that same JSONB column -- no
migration needed, since it was already an arbitrary-shape JSON column (see
`identity/models.py`'s own docstring); a `schema_version` field lets the
frontend distinguish a rich, new-shape record from a legacy `{"sql": ...}`
-or-nothing one and degrade gracefully rather than guessing.

`persist_execute_result` is new: it updates an *already-persisted* turn's
metadata with the confirmed execution result (bounded rows, chart
recommendation, the actually-executed SQL) once `POST /execute` succeeds,
so reopening a conversation later shows a previously confirmed result
immediately -- **without ever re-running the SQL itself**. Nothing in this
module executes a query, calls a model, fetches a URL, or performs OCR --
it only ever stores what the caller-facing response already computed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from identity.repositories.history import (
    append_turn,
    create_conversation,
    get_ai_output,
    get_conversation,
    update_ai_output_metadata,
)

from api.schemas import AskResponse, ExecuteResponse, SourceAnswerOut
from config.settings import Settings, get_settings
from security.oidc import AuthIdentity, real_caller_subject

logger = logging.getLogger(__name__)

# Bumped whenever the shape of the persisted metadata dict changes in a way
# the frontend needs to know about (frontend/src/lib/history.ts's
# `serverMessagesToQueryHistory` branches on this). A record with no
# `schema_version` at all (or with a lower one) is a pre-this-feature
# `{"sql": ...}`-or-nothing row -- see this module's own docstring.
_METADATA_SCHEMA_VERSION = 2

# `schema_tables` is a "what context was consulted" disclosure, not
# something a reopened conversation needs to re-render the full retrieval
# context (the DDL itself is dropped -- see `_schema_table_refs`) -- capped
# so a question that matched many tables doesn't bloat every turn's stored
# metadata.
_MAX_SCHEMA_TABLE_REFS = 8


def _truncate_text(value: str | None, settings: Settings) -> str | None:
    """Bounds one persisted free-text field to `Settings
    .chat_history_max_text_chars` -- independent of this same text's live
    response length, which is never truncated. A trailing marker discloses
    the truncation rather than silently cutting it off."""
    if value is None:
        return None
    limit = settings.chat_history_max_text_chars
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _source_answer_dict(
    source: SourceAnswerOut | None, settings: Settings
) -> dict[str, Any] | None:
    if source is None:
        return None
    return {
        "answer": _truncate_text(source.answer, settings),
        "citations": [c.model_dump(mode="json") for c in source.citations],
        "status": source.status,
    }


def _schema_table_refs(ask_response: AskResponse) -> list[dict[str, Any]]:
    return [
        {"table_name": table.table_name, "similarity_score": table.similarity_score}
        for table in ask_response.schema_tables[:_MAX_SCHEMA_TABLE_REFS]
    ]


def _attachment_refs_snapshot(
    attachment_ids: list[str], *, owner_subject: str | None
) -> list[dict[str, str]]:
    """Best-effort filename/media-type snapshot for each attached file,
    taken while it's still resident in the process-lifetime
    `attachments.store.AttachmentStore` (bounded, FIFO-evicted -- see that
    module's own docstring). An id that's already been evicted by the time
    this runs contributes only its bare id, never a fabricated filename --
    a reloaded conversation shows it as an unavailable attachment rather
    than inventing a name for it."""
    if not attachment_ids:
        return []
    from attachments.store import get_default_attachment_store

    store = get_default_attachment_store()
    refs: list[dict[str, str]] = []
    for attachment_id in attachment_ids:
        item = store.get(attachment_id, owner_subject=owner_subject)
        if item is None:
            refs.append(
                {
                    "attachment_id": attachment_id,
                    "filename": attachment_id,
                    "media_type": "application/octet-stream",
                }
            )
        else:
            refs.append(
                {
                    "attachment_id": attachment_id,
                    "filename": item.safe_filename,
                    "media_type": item.media_type,
                }
            )
    return refs


def _build_history_metadata(
    ask_response: AskResponse,
    settings: Settings,
    *,
    attachment_refs: list[dict[str, str]],
) -> dict[str, Any]:
    """The full, bounded, redacted snapshot persisted into `ai_outputs
    .metadata` for one turn. Reuses `ask_response`'s own fields directly
    (rather than re-deriving anything from the raw agent state) so this
    never persists anything the caller wasn't already shown -- `api.main
    ._ask_response_from_state` has already applied every redaction
    (`_redact_text`/`redact_configured_secrets`) this needs.

    Deliberately excludes: `session_id`/`conversation_id`/`message_id`
    (redundant with the row this is attached to), `result_columns`/
    `result_rows` (the agent's own internal, never-shown-to-the-user
    self-correction execution -- persisting it would violate "SQL is
    untrusted output, always"/"never shown to the user"), and
    `attempt_history`/`error_history`/`followup_*` (retry-loop/session
    bookkeeping, not part of a saved answer). `result_snapshot` starts
    `None` here -- only `persist_execute_result` (below) ever populates it,
    once the user actually clicks "Confirm and Run".
    """
    return {
        "schema_version": _METADATA_SCHEMA_VERSION,
        "status": ask_response.status,
        "sources_used": list(ask_response.sources_used),
        "database": ask_response.database,
        "model": ask_response.model,
        "sql": ask_response.sql,
        "row_count": ask_response.row_count,
        "retry_count": ask_response.retry_count,
        "query_plan": (
            [_truncate_text(step, settings) for step in ask_response.query_plan]
            if ask_response.query_plan is not None
            else None
        ),
        "schema_tables": _schema_table_refs(ask_response),
        "insight": _truncate_text(ask_response.insight, settings),
        "synthesized_answer": _truncate_text(ask_response.synthesized_answer, settings),
        "cost_notice": ask_response.cost_notice,
        "low_confidence_notice": ask_response.low_confidence_notice,
        "rejection_reason": ask_response.rejection_reason,
        "rejection_message": ask_response.rejection_message,
        "rate_limit_message": ask_response.rate_limit_message,
        "clarification_message": ask_response.clarification_message,
        "failure_explanation": _truncate_text(ask_response.failure_explanation, settings),
        "permission_denied_notice": ask_response.permission_denied_notice,
        "document_result": _source_answer_dict(ask_response.document_result, settings),
        "policy_result": _source_answer_dict(ask_response.policy_result, settings),
        "web_result": _source_answer_dict(ask_response.web_result, settings),
        "generation_result": (
            ask_response.generation_result.model_dump(mode="json")
            if ask_response.generation_result is not None
            else None
        ),
        "media_search_result": (
            ask_response.media_search_result.model_dump(mode="json")
            if ask_response.media_search_result is not None
            else None
        ),
        "attachment_result": (
            ask_response.attachment_result.model_dump(mode="json")
            if ask_response.attachment_result is not None
            else None
        ),
        "attachment_refs": attachment_refs,
        "result_snapshot": None,
    }


def _answer_text_for_history(ask_response: AskResponse) -> str:
    """Picks the single best "assistant answer" plain-text to store as
    `ai_outputs.content` (used by `identity.repositories.history
    .search_history`'s full-text search, and as the legacy-record fallback
    `frontend/src/lib/history.ts` renders when richer metadata isn't
    available) -- same priority order `frontend/src/lib/history.ts
    ::buildSpokenAnswerText` already uses, extended to also cover
    generation/media-search/attachment answers (previously silently
    dropped from the searchable text entirely for those routes)."""
    for value in (ask_response.insight, ask_response.synthesized_answer):
        if value:
            return value
    for source in (
        ask_response.document_result,
        ask_response.policy_result,
        ask_response.web_result,
    ):
        if source is not None and source.answer:
            return source.answer
    if ask_response.generation_result is not None and ask_response.generation_result.answer:
        return ask_response.generation_result.answer
    if ask_response.media_search_result is not None and ask_response.media_search_result.answer:
        return ask_response.media_search_result.answer
    if ask_response.attachment_result is not None and ask_response.attachment_result.answer:
        return ask_response.attachment_result.answer
    if ask_response.status == "succeeded":
        if ask_response.row_count is not None:
            return f"Query succeeded. {ask_response.row_count} row(s) returned."
        return "Query succeeded."
    failure = (
        ask_response.failure_explanation
        or ask_response.rejection_message
        or ask_response.clarification_message
        or ask_response.rate_limit_message
    )
    if failure:
        return failure
    return "The request could not be completed."


def _status_for_history(ask_response: AskResponse) -> str:
    return "completed" if ask_response.status == "succeeded" else "failed"


def persist_ask_turn(
    *,
    identity: AuthIdentity,
    conversation_id: str | None,
    question: str,
    ask_response: AskResponse,
    attachment_ids: list[str] | None = None,
    settings: Settings | None = None,
) -> tuple[str | None, str | None]:
    """Persists one `/ask` turn (question + answer) for a locally-
    authenticated caller, creating a new conversation if `conversation_id`
    wasn't supplied (the first turn of a fresh conversation).

    Returns:
        `(conversation_id, message_id)` -- both `None` if persistence
        didn't happen at all (caller isn't locally authenticated, local
        auth isn't enabled, or a genuine failure occurred -- logged, never
        raised, see this module's own docstring). `message_id` is the
        persisted `AiOutput` row's id, the one `POST /execute` should later
        pass back as `ExecuteRequest.message_id` so a confirmed result can
        be attached to *this exact* turn (`persist_execute_result` below)
        -- never populated speculatively, since a turn's SQL may never be
        confirmed at all.
    """
    settings = settings or get_settings()
    if identity.mode != "local" or not settings.local_auth_enabled:
        return None, None

    try:
        user_id = uuid.UUID(identity.subject)
    except ValueError:
        logger.warning("[chat_persistence] local identity had a non-UUID subject; skipping persist")
        return None, None

    try:
        from identity.db import get_identity_session

        session = get_identity_session(settings)
    except Exception as exc:  # noqa: BLE001 - identity DB may be unreachable
        logger.warning("[chat_persistence] could not open identity session: %s", exc)
        return None, None

    try:
        conversation = None
        if conversation_id:
            try:
                parsed_id = uuid.UUID(conversation_id)
            except ValueError:
                parsed_id = None
            if parsed_id is not None:
                conversation = get_conversation(session, conversation_id=parsed_id, user_id=user_id)
            if conversation is None:
                # The supplied id didn't resolve to a conversation this
                # user owns (deleted, never existed, or belongs to someone
                # else) -- silently starts a new one rather than 404ing an
                # otherwise-successful /ask call over a history-only concern.
                logger.info(
                    "[chat_persistence] conversation_id %r not found/owned; starting a new one",
                    conversation_id,
                )
        if conversation is None:
            conversation = create_conversation(session, user_id=user_id, feature_type="text_to_sql")

        attachment_refs = _attachment_refs_snapshot(
            attachment_ids or [], owner_subject=real_caller_subject(identity)
        )
        metadata = _build_history_metadata(ask_response, settings, attachment_refs=attachment_refs)

        _, ai_output = append_turn(
            session,
            conversation=conversation,
            user_id=user_id,
            question=question,
            answer_text=_answer_text_for_history(ask_response),
            output_type="sql_query" if ask_response.sql else "chat_answer",
            status=_status_for_history(ask_response),
            model_provider="ollama" if ask_response.model else None,
            model_name=ask_response.model,
            error_code=str(ask_response.rejection_reason or "") or None,
            output_metadata=metadata,
        )
        return str(conversation.id), str(ai_output.id)
    except Exception as exc:  # noqa: BLE001 - see this module's own docstring
        logger.warning("[chat_persistence] failed to persist /ask turn: %s", exc)
        return None, None
    finally:
        session.close()


def persist_execute_result(
    *,
    identity: AuthIdentity,
    conversation_id: str | None,
    message_id: str | None,
    sql: str,
    execute_response: ExecuteResponse,
    settings: Settings | None = None,
) -> bool:
    """Updates an already-persisted turn's stored metadata with the result
    of a "Confirm and Run" execution, so reopening that conversation later
    shows the confirmed rows/chart immediately -- **without this function,
    or anything it calls, ever re-running the SQL itself**; it only stores
    the result an already-completed `/execute` call produced.

    A no-op (returns `False`) unless the caller is locally authenticated,
    local auth is enabled, and `message_id` names an `AiOutput` row this
    caller actually owns -- a wrong/forged/stale id is treated exactly like
    "nothing to update," never an error surfaced to the "Confirm and Run"
    click that triggered this (same fail-open contract as `persist_ask_turn`).
    If `conversation_id` is also supplied, it must match the message's own
    conversation -- a defense-in-depth cross-check, not the primary
    ownership boundary (that's `get_ai_output`'s own `user_id` scoping).
    """
    settings = settings or get_settings()
    if identity.mode != "local" or not settings.local_auth_enabled:
        return False
    if not message_id:
        return False

    try:
        user_id = uuid.UUID(identity.subject)
        ai_output_id = uuid.UUID(message_id)
    except ValueError:
        return False

    try:
        from identity.db import get_identity_session

        session = get_identity_session(settings)
    except Exception as exc:  # noqa: BLE001 - identity DB may be unreachable
        logger.warning("[chat_persistence] could not open identity session: %s", exc)
        return False

    try:
        ai_output = get_ai_output(session, ai_output_id=ai_output_id, user_id=user_id)
        if ai_output is None:
            logger.info(
                "[chat_persistence] message_id %r not found/owned; skipping result persist",
                message_id,
            )
            return False

        if conversation_id:
            try:
                parsed_conversation_id: uuid.UUID | None = uuid.UUID(conversation_id)
            except ValueError:
                parsed_conversation_id = None
            if (
                parsed_conversation_id is not None
                and ai_output.conversation_id != parsed_conversation_id
            ):
                logger.warning(
                    "[chat_persistence] message_id %r does not belong to conversation_id %r; skipping",
                    message_id,
                    conversation_id,
                )
                return False

        row_cap = settings.chat_history_max_result_rows
        columns = execute_response.result_columns or []
        rows = execute_response.result_rows or []
        bounded_rows = rows[:row_cap]
        normalized_sql = execute_response.normalized_sql or sql

        metadata = dict(ai_output.metadata_json or {})
        metadata["result_snapshot"] = {
            "columns": columns,
            "rows": bounded_rows,
            "row_count": execute_response.row_count,
            "returned_rows": len(bounded_rows),
            "truncated": bool(execute_response.truncated) or len(bounded_rows) < len(rows),
            "column_types": execute_response.column_types,
            "chart_recommendation": (
                execute_response.chart_recommendation.model_dump(mode="json")
                if execute_response.chart_recommendation is not None
                else None
            ),
            "normalized_sql": normalized_sql,
            "duration_ms": execute_response.duration_ms,
            "captured_at": datetime.now(UTC).isoformat(),
        }
        # The user may have hand-edited the SQL box before confirming -- the
        # *executed* SQL is what's worth remembering for a reopened
        # conversation, the same "save what was actually run" principle
        # `embeddings.golden_examples.save_golden_example` already applies.
        metadata["sql"] = normalized_sql

        update_ai_output_metadata(session, ai_output=ai_output, metadata=metadata)
        return True
    except Exception as exc:  # noqa: BLE001 - see this module's own docstring
        logger.warning("[chat_persistence] failed to persist /execute result: %s", exc)
        return False
    finally:
        session.close()
