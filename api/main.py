"""FastAPI app: the REST surface over the LangGraph agent, and (once built)
the process that serves the React dashboard itself -- see
`api/__init__.py`'s module docstring for why `/ask` is a thin wrapper
around `agent.orchestrator.graph.run_orchestrated`, not a second
implementation.

Run with (see `docs/API.md`/`docs/DEPLOYMENT.md` for the full picture):

    uvicorn api.main:app --host 0.0.0.0 --port 8000

This is the only server process this project ships -- the human-facing
surface (`frontend/`, a React SPA) is either served from this same process
(the StaticFiles mount near the bottom of this file, once `frontend/dist`
exists) or proxied to it in development (`frontend/vite.config.ts`'s
`BACKEND_ROUTES`). It's also the foundation `docs/DEPLOYMENT.md`'s
reverse-proxy guidance sits in front of, and remains fully usable for
programmatic/scripted access on its own.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

# `uvicorn api.main:app` does not guarantee the repo root is on sys.path
# (unlike running as an installed package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.authz import Permission
from agent.exceptions import AgentError
from agent.graph import build_graph
from agent.llm_client import get_ollama_client
from agent.orchestrator.graph import build_orchestrator_graph, run_orchestrated
from agent.rate_limit import QUESTION_LIMIT_MESSAGE, BoundedLimiterCache, SlidingWindowRateLimiter
from agent.result_charting import build_chart
from agent.sql_validator import enforce_row_limit, qualify_table_schema, validate_sql
from agent.state import ConversationExchange
from api.authz import require_permission
from api.chat_history import router as chat_history_router
from api.chat_persistence import persist_ask_turn
from api.documents import router as documents_router
from api.generation import router as generation_router
from api.identity_auth import router as identity_auth_router
from api.media import router as media_router
from api.media_library import router as media_library_router
from api.media_search import router as media_search_router
from api.rate_limit import enforce_api_action_rate_limit
from api.schemas import (
    AskRequest,
    AskResponse,
    AttemptRecordOut,
    CitationOut,
    ColumnOut,
    ComponentHealth,
    ConversationExchangeOut,
    DatabaseHealth,
    ExecuteRequest,
    ExecuteResponse,
    GoldenExampleFeedbackRequest,
    GoldenExampleFeedbackResponse,
    HealthResponse,
    MediaGenerationResultOut,
    MediaSearchHitOut,
    MediaSearchResultOut,
    PerformanceMetricsResponse,
    RequestMetricOut,
    SchemaRefreshResponse,
    SchemaRefreshResult,
    SchemaTableOut,
    SourceAnswerOut,
    StageMetricOut,
    TableOut,
    TablesResponse,
)
from api.voice import router as voice_router
from config.settings import ConfigurationError, configure_logging, get_settings
from db.connection import (
    check_write_privileges,
    get_connection,
    get_read_only_engine,
    get_sqlglot_dialect,
    test_connection,
)
from db.execution import execute_readonly_sql
from db.schema_introspection import introspect_schema
from embeddings.golden_examples import save_golden_example
from embeddings.schema_indexer import get_chroma_client, get_collection, refresh_all_schema_indexes
from observability.metrics import get_default_metrics
from security.audit_log import (
    get_correlation_id,
    log_security_event,
    reset_correlation_id,
    set_correlation_id,
)
from security.oidc import AuthIdentity
from security.redaction import redact_secrets

configure_logging()
logger = logging.getLogger(__name__)


def _enforce_database_write_privileges(db_engines: Mapping[str, Any], settings) -> None:
    """Runs `db.connection.check_write_privileges` against every
    successfully-warmed database engine and, in production, refuses to
    start if any of them appears to hold write privileges.

    2026 Phase 3 security review: `check_write_privileges` existed since
    Phase 2 but was only ever invoked from the manual
    `scripts/test_db_connection.py` CLI -- an operator who never runs that
    script by hand (entirely plausible for a containerized/CI-deployed
    setup) got no signal at all that their supposedly-read-only `DB_USER`
    actually holds INSERT/UPDATE/DELETE. Calling this from `lifespan`, once
    per warmed engine, at startup, closes that gap for every deployment
    automatically rather than only the ones an operator remembers to check
    by hand.

    A separate, plain function (not inlined in `lifespan`) specifically so
    it's unit-testable without spinning up the whole ASGI app/ LangGraph
    graph -- `lifespan` itself is thin glue calling this.

    Fails open on a database whose check itself couldn't run
    (`result.checked is False` -- an unsupported `DB_TYPE`, or the role
    lacking permission to read its own privilege catalog) -- see
    `check_write_privileges`'s own docstring for why that must never be
    treated as evidence of anything.

    Raises:
        ConfigurationError: if `settings.environment == "production"` and
            at least one database's connected role appears to hold write
            privileges. Never raises in any other environment -- a
            `development` deployment only ever gets a warning log line, the
            same "never breaks local dev" posture every other
            production-only check in this codebase already has.
    """
    writable_databases: list[str] = []
    for db_name, engine in db_engines.items():
        db_config = next((db for db in settings.databases if db.name == db_name), None)
        result = check_write_privileges(engine, db_config)
        if not result.checked or not result.has_write_privileges:
            continue
        writable_databases.append(db_name)
        log_security_event(
            "db_write_privileges_detected",
            "critical",
            f"The connected database role for database {db_name!r} appears to "
            "have write privileges (INSERT/UPDATE/DELETE) -- this app only "
            "ever validates and executes read-only SELECT statements, but the "
            "database role is the real safety boundary if that validation "
            "were ever bypassed.",
            database=db_name,
        )

    if not writable_databases:
        return

    if settings.environment == "production":
        # Fail closed, the same posture `_require_identity_in_production`
        # already established for authentication -- a production deployment
        # must never silently come up with its one real safety-boundary
        # backstop (a genuinely read-only DB role) missing.
        raise ConfigurationError(
            "Refusing to start in ENVIRONMENT=production: the connected "
            f"database role for {', '.join(sorted(writable_databases))} "
            "appears to have write privileges (INSERT/UPDATE/DELETE). This "
            "app's SQL validator is not the final protection -- see "
            "db.connection.check_write_privileges's docstring and "
            "SECURITY.md's least-privilege guidance. Point DB_USER at a "
            "genuinely read-only database role before starting this "
            "deployment again."
        )

    logger.warning(
        "[startup] %d database(s) appear to have write privileges: %s -- "
        "see SECURITY.md's least-privilege guidance. This is a warning "
        "only because ENVIRONMENT is not 'production'.",
        len(writable_databases),
        ", ".join(sorted(writable_databases)),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Builds every expensive, process-lifetime resource once at startup
    instead of lazily on whichever request happens to need it first.

    None of this is strictly required for correctness -- `db.connection
    .get_read_only_engine`, `agent.llm_client._get_ollama_client`, and
    `agent.graph.build_graph`/`agent.orchestrator.graph
    .build_orchestrator_graph` are all already process-wide singletons
    (`functools.cache`/`lru_cache`), so the *first* request to touch each
    one would build and cache it anyway. What this buys is moving that
    one-time cost (DB connection-pool spin-up, compiling ten LangGraph
    nodes into a graph, an Ollama client's initial handshake) out of the
    request that happens to arrive first and into startup, where it
    doesn't cost a real user any latency. The built objects are also
    stashed on `app.state` so they're discoverable/inspectable from a
    debugger or a future admin endpoint, even though every request path
    keeps reaching them the same way it always did -- through the cached
    module-level functions, not by reading `app.state` -- since those
    functions are also called from `eval/runner.py` and scripts that never
    go through this FastAPI app at all.
    """
    settings = get_settings()
    app.state.settings = settings

    db_engines = {}
    for db_config in settings.databases:
        try:
            db_engines[db_config.name] = get_read_only_engine(db_config)
        except ConfigurationError as exc:
            # A misconfigured *additional* database shouldn't prevent the
            # app from starting at all -- /health already surfaces a
            # per-database "not OK" status for exactly this case, the same
            # way it tolerates one unreachable database among several.
            logger.warning(
                "[startup] could not pre-warm engine for database %r: %s",
                db_config.name,
                redact_secrets(str(exc), db_config),
            )
    app.state.db_engines = db_engines
    _enforce_database_write_privileges(db_engines, settings)

    app.state.ollama_client = get_ollama_client(settings)
    app.state.compiled_graph = build_graph()
    app.state.orchestrator_graph = (
        build_orchestrator_graph() if settings.enable_multi_source_router else None
    )

    logger.info(
        "[startup] warmed %d database engine(s), the Ollama client, and the compiled agent graph.",
        len(settings.databases),
    )
    yield


app = FastAPI(
    title="Text-to-SQL API",
    description=__doc__,
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(documents_router)
app.include_router(media_router)
app.include_router(generation_router)
app.include_router(voice_router)
app.include_router(media_search_router)
app.include_router(media_library_router)
app.include_router(identity_auth_router)
app.include_router(chat_history_router)

# No-op when Settings.cors_allowed_origins is empty (the default) -- a
# same-origin deployment (the built React app served by this same FastAPI
# app) needs no CORS at all. Only active when a dev origin (e.g. a Vite
# dev server) is explicitly configured via .env.
_cors_origins = get_settings().cors_allowed_origins
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# 2026 Phase 3 security review: matches what the built React dashboard
# (frontend/dist/index.html) actually loads -- no inline <script> (Vite
# bundles every script into an external, hashed file, so `script-src 'self'`
# needs no `unsafe-inline`/`unsafe-eval`), the Google Fonts stylesheet/font
# files `index.html` links directly, `blob:` object URLs for generated/
# fetched media (`MediaResultCard.tsx`, `useVoiceConversation.ts`'s spoken-
# answer audio), and `data:` URIs for the inline SVG favicon. `style-src`
# allows `unsafe-inline` -- React's inline `style={{...}}` (used by this
# app's dynamic accent-color/theme system, `src/lib/theme.ts`) would
# otherwise be blocked, and unlike `script-src`, an inline-style allowance
# is a narrow, low-severity relaxation (it cannot execute script) that many
# production CSPs accept as a pragmatic tradeoff for exactly this reason.
# `frame-ancestors 'none'` is this policy's clickjacking defense (also
# covered by the `X-Frame-Options: DENY` header below, for the handful of
# older browsers that don't honor CSP frame-ancestors).
#
# 2026 Phase 3 addition, `frame-src`: `frontend/src/lib/auth.ts`'s OIDC
# silent-renew (`UserManager`'s `signinSilent()`, `automaticSilentRenew`)
# loads the identity provider's authorization endpoint in a **hidden
# iframe** -- unlike the interactive `signinRedirect()` login flow (a full
# top-level page navigation, which CSP does not restrict at all), an
# iframe load *is* governed by CSP's `frame-src` directive, and without an
# explicit entry it inherits `default-src 'self'`, which would silently
# block every silent-renew attempt and force a full interactive
# re-login far more often than necessary. Derived from `OIDC_ISSUER`
# (already backend-config, not duplicated) rather than requiring a second,
# separately-set value -- covers the overwhelmingly common case where the
# frontend's `VITE_OIDC_AUTHORITY` (a build-time, frontend-only setting
# this Python process can't see) is the same identity provider as the
# backend's own JWT validation. A deployment using two different
# authorities for some reason needs `CONTENT_SECURITY_POLICY` to override
# this default explicitly.
def _default_csp(settings) -> str:
    frame_src = "'self'"
    if settings.oidc_issuer:
        issuer_origin = urlparse(settings.oidc_issuer)
        if issuer_origin.scheme and issuer_origin.netloc:
            frame_src = f"'self' {issuer_origin.scheme}://{issuer_origin.netloc}"
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        f"frame-src {frame_src}; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    """Adds a standard set of defensive response headers to every response,
    on by default (`Settings.enable_security_headers`) -- see this
    codebase's "no single security control is the final barrier" principle
    (CLAUDE.md/SECURITY.md): none of these replace an existing control
    (authentication, authorization, the SQL validator, CORS), they're an
    additional, independent layer against clickjacking (`X-Frame-Options`/
    `frame-ancestors`), MIME-sniffing-based content-type confusion
    (`X-Content-Type-Options`), and script-injection XSS (`Content-Security-
    Policy`'s `script-src`).

    Uses `setdefault` throughout so a route that already sets one of these
    itself (`api/documents.py`'s PDF download route sets its own
    `X-Content-Type-Options`) is never overridden.

    `Settings.content_security_policy` lets an operator override
    `_default_csp()` entirely (including to the empty string, which omits
    the CSP header while keeping the others) for a deployment this default
    doesn't fit -- e.g. one embedding a different font provider, or one
    that needs to be embeddable in a frame this default's `frame-ancestors
    'none'` would otherwise block.
    """
    response = await call_next(request)
    settings = get_settings()
    if not settings.enable_security_headers:
        return response

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        # Deny every sensor/capability this app's own frontend never needs
        # -- except microphone, which voice mode (`useAudioRecorder.ts`)
        # genuinely requires for the same origin.
        "Permissions-Policy",
        "camera=(), geolocation=(), payment=(), usb=(), microphone=(self)",
    )
    response.headers.setdefault(
        "Strict-Transport-Security",
        f"max-age={settings.hsts_max_age_seconds}; includeSubDomains",
    )
    csp = (
        settings.content_security_policy
        if settings.content_security_policy is not None
        else _default_csp(settings)
    )
    if csp:
        response.headers.setdefault("Content-Security-Policy", csp)
    return response


@app.exception_handler(AgentError)
async def _agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
    """Last-resort net for an `AgentError` that escapes its usual handling.

    Every *known* agent-layer failure mode is normally absorbed inside
    `agent/nodes.py` into `AgentState` fields (see that module) or, for the
    couple of exceptions that can still propagate out of `run_orchestrated`
    today, caught locally in `/ask` -- both paths already use
    `exc.safe_message`, never `str(exc)`. This handler exists for whatever
    isn't covered by either yet (a genuinely new source added later, a
    call site that forgets), so the fallback is still "log the full detail,
    show the safe one" rather than a raw exception leaking through
    FastAPI's default error handling.
    """
    logger.error(
        "[api] unhandled AgentError on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": exc.safe_message, "correlation_id": get_correlation_id()},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catches anything not already handled -- a bug, an unexpected
    third-party exception type, anything. The one guarantee this makes:
    whatever the internal exception text says (which can be an arbitrary
    driver/library message, potentially including connection details this
    app never intended to display), the response body never contains it.
    The full exception (with traceback) still goes to the logs, tagged
    with the same correlation ID returned to the caller, so it's still
    fully debuggable server-side.
    """
    logger.exception(
        "[api] unhandled exception on %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "An unexpected error occurred. Please try again.",
            "correlation_id": get_correlation_id(),
        },
    )


# Per-client-IP question-submission limiter -- the API has no server-side
# session concept, so client IP is the closest available scope. The
# stricter, process-wide LLM-*call* limiter (agent.rate_limit's other
# limiter) already applies automatically inside generate_sql_node -- this
# one adds a separate question-submission-level layer on top of it.
# Bounded (see BoundedLimiterCache's docstring) -- previously a bare dict
# that grew one entry per distinct client IP forever (2026 Phase 1 security
# review, finding API-02).
_ip_limiters = BoundedLimiterCache()


def _limiter_for(client_ip: str) -> SlidingWindowRateLimiter:
    return _ip_limiters.get_or_create(
        client_ip,
        max_events=get_settings().question_rate_limit_per_minute,
        window_seconds=60.0,
        name=f"api_questions[{client_ip}]",
    )


@app.middleware("http")
async def _correlation_id_middleware(request: Request, call_next):
    """Binds a per-request correlation ID for `security.audit_log`'s
    structured events (see that module's docstring) and echoes it back as a
    response header, so a caller can correlate their request with a
    specific audit-log line without this app needing per-user identity."""
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    token = set_correlation_id(correlation_id)
    try:
        response = await call_next(request)
    finally:
        reset_correlation_id(token)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


def _attempt_records_out(state: Mapping[str, Any]) -> list[AttemptRecordOut]:
    return [
        AttemptRecordOut(
            attempt=record["attempt"],
            sql=record.get("sql"),
            outcome=record["outcome"],
            error=record.get("error"),
            will_retry=record.get("will_retry", False),
        )
        for record in state.get("attempt_history", [])
    ]


def _rows_to_json(rows: list[Any] | None) -> list[list[Any]] | None:
    """Converts DB result rows to plain JSON-encodable lists.

    `db.execution.execute_readonly_sql` returns `sqlalchemy.engine.row.Row`
    objects, not plain tuples -- `Row` behaves like a tuple (iterable,
    indexable) but isn't recognized as one by `fastapi.encoders
    .jsonable_encoder`'s isinstance checks, so passing it directly raises
    (its dict()/vars() object-fallback path doesn't apply to Row either).
    Converting to `list(row)` first sidesteps that entirely; jsonable_encoder
    still does the real work of encoding each row's actual values (Decimal,
    datetime, UUID, ...).
    """
    if rows is None:
        return None
    return jsonable_encoder([list(row) for row in rows])


def _source_answer_out(result: Mapping[str, Any] | None) -> SourceAnswerOut | None:
    """Converts one `agent.orchestrator.state.SourceAnswer` dict (document_result/
    policy_result/web_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return SourceAnswerOut(
        answer=result.get("answer", ""),
        citations=[
            CitationOut(
                filename=citation["filename"],
                chunk_index=citation["chunk_index"],
                page_number=citation.get("page_number"),
                document_id=citation["document_id"],
                has_pdf_bytes=citation["has_pdf_bytes"],
            )
            for citation in result.get("citations", [])
        ],
        status=result.get("status", "succeeded"),
    )


def _media_generation_result_out(
    result: Mapping[str, Any] | None,
) -> MediaGenerationResultOut | None:
    """Converts one `agent.orchestrator.state.MediaGenerationResult` dict
    (generation_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return MediaGenerationResultOut(
        answer=result.get("answer", ""),
        status=result.get("status", "succeeded"),
        media_id=result.get("media_id"),
        media_type=result.get("media_type"),
        model=result.get("model"),
    )


def _media_search_result_out(
    result: Mapping[str, Any] | None,
) -> MediaSearchResultOut | None:
    """Converts one `agent.orchestrator.state.MediaSearchResult` dict
    (media_search_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return MediaSearchResultOut(
        answer=result.get("answer", ""),
        status=result.get("status", "succeeded"),
        hits=[
            MediaSearchHitOut(
                media_id=hit["media_id"],
                media_type=hit["media_type"],
                caption=hit["caption"],
                timestamp_start=hit.get("timestamp_start"),
                timestamp_end=hit.get("timestamp_end"),
            )
            for hit in result.get("hits", [])
        ],
    )


def _followup_resolved_against_out(
    exchange: Mapping[str, Any] | None,
) -> ConversationExchangeOut | None:
    if exchange is None:
        return None
    return ConversationExchangeOut(
        question=exchange["question"],
        sql=exchange.get("sql"),
        tables=exchange.get("tables", []),
        status=exchange["status"],
    )


def _ask_response_from_state(
    state: Mapping[str, Any], session_id: str, conversation_id: str | None = None
) -> AskResponse:
    # Typed as a plain Mapping, not AgentState, since `run_orchestrated` can
    # return either an AgentState (router off) or an OrchestratorState
    # (router on) -- the latter's extra keys (sources_used, document_result,
    # ...) aren't part of AgentState's declared shape. Same convention
    # the React dashboard's own `isSqlResult` check uses for the identical reason.
    result_rows = state.get("result_rows")
    return AskResponse(
        session_id=session_id,
        conversation_id=conversation_id,
        status=state.get("status", "failed"),
        database=state.get("selected_database"),
        sql=state.get("sql"),
        result_columns=state.get("result_columns"),
        result_rows=_rows_to_json(result_rows),
        row_count=state.get("row_count"),
        retry_count=state.get("retry_count", 0),
        attempt_history=_attempt_records_out(state),
        insight=state.get("insight"),
        cost_notice=state.get("cost_notice"),
        low_confidence_notice=state.get("low_confidence_notice"),
        rejection_reason=state.get("rejection_reason"),
        rejection_message=state.get("rejection_message"),
        rate_limit_message=state.get("rate_limit_message"),
        clarification_message=state.get("clarification_message"),
        failure_explanation=state.get("failure_explanation"),
        error_history=state.get("error_history", []),
        sources_used=state.get("sources_used", []),
        synthesized_answer=state.get("synthesized_answer"),
        document_result=_source_answer_out(state.get("document_result")),
        policy_result=_source_answer_out(state.get("policy_result")),
        web_result=_source_answer_out(state.get("web_result")),
        generation_result=_media_generation_result_out(state.get("generation_result")),
        media_search_result=_media_search_result_out(state.get("media_search_result")),
        query_plan=state.get("query_plan"),
        schema_tables=[
            SchemaTableOut(
                table_name=table["table_name"],
                ddl=table["ddl"],
                similarity_score=table["similarity_score"],
            )
            for table in state.get("schema_tables", [])
        ],
        followup_classification=state.get("followup_classification"),
        followup_resolved_against=_followup_resolved_against_out(
            state.get("followup_resolved_against")
        ),
        permission_denied_notice=state.get("permission_denied_notice"),
    )


@app.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    """Real, non-cached reachability check of every external dependency
    this app needs -- every configured database (`Settings.databases`) plus
    its schema index, and Ollama. Deliberately cheap: no LLM generation
    call, no query execution, no schema re-introspection -- just "can we
    reach it" for each.

    Returns HTTP 200 when every component -- Ollama and *every* configured
    database -- is reachable, 503 otherwise (so typical
    container/orchestrator health-check tooling that checks the status
    code, not just the body, behaves correctly). One unreachable database
    among several still marks the whole response "degraded" rather than
    being silently dropped, since a caller relying on that database would
    otherwise have no way to know.
    """
    settings = get_settings()

    databases: list[DatabaseHealth] = []
    for config in settings.databases:
        db_result = test_connection(config)
        connection = ComponentHealth(ok=db_result.success, detail=db_result.message)

        try:
            client = get_chroma_client(settings)
            collection = get_collection(client, settings, config.name)
            count = collection.count()
            schema_index = ComponentHealth(
                ok=count > 0,
                detail=(
                    f"{count} table(s) indexed."
                    if count > 0
                    else "Index is empty -- run scripts/build_embeddings.py."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - health check must never crash the endpoint
            # redact_secrets: this endpoint is unauthenticated (no auth
            # dependency at all, unlike /ask and /schema/tables, both
            # gated by api.authz.require_permission now -- health checks
            # typically need to be reachable by an orchestrator with no
            # API key), so raw driver/Chroma text ending up here is a
            # real, publicly-visible leak, not just an internal one.
            schema_index = ComponentHealth(
                ok=False, detail=f"Unreachable: {redact_secrets(str(exc), config)}"
            )

        databases.append(
            DatabaseHealth(name=config.name, connection=connection, schema_index=schema_index)
        )

    try:
        get_ollama_client(settings).list()
        ollama_health = ComponentHealth(ok=True, detail=f"Reachable at {settings.ollama_host}.")
    except Exception as exc:  # noqa: BLE001 - health check must never crash the endpoint
        ollama_health = ComponentHealth(ok=False, detail=f"Unreachable: {redact_secrets(str(exc))}")

    overall_ok = ollama_health.ok and all(
        db.connection.ok and db.schema_index.ok for db in databases
    )
    response.status_code = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if overall_ok else "degraded",
        databases=databases,
        ollama=ollama_health,
        voice_enabled=settings.enable_voice_mode,
        media_search_enabled=settings.enable_media_search,
        local_auth_enabled=settings.local_auth_enabled,
    )


class _AskRequestTimedOut(Exception):
    """Raised by `_run_orchestrated_with_timeout` when `run_orchestrated`
    hasn't returned within `Settings.request_timeout_seconds` -- see that
    setting's docstring for the reliability gap this closes and its
    "stop waiting, not true cancellation" caveat."""


def _run_orchestrated_with_timeout(
    question: str,
    conversation_history: list[ConversationExchange],
    *,
    enable_insight: bool,
    session_id: str,
    caller_roles: tuple[str, ...],
    caller_subject: str | None,
    timeout_seconds: int,
) -> Mapping[str, Any]:
    """Runs `run_orchestrated` on a background thread and gives up waiting
    past `timeout_seconds`, raising `_AskRequestTimedOut` instead of letting
    the calling (FastAPI request-handling) thread block indefinitely.

    Mirrors `db.execution._execute_with_timeout`'s "background thread +
    `join(timeout)`" idiom -- the one difference is there is no connection
    to force-close here (an in-flight Ollama call has no clean cross-thread
    cancellation), so the abandoned thread simply keeps running to
    completion in the background with its result discarded; see
    `Settings.request_timeout_seconds`'s docstring for why that's still
    worth doing (it frees the request-handling thread and gives the caller
    a timely response, even though it doesn't reduce backend load from the
    already-in-flight call).
    """
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            result["state"] = run_orchestrated(
                question,
                conversation_history,
                enable_insight=enable_insight,
                session_id=session_id,
                caller_roles=caller_roles,
                caller_subject=caller_subject,
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        raise _AskRequestTimedOut(timeout_seconds)
    if "error" in error:
        raise error["error"]
    return result["state"]


@app.post("/ask", response_model=AskResponse)
def ask(
    payload: AskRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> AskResponse:
    """Runs one question through the full agent graph -- schema retrieval,
    SQL generation, validation, cost estimation, execution, self-correction
    -- via the same
    `agent.orchestrator.graph.run_orchestrated` call the React dashboard
    ultimately triggers (which itself is a
    pure pass-through to `agent.graph.run_agent` unless
    `ENABLE_MULTI_SOURCE_ROUTER` is set -- see that module's docstring).
    Every safety layer that governs the UI (input guard, SQL validator, row
    cap, timeout, LLM-call rate limit, sensitive-column blocking) applies
    identically here, since it's the same underlying graph.

    `payload.session_id`, if supplied, is echoed back unchanged; if omitted
    (the first turn of a new conversation), a fresh one is generated and
    returned -- see `AskRequest.session_id`'s docstring for what this does
    and does not do today (a correlation token, not yet a server-side
    session key).
    """
    session_id = payload.session_id or str(uuid.uuid4())

    client_ip = request.client.host if request.client else "unknown"
    rate_limit_result = _limiter_for(client_ip).check()
    if not rate_limit_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=QUESTION_LIMIT_MESSAGE,
            headers={"Retry-After": str(int(rate_limit_result.retry_after_seconds) + 1)},
        )

    conversation_history: list[ConversationExchange] = [
        ConversationExchange(
            question=turn.question, sql=turn.sql, tables=turn.tables, status=turn.status
        )
        for turn in payload.conversation_history
    ]

    settings = get_settings()
    try:
        final_state = _run_orchestrated_with_timeout(
            payload.question,
            conversation_history,
            enable_insight=payload.enable_insight,
            session_id=session_id,
            caller_roles=identity.roles,
            caller_subject=identity.subject if identity.mode == "oidc" else None,
            timeout_seconds=settings.request_timeout_seconds,
        )
    except _AskRequestTimedOut:
        # See Settings.request_timeout_seconds's docstring: this is "stop
        # waiting," not true cancellation -- the abandoned background thread
        # keeps running and its result is discarded, but the caller gets a
        # timely response instead of an indefinitely blocked request.
        logger.warning(
            "[api] /ask exceeded request_timeout_seconds=%ds (session_id=%s)",
            settings.request_timeout_seconds,
            session_id,
        )
        final_state = {
            "status": "failed",
            "error_history": [
                f"The request exceeded the maximum processing time "
                f"({settings.request_timeout_seconds}s) and was abandoned."
            ],
        }
    except AgentError as exc:
        # A source (schema retrieval today; document/policy RAG or web
        # search tomorrow, once they're wired into run_orchestrated the
        # same way) failed outside the graph's own internal retry/self-
        # correction handling. Same 200-with-a-"failed"-body shape
        # returned for every other exception here, built from
        # exc.safe_message rather than str(exc) -- the full detail is
        # logged, never returned. See agent/exceptions.py's module
        # docstring for why both exist on every AgentError.
        logger.error(
            "[api] /ask failed with %s (session_id=%s): %s",
            type(exc).__name__,
            session_id,
            exc,
            exc_info=exc,
        )
        final_state = {"status": "failed", "error_history": [exc.safe_message]}

    persisted_conversation_id: str | None = None
    try:
        persisted_conversation_id = persist_ask_turn(
            identity=identity,
            conversation_id=payload.conversation_id,
            question=payload.question,
            final_state=final_state,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - see api/chat_persistence.py's own docstring
        logger.warning(
            "[api] /ask: chat-history persistence failed unexpectedly (session_id=%s): %s",
            session_id,
            exc,
        )

    return _ask_response_from_state(final_state, session_id, persisted_conversation_id)


@app.post("/execute", response_model=ExecuteResponse)
def execute(
    payload: ExecuteRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_permission(Permission.EXECUTE_SQL)),
) -> ExecuteResponse:
    """Validates and executes a specific SQL string read-only -- the exact
    `validate_sql` -> `enforce_row_limit` -> `qualify_table_schema` ->
    `execute_readonly_sql` sequence the React dashboard's "Confirm and Run"
    button calls this route to run. This is the "SQL is untrusted output,
    always" rule applied at this layer too: `sql` is always re-validated
    and re-executed exactly as supplied, never
    trusted because it happens to look like something `/ask` returned.

    Rate-limited per client IP (`enforce_api_action_rate_limit`) -- unlike
    `/ask`, this route previously had no rate limit of its own at all.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "execute", settings)
    if not settings.databases:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No database configured."
        )
    database_name = payload.database or settings.databases[0].name
    try:
        db_config = get_connection(settings, database_name)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown database {database_name!r}."
        ) from exc

    dialect = get_sqlglot_dialect(db_config.db_type)
    validation = validate_sql(payload.sql, dialect=dialect)
    if not validation.is_valid:
        return ExecuteResponse(
            status="rejected", database=database_name, error=f"Rejected: {validation.error}"
        )

    if validation.normalized_sql is None:
        # 2026 Phase 3 security review (bandit B101): explicit raise, not
        # `assert` -- stripped under `python -O`. `ValidationResult`'s own
        # model_validator (agent/sql_validator.py) already guarantees
        # normalized_sql is set whenever is_valid is True, so this is a
        # precondition violation, not a real runtime case.
        raise RuntimeError("validate_sql returned is_valid=True with no normalized_sql")
    safe_sql = enforce_row_limit(
        validation.normalized_sql, settings.max_result_rows, dialect=dialect
    )
    # Schema-qualifies unqualified table references for this execution only
    # -- see qualify_table_schema's docstring. `normalized_sql` in the
    # response stays bare-named, matching the dashboard's editable-SQL-box
    # convention (the box never shows the schema-qualified form).
    execution_sql = qualify_table_schema(safe_sql, db_config.db_schema, dialect=dialect)

    try:
        start = time.perf_counter()
        columns, rows = execute_readonly_sql(
            execution_sql,
            settings.query_timeout_seconds,
            settings.max_result_rows,
            engine=get_read_only_engine(db_config),
        )
        duration_ms = (time.perf_counter() - start) * 1000
    except (SQLAlchemyError, TimeoutError) as exc:
        # Passed db_config (not the global settings) so the exact password
        # redacted is the one actually in play for this connection -- see
        # security.redaction.redact_secrets' docstring.
        safe_detail = redact_secrets(str(exc), db_config)
        return ExecuteResponse(
            status="failed", database=database_name, error=f"Execution failed: {safe_detail}"
        )

    chart_json: dict[str, Any] | None = None
    figure = build_chart(pd.DataFrame(rows, columns=columns))
    if figure is not None:
        # figure.to_json() (Plotly's own encoder, not jsonable_encoder) is
        # what correctly handles the numpy arrays/pandas Timestamps a
        # Plotly figure's data traces are built from.
        chart_json = json.loads(figure.to_json())

    return ExecuteResponse(
        status="succeeded",
        database=database_name,
        normalized_sql=safe_sql,
        result_columns=columns,
        result_rows=_rows_to_json(rows),
        row_count=len(rows),
        duration_ms=duration_ms,
        chart=chart_json,
    )


@app.post(
    "/feedback/golden-example",
    response_model=GoldenExampleFeedbackResponse,
)
def feedback_golden_example(
    payload: GoldenExampleFeedbackRequest,
    _identity: AuthIdentity = Depends(require_permission(Permission.GOLDEN_EXAMPLE_WRITE)),
) -> GoldenExampleFeedbackResponse:
    """Records a human-approved (question, SQL) pair for future few-shot
    retrieval -- the same `embeddings.golden_examples.save_golden_example`
    call the dashboard's thumbs-up widget makes when a user confirms a result.
    `sql` should be the exact SQL that was actually run and confirmed
    correct (e.g. from a prior `/execute` call), not necessarily the
    original `/ask` draft if the caller edited it.
    """
    save_golden_example(payload.question, payload.sql, payload.database, get_settings())
    return GoldenExampleFeedbackResponse(saved=True)


@app.post("/schema/refresh", response_model=SchemaRefreshResponse)
def schema_refresh(
    request: Request,
    _identity: AuthIdentity = Depends(require_permission(Permission.SCHEMA_REFRESH)),
) -> SchemaRefreshResponse:
    """Re-introspects and re-embeds every configured database's schema --
    the same `refresh_all_schema_indexes` call the React dashboard's
    "Refresh Schema" button makes. Skips re-embedding a database whose
    schema fingerprint hasn't changed (see that function's docstring), so
    calling this when nothing changed is cheap.

    Rate-limited per client IP (`enforce_api_action_rate_limit`) -- this
    route previously had no rate limit of its own at all, despite
    re-introspecting/re-embedding every configured database being real
    work.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "schema_refresh", settings)
    results = refresh_all_schema_indexes(settings)
    return SchemaRefreshResponse(
        databases=[
            SchemaRefreshResult(database=db_name, table_count=len(tables))
            for db_name, tables in results.items()
        ]
    )


@app.get("/metrics/performance", response_model=PerformanceMetricsResponse)
def metrics_performance(
    _identity: AuthIdentity = Depends(require_permission(Permission.ADMIN_CONFIG)),
) -> PerformanceMetricsResponse:
    """A live rollup of per-LangGraph-stage timing across recent `/ask`
    requests (`observability.metrics`) -- turns the `[timing] stage=...`
    log lines `agent.nodes._timed_node` has always emitted into a
    queryable snapshot, without adding any new per-request instrumentation.
    Admin-only: while nothing returned here is question/answer content
    (only stage names and aggregate durations), it is still an internal
    operational view, gated the same way `/schema/refresh` already is.

    Single-process, in-memory, resets on restart -- see
    `observability/metrics.py`'s own docstring for why, and
    `docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md` for the broader
    observability assessment this endpoint is the first concrete step of.
    """
    snapshot = get_default_metrics().snapshot()
    return PerformanceMetricsResponse(
        started_at=snapshot["started_at"],
        window_requests=snapshot["window_requests"],
        max_window_requests=snapshot["max_window_requests"],
        requests=RequestMetricOut(**snapshot["requests"]),
        stages=[StageMetricOut(**stage) for stage in snapshot["stages"]],
        status_counts=snapshot["status_counts"],
    )


@app.get("/schema/tables", response_model=TablesResponse)
def schema_tables(
    database: str | None = None,
    _identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> TablesResponse:
    """Live schema listing (table/column names, types) -- the same
    `db.schema_introspection.introspect_schema` the UI's sidebar schema
    browser and `scripts/build_embeddings.py` use, metadata-only (no data
    queries), reflecting the database(s) as they are right now.

    Args:
        database: Optional `Settings.databases[i].name` filter. Omitted
            (the default) returns every configured database's tables,
            each tagged with its `database` name; raises 404 if `database`
            names a connection that isn't configured.
    """
    settings = get_settings()
    if database is not None and database not in {config.name for config in settings.databases}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown database {database!r}."
        )

    configs = (
        [config for config in settings.databases if config.name == database]
        if database is not None
        else settings.databases
    )

    tables_out: list[TableOut] = []
    for config in configs:
        engine = get_read_only_engine(config)
        for table in introspect_schema(engine, config.db_schema):
            tables_out.append(
                TableOut(
                    database=config.name,
                    table_name=table.table_name,
                    columns=[
                        ColumnOut(
                            name=column.name,
                            type=column.type,
                            nullable=column.nullable,
                            is_primary_key=column.is_primary_key,
                        )
                        for column in table.columns
                    ],
                )
            )
    return TablesResponse(tables=tables_out)


# Serves the built React frontend (frontend/dist, `npm run build`) from this
# same process -- what makes "single origin, no CORS needed" true in
# production. A no-op (one-time log line, not a startup failure) if the
# frontend hasn't been built yet -- this API is fully usable standalone
# during development.
_frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # Vite's build output puts every hashed JS/CSS bundle under /assets --
    # a plain mount is enough for those (real files only, never a directory
    # listing or a fallback).
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> FileResponse:
        """Serves any other built root-level file (favicon, etc.) verbatim,
        and falls back to `index.html` for everything else -- what lets
        react-router's client-side routes (e.g. `/knowledge-sources`)
        survive a hard refresh instead of 404ing on this server. Registered
        last (after every API route above), so those always win first --
        FastAPI/Starlette matches routes in registration order.
        """
        candidate = _frontend_dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_frontend_dist / "index.html")

else:
    logger.info(
        "[startup] frontend/dist not found -- run `npm run build` in frontend/ to serve the "
        "React UI from this API process. The API itself is unaffected."
    )
