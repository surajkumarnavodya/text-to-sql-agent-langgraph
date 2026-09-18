"""Wires `POST /ask` (`api/main.py`) into the server-side chat-history
store (`identity/repositories/history.py`) -- the one place this app
turns an already-computed agent answer into a permanent, cross-device
record.

**Persistence failure must never fail an otherwise-successful `/ask`
response.** The SQL has already been generated, validated, and (if
applicable) executed by the time this runs -- a database hiccup writing
the *history* record is a completely separate concern from whether the
user's question was answered, and treating it as fatal would make a
transient identity-DB blip take down the core Text-to-SQL feature for no
reason. Every call site wraps this in a broad `except Exception`, logs a
structured warning, and continues -- the same fail-open contract this
codebase already applies to `plan_query_node`/`retrieve_golden_examples_node`/
`retrieve_business_context_node`, applied here to a different kind of
non-critical-path failure.

Only ever engages for a **locally-authenticated** caller
(`AuthIdentity.mode == "local"`) -- an OIDC-authenticated or
unauthenticated caller has no corresponding row in `identity.users` at
all, so there is nothing to attach a conversation to (see
`docs/chat-history-architecture.md`'s "Known limitations" for why this
isn't extended to OIDC callers in this pass).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from identity.repositories.history import append_turn, create_conversation, get_conversation

from config.settings import Settings, get_settings
from security.oidc import AuthIdentity

logger = logging.getLogger(__name__)


def _answer_text_for_history(state: Mapping[str, Any]) -> str:
    """Picks the single best "assistant answer" text to store, in the same
    priority order `frontend/src/lib/history.ts::buildSpokenAnswerText`
    already uses for what to read aloud -- reusing an already-established
    "what's the one representative answer for this turn" convention rather
    than inventing a second one here."""
    for key in ("insight", "synthesized_answer"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("document_result", "policy_result", "web_result"):
        source = state.get(key)
        if isinstance(source, Mapping) and source.get("answer"):
            return str(source["answer"])
    if state.get("status") == "succeeded":
        row_count = state.get("row_count")
        if isinstance(row_count, int):
            return f"Query succeeded. {row_count} row(s) returned."
        return "Query succeeded."
    failure = state.get("failure_explanation") or state.get("rejection_message")
    if isinstance(failure, str) and failure:
        return failure
    return "The request could not be completed."


def _status_for_history(state: Mapping[str, Any]) -> str:
    return "completed" if state.get("status") == "succeeded" else "failed"


def persist_ask_turn(
    *,
    identity: AuthIdentity,
    conversation_id: str | None,
    question: str,
    final_state: Mapping[str, Any],
    settings: Settings | None = None,
) -> str | None:
    """Persists one `/ask` turn (question + answer) for a locally-
    authenticated caller, creating a new conversation if `conversation_id`
    wasn't supplied (the first turn of a fresh conversation).

    Returns:
        The conversation id (as a string) the turn was persisted under, or
        `None` if persistence didn't happen at all -- either because the
        caller isn't locally authenticated, local auth isn't enabled, or a
        genuine failure occurred (logged, never raised -- see this
        module's own docstring).
    """
    settings = settings or get_settings()
    if identity.mode != "local" or not settings.local_auth_enabled:
        return None

    try:
        user_id = uuid.UUID(identity.subject)
    except ValueError:
        logger.warning("[chat_persistence] local identity had a non-UUID subject; skipping persist")
        return None

    try:
        from identity.db import get_identity_session

        session = get_identity_session(settings)
    except Exception as exc:  # noqa: BLE001 - identity DB may be unreachable
        logger.warning("[chat_persistence] could not open identity session: %s", exc)
        return None

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

        append_turn(
            session,
            conversation=conversation,
            user_id=user_id,
            question=question,
            answer_text=_answer_text_for_history(final_state),
            output_type="sql_query" if final_state.get("sql") else "chat_answer",
            status=_status_for_history(final_state),
            model_provider="ollama",
            model_name=settings.ollama_model,
            error_code=str(final_state.get("rejection_reason") or "") or None,
            output_metadata={"sql": final_state.get("sql")} if final_state.get("sql") else None,
        )
        return str(conversation.id)
    except Exception as exc:  # noqa: BLE001 - see this module's own docstring
        logger.warning("[chat_persistence] failed to persist /ask turn: %s", exc)
        return None
    finally:
        session.close()
