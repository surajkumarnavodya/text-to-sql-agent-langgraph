"""Stores like/dislike feedback (plus an optional free-text comment) on any
assistant answer -- SQL, document/policy RAG, web search, or media -- the
general-purpose counterpart to `embeddings.golden_examples`, which only
ever captures a thumbs-up on a *confirmed, executed SQL* result, and only
for few-shot prompt injection. This module is the "Give positive/negative
feedback" pattern (rating + optional details, matching
`frontend/src/components/chat/ResponseFeedbackWidget.tsx`).

Deliberately modeled on `embeddings/golden_examples.py`'s ChromaDB pattern,
for the identical reason that module gives for choosing Chroma over
`rag/store.py`'s SQL-Server-`VECTOR` one: Chroma is already a required,
unconditional dependency of this app's core pipeline (schema retrieval),
so this needs no new optional subsystem, and it works identically
regardless of `LOCAL_AUTH_ENABLED`/`DB_CONNECTIONS` -- an anonymous or
OIDC-only caller's feedback is captured exactly the same way as a
locally-authenticated one's would be.

One shared collection, unlike `golden_examples.py`'s one-per-database
split -- this is a feedback log, not a per-database few-shot corpus, so
there's no reason to fragment it by whichever database a question
happened to route to; `database` is stored as a metadata field on each
entry instead.

This module never feeds `agent.nodes.retrieve_golden_examples_node`'s
few-shot retrieval -- a thumbs-up on a confirmed SQL answer still
*separately* calls `embeddings.golden_examples.save_golden_example` (see
`frontend/src/store/chatStore.ts::giveMessageFeedback`), so this is purely
additive to that existing loop, never a replacement.

Fails open exactly like `golden_examples.py`: a storage error is logged
and swallowed, never raised -- a broken feedback click must never look
like a broken app to the user.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from chromadb.api.models.Collection import Collection

from config.settings import Settings, get_settings
from embeddings.schema_indexer import get_chroma_client, get_embedding_function
from security.redaction import redact_secrets
from security.tenancy import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "response_feedback"

Rating = Literal["positive", "negative"]

# A comment or answer is user- or LLM-produced free text of unbounded
# length -- capped before storage for the same log-flooding-prevention
# reason `security.audit_log._MAX_CONTEXT_VALUE_LENGTH` caps structured log
# context, just a larger bound since this is a real stored record meant to
# be read back later, not a single log line.
_MAX_TEXT_LENGTH = 4000


def get_feedback_collection(settings: Settings) -> Collection:
    """Gets or creates the single, shared response-feedback collection."""
    client = get_chroma_client(settings)
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=get_embedding_function(settings),
    )


def _truncate(value: str | None) -> str:
    return (value or "")[:_MAX_TEXT_LENGTH]


def save_response_feedback(
    question: str,
    answer: str,
    rating: Rating,
    *,
    sql: str | None = None,
    database: str | None = None,
    sources_used: list[str] | None = None,
    comment: str | None = None,
    conversation_id: str | None = None,
    settings: Settings | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    """Records one like/dislike event against an assistant answer.

    Args:
        question: The natural-language question that produced this answer.
        answer: The rendered answer text (e.g.
            `frontend/src/lib/history.ts::buildAnswerMarkdown`'s output) --
            whatever the user actually read when they rated it.
        rating: `"positive"` or `"negative"`.
        sql: The SQL shown for this turn, if any (not necessarily executed
            -- unlike `embeddings.golden_examples.save_golden_example`,
            this module logs feedback on a *displayed* answer, whether or
            not "Confirm and Run" was ever clicked).
        database: Which configured database this turn was routed to, if
            any (see `Settings.databases`).
        sources_used: Which sources contributed to this answer (e.g.
            `["sql"]`, `["policies", "web"]`) -- mirrors
            `AgentState["sources_used"]`.
        comment: Optional free-text detail the user typed into the
            feedback modal.
        conversation_id: The conversation this turn belongs to, if known.
        settings: Optional `Settings` override (mainly for tests).
        tenant_id: Which tenant submitted this feedback (Prompt 20),
            resolved server-side from the caller's verified identity. This
            collection is deliberately shared across databases (unlike
            `embeddings.golden_examples`'), so recording the tenant is what
            makes a later per-tenant read of the log possible at all --
            every row here holds a real user's question and the answer text
            they read. Note the asymmetry with golden examples, and why it
            is safe: nothing ever *retrieves* from this collection into a
            prompt (it is a read-later log, see this module's docstring), so
            there is no retrieval path a tenant filter would need to guard.
            The tenant is stored so that reading the log back stays
            partitionable; it is not a runtime access control.

    Never raises -- see this module's docstring.
    """
    settings = settings or get_settings()
    try:
        collection = get_feedback_collection(settings)
        collection.add(
            ids=[str(uuid.uuid4())],
            documents=[_truncate(question)],
            metadatas=[
                {
                    "rating": rating,
                    "answer": _truncate(answer),
                    "sql": _truncate(sql),
                    "database": database or "",
                    "sources_used": ",".join(sources_used) if sources_used else "",
                    "comment": _truncate(comment),
                    "conversation_id": conversation_id or "",
                    "tenant_id": tenant_id,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - saving feedback must never crash the UI
        safe_detail = redact_secrets(str(exc), settings)
        logger.warning("Failed to save response feedback (rating=%r): %s", rating, safe_detail)
        return
    logger.info("Saved response feedback: rating=%r sources_used=%r", rating, sources_used)
