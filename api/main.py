"""FastAPI app: a REST surface over the same LangGraph agent the Streamlit
UI drives -- see `api/__init__.py`'s module docstring for why `/ask` is a
thin wrapper around `agent.orchestrator.graph.run_orchestrated`, not a
second implementation.

Run with (see `docs/API.md`/`docs/DEPLOYMENT.md` for the full picture):

    uvicorn api.main:app --host 0.0.0.0 --port 8000

Never the only interface this project ships -- `ui/app.py` remains the
primary, human-facing surface. This exists for programmatic/scripted access
and as the foundation `docs/DEPLOYMENT.md`'s reverse-proxy guidance sits in
front of.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

# `uvicorn api.main:app` does not guarantee the repo root is on sys.path
# (unlike running as an installed package) -- same reasoning as
# ui/app.py's identical sys.path.insert for `streamlit run`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.exceptions import AgentError
from agent.graph import build_graph
from agent.llm_client import get_ollama_client
from agent.orchestrator.graph import build_orchestrator_graph, run_orchestrated
from agent.rate_limit import QUESTION_LIMIT_MESSAGE, SlidingWindowRateLimiter
from agent.state import AgentState, ConversationExchange
from api.auth import verify_api_key
from api.schemas import (
    AskRequest,
    AskResponse,
    AttemptRecordOut,
    ColumnOut,
    ComponentHealth,
    DatabaseHealth,
    HealthResponse,
    TableOut,
    TablesResponse,
)
from config.settings import ConfigurationError, configure_logging, get_settings
from db.connection import get_read_only_engine, test_connection
from db.schema_introspection import introspect_schema
from embeddings.schema_indexer import get_chroma_client, get_collection
from security.audit_log import get_correlation_id, reset_correlation_id, set_correlation_id
from security.redaction import redact_secrets

configure_logging()
logger = logging.getLogger(__name__)


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
    functions are also called from `ui/app.py`, `eval/runner.py`, and
    scripts that never go through this FastAPI app at all.
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


# Per-client-IP question-submission limiter, mirroring ui/app.py's
# per-Streamlit-session `SlidingWindowRateLimiter` -- the API has no
# server-side session concept, so client IP is the closest equivalent scope.
# The stricter, process-wide LLM-*call* limiter (agent.rate_limit's other
# limiter) already applies automatically inside generate_sql_node -- this
# one only adds the question-submission-level layer the UI also has.
_ip_limiters: dict[str, SlidingWindowRateLimiter] = {}


def _limiter_for(client_ip: str) -> SlidingWindowRateLimiter:
    limiter = _ip_limiters.get(client_ip)
    if limiter is None:
        limiter = SlidingWindowRateLimiter(
            max_events=get_settings().question_rate_limit_per_minute,
            window_seconds=60.0,
            name=f"api_questions[{client_ip}]",
        )
        _ip_limiters[client_ip] = limiter
    return limiter


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


def _attempt_records_out(state: AgentState) -> list[AttemptRecordOut]:
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


def _ask_response_from_state(state: AgentState, session_id: str) -> AskResponse:
    result_rows = state.get("result_rows")
    return AskResponse(
        session_id=session_id,
        status=state.get("status", "failed"),
        database=state.get("selected_database"),
        sql=state.get("sql"),
        result_columns=state.get("result_columns"),
        result_rows=jsonable_encoder(result_rows) if result_rows is not None else None,
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
            # redact_secrets: this endpoint is unauthenticated (no
            # Depends(verify_api_key), unlike /ask and /schema/tables --
            # health checks typically need to be reachable by an
            # orchestrator with no API key), so raw driver/Chroma text
            # ending up here is a real, publicly-visible leak, not just an
            # internal one.
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
    )


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(verify_api_key)])
def ask(payload: AskRequest, request: Request) -> AskResponse:
    """Runs one question through the full agent graph -- schema retrieval,
    SQL generation, validation, cost estimation, execution, self-correction
    -- exactly as `ui/app.py` does via the same
    `agent.orchestrator.graph.run_orchestrated` call (which itself is a
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

    try:
        final_state = run_orchestrated(
            payload.question, conversation_history, enable_insight=payload.enable_insight
        )
    except AgentError as exc:
        # A source (schema retrieval today; document/policy RAG or web
        # search tomorrow, once they're wired into run_orchestrated the
        # same way) failed outside the graph's own internal retry/self-
        # correction handling. Same 200-with-a-"failed"-body shape
        # ui/app.py uses for the same exceptions, just now built from
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

    return _ask_response_from_state(final_state, session_id)


@app.get("/schema/tables", response_model=TablesResponse, dependencies=[Depends(verify_api_key)])
def schema_tables(database: str | None = None) -> TablesResponse:
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
