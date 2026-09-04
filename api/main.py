"""FastAPI app: a REST surface over the same LangGraph agent the Streamlit
UI drives -- see `api/__init__.py`'s module docstring for why `/ask` is a
thin wrapper around `agent.graph.run_agent`, not a second implementation.

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
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder

# `uvicorn api.main:app` does not guarantee the repo root is on sys.path
# (unlike running as an installed package) -- same reasoning as
# ui/app.py's identical sys.path.insert for `streamlit run`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.exceptions import SchemaRetrievalError
from agent.graph import run_agent
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
from config.settings import configure_logging, get_settings
from db.connection import get_read_only_engine, test_connection
from db.schema_introspection import introspect_schema
from embeddings.schema_indexer import get_chroma_client, get_collection
from security.audit_log import reset_correlation_id, set_correlation_id

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Text-to-SQL API",
    description=__doc__,
    version="0.1.0",
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


def _ask_response_from_state(state: AgentState) -> AskResponse:
    result_rows = state.get("result_rows")
    return AskResponse(
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
            schema_index = ComponentHealth(ok=False, detail=f"Unreachable: {exc}")

        databases.append(
            DatabaseHealth(name=config.name, connection=connection, schema_index=schema_index)
        )

    try:
        import ollama

        ollama.Client(host=settings.ollama_host).list()
        ollama_health = ComponentHealth(ok=True, detail=f"Reachable at {settings.ollama_host}.")
    except Exception as exc:  # noqa: BLE001 - health check must never crash the endpoint
        ollama_health = ComponentHealth(ok=False, detail=f"Unreachable: {exc}")

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
    -- exactly as `ui/app.py` does via the same `agent.graph.run_agent`
    call. Every safety layer that governs the UI (input guard, SQL
    validator, row cap, timeout, LLM-call rate limit, sensitive-column
    blocking) applies identically here, since it's the same graph.
    """
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
        final_state = run_agent(
            payload.question, conversation_history, enable_insight=payload.enable_insight
        )
    except SchemaRetrievalError as exc:
        # Same fallback shape ui/app.py uses for the same exception -- see
        # that module's chat-input handler.
        final_state = {"status": "failed", "error_history": [str(exc)]}

    return _ask_response_from_state(final_state)


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
