"""Wires `POST /ask` (`api/main.py`) into the governed recommendation
store (`identity/repositories/recommendation_governance.py`) -- Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).

**Persistence failure must never fail an otherwise-successful `/ask`
response.** The exact same fail-open contract `api/chat_persistence.py`
already establishes for conversation history, applied here to a
different, additive kind of non-critical-path write: the SQL has already
been generated, validated, (if applicable) executed, and
`recommendation.engine.generate_recommendations` has already produced
its output by the time this module runs -- a database hiccup writing the
*governance* record must never take down the core Text-to-SQL feature.

**Only ever engages for a locally-authenticated caller**
(`AuthIdentity.mode == "local"`) -- the identical scoping
`api/chat_persistence.py` already applies, for the identical reason (an
OIDC-authenticated or unauthenticated caller has no corresponding
`identity.users` row to attribute a created-by actor to, and no resolved
tenant to scope the record to -- see `security.tenancy
.resolve_tenant_id_for_identity`'s own docstring).

**This module only ever reads `AskResponse.recommendations`** (Prompt
17's already-computed, already-validated output) **and writes new
rows** -- it never recomputes a recommendation, never changes one's
content, and is not itself part of `recommendation.engine`'s pipeline.
Reconstructing `recommendation.models.Recommendation` objects from the
response's own `model_dump()` dicts (rather than threading the typed
objects through the whole `/ask` call stack) keeps this module decoupled
from `agent.graph.run_agent`'s own internals, mirroring how
`api/chat_persistence.py` already builds everything it persists from
`AskResponse` alone, never from raw agent state.
"""

from __future__ import annotations

import logging
import uuid

from identity.repositories.recommendation_governance import create_record
from recommendation.models import Recommendation

from api.schemas import AskResponse
from config.settings import Settings, get_settings
from security.oidc import AuthIdentity
from security.tenancy import resolve_tenant_id_for_identity

logger = logging.getLogger(__name__)


def persist_ask_recommendations(
    *,
    identity: AuthIdentity,
    question: str,
    ask_response: AskResponse,
    settings: Settings | None = None,
) -> int:
    """Persists every recommendation in `ask_response.recommendations` as
    a new `identity.models.RecommendationRecord`, each starting in
    `"generated"` status with its own initial audit event (see
    `identity.repositories.recommendation_governance.create_record`).

    Returns:
        How many records were persisted -- `0` whenever persistence
        didn't happen at all (caller isn't locally authenticated, local
        auth isn't enabled, `Settings.enable_recommendation_persistence`
        is off, there was nothing to persist, or a genuine failure
        occurred -- logged, never raised, see this module's own
        docstring).
    """
    settings = settings or get_settings()
    if not settings.enable_recommendation_persistence:
        return 0
    if identity.mode != "local" or not settings.local_auth_enabled:
        return 0
    if not ask_response.recommendations:
        return 0

    tenant_id = resolve_tenant_id_for_identity(identity)
    if tenant_id is None:
        return 0

    try:
        user_id = uuid.UUID(identity.subject)
    except ValueError:
        logger.warning(
            "[recommendation_persistence] local identity had a non-UUID subject; skipping"
        )
        return 0

    try:
        from identity.db import get_identity_session

        session = get_identity_session(settings)
    except Exception as exc:  # noqa: BLE001 - identity DB may be unreachable
        logger.warning("[recommendation_persistence] could not open identity session: %s", exc)
        return 0

    persisted = 0
    try:
        database_id = ask_response.database or "default"
        for raw in ask_response.recommendations:
            try:
                recommendation = Recommendation(**raw)
            except Exception as exc:  # noqa: BLE001 - a malformed dict must not block the rest
                logger.warning(
                    "[recommendation_persistence] skipping a malformed recommendation dict: %s",
                    exc,
                )
                continue
            create_record(
                session,
                tenant_id=tenant_id,
                database_id=database_id,
                recommendation=recommendation,
                created_by_user_id=user_id,
                source_question=question,
                source_sql=ask_response.sql,
            )
            persisted += 1
        return persisted
    except Exception as exc:  # noqa: BLE001 - see this module's own docstring
        logger.warning("[recommendation_persistence] failed to persist recommendations: %s", exc)
        return persisted
    finally:
        session.close()
