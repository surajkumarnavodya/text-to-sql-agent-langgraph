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

import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any, Literal
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

from analytics.engine import compute_analytics_result
from analytics.visualization import build_chart_spec
from governance.result_policy import govern_rows_for_caller

from agent.authz import Permission
from agent.exceptions import AgentError
from agent.graph import build_graph
from agent.llm_client import get_ollama_client
from agent.model_registry import (
    InvalidModelSelectionError,
    build_model_options,
    installed_model_names,
    validate_model_selection,
)
from agent.orchestrator.graph import build_orchestrator_graph, run_orchestrated
from agent.rate_limit import (
    ASK_CONCURRENCY_LIMIT_MESSAGE,
    PER_CALLER_ASK_CONCURRENCY_LIMIT_MESSAGE,
    QUESTION_LIMIT_MESSAGE,
    BoundedLimiterCache,
    SlidingWindowRateLimiter,
    get_ask_concurrency_limiter,
    get_database_execution_limiter,
    get_per_caller_ask_concurrency_limiter,
)
from agent.result_charting import classify_columns, recommend_chart
from agent.sql_validator import enforce_row_limit, qualify_table_schema, validate_sql
from agent.state import ConversationExchange
from api.analyst import router as analyst_router
from api.attachments import router as attachments_router
from api.authz import require_permission
from api.chat_history import router as chat_history_router
from api.chat_persistence import persist_ask_turn, persist_execute_result
from api.documents import router as documents_router
from api.generation import router as generation_router
from api.identity_auth import router as identity_auth_router
from api.media import router as media_router
from api.media_library import router as media_library_router
from api.media_search import router as media_search_router
from api.navigation import router as navigation_router
from api.onboarding import router as onboarding_router
from api.platform_admin import router as platform_admin_router
from api.rate_limit import enforce_api_action_rate_limit
from api.recommendation_governance import router as recommendation_governance_router
from api.recommendation_persistence import persist_ask_recommendations
from api.schemas import (
    AccessibilityMetadataOut,
    AnalyticalIntentOut,
    AnalyticalPlanOut,
    AskRequest,
    AskResponse,
    AttachmentResultOut,
    AttemptRecordOut,
    ChartFieldOut,
    ChartRecommendationOut,
    ChartSortOut,
    CitationOut,
    ColumnOut,
    ComponentHealth,
    ConversationExchangeOut,
    DatabaseHealth,
    ExecuteRequest,
    ExecuteResponse,
    ForecastResultOut,
    GoldenExampleFeedbackRequest,
    GoldenExampleFeedbackResponse,
    GoverningMetricOut,
    HealthResponse,
    MediaGenerationResultOut,
    MediaSearchHitOut,
    MediaSearchResultOut,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
    ModelOut,
    ModelsResponse,
    PerformanceMetricsResponse,
    RequestMetricOut,
    SchemaRefreshResponse,
    SchemaRefreshResult,
    SchemaTableOut,
    SourceAnswerOut,
    StageMetricOut,
    TableOut,
    TablesResponse,
    VisualizationSpecOut,
)
from api.semantic_catalog import router as semantic_catalog_router
from api.semantic_intelligence import router as semantic_intelligence_router
from api.shares import router as shares_router
from api.tenant_admin import router as tenant_admin_router
from api.voice import router as voice_router
from config.settings import ConfigurationError, Settings, configure_logging, get_settings
from db.connection import (
    check_write_privileges,
    get_read_only_engine,
    get_sqlglot_dialect,
    test_connection,
)
from db.execution import execute_readonly_sql
from db.result_cache import get_result_cache, is_cacheable_sql
from db.schema_introspection import introspect_schema
from embeddings.golden_examples import save_golden_example
from embeddings.schema_indexer import (
    get_chroma_client,
    get_collection,
    get_last_discovery_diff,
    refresh_all_schema_indexes,
)
from feedback.store import save_response_feedback
from observability.metrics import get_default_metrics
from security.audit_log import (
    get_correlation_id,
    log_security_event,
    reset_correlation_id,
    set_correlation_id,
)
from security.client_ip import resolve_client_ip
from security.oidc import AuthIdentity, real_caller_subject
from security.redaction import redact_configured_secrets, redact_secrets
from security.tenancy import DEFAULT_TENANT_ID, resolve_tenant_id_for_identity

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


def _warn_on_ask_concurrency_pool_mismatch(settings) -> None:
    """Enterprise scalability assessment (2026-09-27): warns at startup when
    this process could admit more concurrent `/ask` requests
    (`Settings.max_concurrent_ask_requests`) than its own SQLAlchemy
    connection pool can serve for a configured database
    (`Settings.db_pool_size + db_max_overflow`, applied uniformly to every
    `Settings.databases` entry -- there is no per-database pool override).

    Never blocks startup (unlike `_enforce_database_write_privileges`) --
    this is a latency/throughput tuning signal, not a safety boundary: an
    admitted request that can't immediately check out a pooled connection
    simply queues behind one that's still using it (SQLAlchemy's own
    `pool_timeout`, then a clear error if that's also exceeded), which is
    correct, bounded behavior, not data loss or a crash. Left uncorrected it
    just means some fraction of "concurrently admitted" requests are
    actually waiting on a connection rather than truly running in parallel
    -- worth knowing at startup rather than discovering it as an unexplained
    latency plateau under load.

    A separate, plain function (mirroring `_enforce_database_write_privileges`'s
    own reasoning) so it's unit-testable without spinning up the whole app.
    """
    pool_capacity = settings.db_pool_size + settings.db_max_overflow
    if settings.max_concurrent_ask_requests <= pool_capacity:
        return
    logger.warning(
        "[startup] MAX_CONCURRENT_ASK_REQUESTS=%d exceeds this process's own "
        "per-database connection pool capacity (DB_POOL_SIZE=%d + "
        "DB_MAX_OVERFLOW=%d = %d), for %d configured database(s). Requests "
        "beyond %d will queue for a pooled connection rather than running "
        "truly in parallel -- correct, bounded behavior (not an error), but "
        "worth raising DB_POOL_SIZE/DB_MAX_OVERFLOW (subject to your "
        "database server's own max_connections, across every replica of "
        "this API process) or lowering MAX_CONCURRENT_ASK_REQUESTS if this "
        "wasn't intentional.",
        settings.max_concurrent_ask_requests,
        settings.db_pool_size,
        settings.db_max_overflow,
        pool_capacity,
        len(settings.databases),
        pool_capacity,
    )


def _shutdown_ask_executor(settings) -> None:
    """Drains `_get_ask_executor`'s bounded `/ask` thread pool on shutdown.

    Enterprise scalability assessment (2026-09-27): without this, a SIGTERM
    (a rolling-deployment/orchestrator-initiated restart, or `docker compose
    stop`) tears down the process while the pool may still have in-flight
    `/ask` work running -- a graph mid-generation/mid-execution, holding a
    live DB connection and an in-flight Ollama call. `ThreadPoolExecutor
    .shutdown(wait=True)` blocks exactly until every already-submitted task
    finishes (never accepting new ones -- but nothing new arrives here
    anyway, since ASGI servers stop routing new requests to a shutting-down
    app before calling this) rather than abandoning them mid-flight.
    `_get_ask_executor(...)` retrieves the *same* `@cache`d instance every
    request already shares -- calling it again here does not create a
    second pool. Bounded by however long the slowest in-flight request
    takes (ultimately `Settings.request_timeout_seconds`); an operator's own
    orchestrator graceful-termination grace period should be set with that
    in mind (see docs/DEPLOYMENT.md).

    A separate, plain function (mirroring `_enforce_database_write_privileges`'s
    own reasoning) so it's unit-testable without spinning up the whole app.
    """
    _get_ask_executor(settings.max_concurrent_ask_requests).shutdown(wait=True)


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
    _warn_on_ask_concurrency_pool_mismatch(settings)

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

    _shutdown_ask_executor(settings)


app = FastAPI(
    title="Text-to-SQL API",
    description=__doc__,
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(analyst_router)
app.include_router(attachments_router)
app.include_router(documents_router)
app.include_router(media_router)
app.include_router(generation_router)
app.include_router(voice_router)
app.include_router(media_search_router)
app.include_router(media_library_router)
app.include_router(identity_auth_router)
app.include_router(chat_history_router)
app.include_router(shares_router)
app.include_router(onboarding_router)
app.include_router(semantic_catalog_router)
app.include_router(semantic_intelligence_router)
app.include_router(recommendation_governance_router)
app.include_router(platform_admin_router)
app.include_router(tenant_admin_router)
app.include_router(navigation_router)

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

    # 2026-09-28, Google sign-in: Google Identity Services'
    # `https://accounts.google.com/gsi/client` script renders the
    # "Continue with Google" button and, on click, opens a Google-hosted
    # popup/iframe to obtain the credential -- conditional on
    # `google_oauth_client_id` actually being configured, never granted
    # for a deployment not using this feature at all.
    script_src = "'self'"
    connect_src = "'self'"
    if settings.google_oauth_client_id is not None:
        script_src += " https://accounts.google.com"
        connect_src += " https://accounts.google.com"
        frame_src += " https://accounts.google.com"

    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        f"connect-src {connect_src}; "
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


# Per-caller question-submission limiter. The stricter, process-wide
# LLM-*call* limiter (agent.rate_limit's other limiter) already applies
# automatically inside generate_sql_node -- this one adds a separate
# question-submission-level layer on top of it. Bounded (see
# BoundedLimiterCache's docstring) -- previously a bare dict that grew one
# entry per distinct key forever (2026 Phase 1 security review, finding
# API-02).
_ip_limiters = BoundedLimiterCache()


def _limiter_for(key: str) -> SlidingWindowRateLimiter:
    return _ip_limiters.get_or_create(
        key,
        max_events=get_settings().question_rate_limit_per_minute,
        window_seconds=60.0,
        name=f"api_questions[{key}]",
    )


def _rate_limit_key(identity: AuthIdentity, request: Request, settings: Settings) -> str:
    """The key every `/ask`-scoped rate/concurrency limiter uses for one
    caller -- scale-out hardening pass (`docs/SCALE_OUT_PROMPT.md`
    bottleneck #3): raw client IP is meaningless behind a load balancer
    (every caller shares the LB's IP) or carrier NAT (thousands of users
    share one IP), and this app now has a real per-caller identity
    whenever local/OIDC auth is configured. Falls back to client IP only
    when `security.oidc.real_caller_subject` reports there isn't one
    (`none`/`static_token` modes, where every caller shares one fixed
    sentinel) -- see that function's own docstring.

    The IP fallback itself goes through `security.client_ip.resolve_client_ip`
    (enterprise scalability assessment, 2026-09-27) rather than reading
    `request.client.host` directly, so a deployment that puts a trusted
    reverse proxy in front of this app can opt into correct per-client
    keying via `Settings.trusted_proxy_count` instead of every "none"/
    "static_token"-mode caller silently sharing the proxy's own bucket.
    """
    subject = real_caller_subject(identity)
    if subject is not None:
        return f"user:{subject}"
    return f"ip:{resolve_client_ip(request, settings)}"


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


def _json_safe_cell(value: Any) -> Any:
    """Replaces a raw binary value with a short, readable placeholder.

    A real crash, found via live use: `fastapi.encoders.jsonable_encoder`'s
    default handling for `bytes` is `lambda o: o.decode()` -- a bare UTF-8
    decode, which raises `UnicodeDecodeError` (crashing the *entire*
    response, not just that one cell) the moment a SELECT touches a
    genuinely binary column, e.g. AdventureWorks' own
    `Production.ProductPhoto.LargePhoto` (a `varbinary(max)` storing real
    JPEG bytes). This app's SQL validator has no reason to reject such a
    column (it's an ordinary, safe, read-only SELECT), so this must be
    handled at serialization time, not prevented upstream. A results table
    has no useful way to render raw binary anyway, so this is a short
    placeholder -- never a base64 dump, which could turn one cell into
    several MB of response body for a large image/blob column.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<binary data, {len(value)} bytes>"
    return value


def _rows_to_json(rows: list[Any] | None) -> list[list[Any]] | None:
    """Converts DB result rows to plain JSON-encodable lists.

    `db.execution.execute_readonly_sql` returns `sqlalchemy.engine.row.Row`
    objects, not plain tuples -- `Row` behaves like a tuple (iterable,
    indexable) but isn't recognized as one by `fastapi.encoders
    .jsonable_encoder`'s isinstance checks, so passing it directly raises
    (its dict()/vars() object-fallback path doesn't apply to Row either).
    Converting to `list(row)` first sidesteps that entirely; jsonable_encoder
    still does the real work of encoding each row's actual values (Decimal,
    datetime, UUID, ...) -- `_json_safe_cell` runs first so a binary value
    never reaches jsonable_encoder's own crash-prone default handling.
    """
    if rows is None:
        return None
    return jsonable_encoder([[_json_safe_cell(value) for value in row] for row in rows])


def _redact_text(text: str | None, settings: Any) -> str | None:
    """Applies `security.redaction.redact_configured_secrets` to one
    caller-visible text field -- a last-line defense-in-depth net in case a
    real secret value ever reached LLM context and got parroted back (no
    secret is ever intentionally included in a prompt; see that function's
    own docstring). A `None` input passes through unchanged."""
    if text is None:
        return None
    return redact_configured_secrets(text, settings)


def _source_answer_out(result: Mapping[str, Any] | None, settings: Any) -> SourceAnswerOut | None:
    """Converts one `agent.orchestrator.state.SourceAnswer` dict (document_result/
    policy_result/web_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return SourceAnswerOut(
        answer=_redact_text(result.get("answer", ""), settings) or "",
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
    result: Mapping[str, Any] | None, settings: Any
) -> MediaGenerationResultOut | None:
    """Converts one `agent.orchestrator.state.MediaGenerationResult` dict
    (generation_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return MediaGenerationResultOut(
        answer=_redact_text(result.get("answer", ""), settings) or "",
        status=result.get("status", "succeeded"),
        media_id=result.get("media_id"),
        media_type=result.get("media_type"),
        model=result.get("model"),
    )


def _media_search_result_out(
    result: Mapping[str, Any] | None, settings: Any
) -> MediaSearchResultOut | None:
    """Converts one `agent.orchestrator.state.MediaSearchResult` dict
    (media_search_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return MediaSearchResultOut(
        answer=_redact_text(result.get("answer", ""), settings) or "",
        status=result.get("status", "succeeded"),
        hits=[
            MediaSearchHitOut(
                media_id=hit["media_id"],
                media_type=hit["media_type"],
                caption=_redact_text(hit["caption"], settings) or "",
                timestamp_start=hit.get("timestamp_start"),
                timestamp_end=hit.get("timestamp_end"),
            )
            for hit in result.get("hits", [])
        ],
    )


def _attachment_result_out(
    result: Mapping[str, Any] | None, settings: Any
) -> AttachmentResultOut | None:
    """Converts one `agent.orchestrator.state.AttachmentResult` dict
    (attachment_result) to its API shape -- None passes through as None."""
    if result is None:
        return None
    return AttachmentResultOut(
        answer=_redact_text(result.get("answer", ""), settings) or "",
        status=result.get("status", "succeeded"),
        used_attachment_ids=result.get("used_attachment_ids", []),
        vision_unavailable=result.get("vision_unavailable", False),
    )


def _forecast_result_out(result: Mapping[str, Any] | None) -> ForecastResultOut | None:
    """Converts one `analytics.models.ForecastResult.model_dump()` dict
    (`AgentState["forecast_result"]`) to its API shape -- `None` passes
    through as `None`. No redaction applied (unlike `insight`/
    `rejection_message`/etc.) -- a forecast's text fields
    (`summary`/`limitations`/`rejection_reasons`) are built entirely from
    this module's own fixed strings and the question's already-validated
    historical period labels/values, never from raw LLM or database-error
    text, so there's nothing here `security.redaction.redact_secrets`
    would ever need to catch.
    """
    if result is None:
        return None
    return ForecastResultOut(**result)


def _analytical_intent_out(intent: Mapping[str, Any] | None) -> AnalyticalIntentOut | None:
    """Converts `AgentState["analytical_intent"]` (an
    `agent.intent.AnalyticalIntentClassification.model_dump()` dict) to its
    API shape. Pass-through of already-validated typed data -- no redaction
    applied, since its only free-text fields are LLM-produced phrases from
    the question itself, and the same question text is already echoed back
    to the caller verbatim in every response."""
    if intent is None:
        return None
    return AnalyticalIntentOut(**intent)


def _analytical_plan_out(plan: Mapping[str, Any] | None) -> AnalyticalPlanOut | None:
    """Converts `AgentState["analytical_plan"]` (an
    `agent.analytical_plan.AnalyticalPlan.model_dump()` dict) to its API
    shape. Only ever a plan `agent.plan_validator.validate_plan` already
    accepted (`build_analytical_plan_node` discards an invalid one entirely,
    leaving this `None`) -- so every table/column name here has already
    been checked against the retrieved schema."""
    if plan is None:
        return None
    return AnalyticalPlanOut(**plan)


def _governing_metrics_out(metrics: Sequence[Mapping[str, Any]] | None) -> list[GoverningMetricOut]:
    """Converts `AgentState["governing_metrics"]` (plain dicts from
    `retrieval.retriever.extract_governing_metrics`) -- every entry is a
    published `CONFIRMED_BUSINESS_TRUTH` definition by construction. An empty
    list (the common case, no governed metric matched) passes through as
    `[]`, never `None`."""
    return [
        GoverningMetricOut(
            business_name=str(metric.get("business_name") or ""),
            approved_expression=metric.get("approved_expression"),
            aggregation=metric.get("aggregation"),
            text=str(metric.get("text") or ""),
        )
        for metric in (metrics or [])
    ]


#: Prompt 30 -- presentation-only mapping of an already-computed failure
#: category (`agent.nodes`'s restricted-column gate, retry-exhausted) to a
#: plain-language notice. The check, its retry behavior, and what it blocks
#: are all unchanged; this only names the cause in a way a business user can
#: act on (ask an admin for access) instead of a generic failure.
_RESTRICTED_FIELD_NOTICE = (
    "One or more fields this question needs are access-restricted for your role, "
    "so the answer could not be produced. Ask an administrator if you need access."
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
    settings = get_settings()
    error_history = state.get("error_history", [])
    query_plan = state.get("query_plan")
    return AskResponse(
        session_id=session_id,
        conversation_id=conversation_id,
        status=state.get("status", "failed"),
        database=state.get("selected_database"),
        model=state.get("selected_model"),
        sql=state.get("sql"),
        result_columns=state.get("result_columns"),
        result_rows=_rows_to_json(result_rows),
        row_count=state.get("row_count"),
        retry_count=state.get("retry_count", 0),
        attempt_history=_attempt_records_out(state),
        insight=_redact_text(state.get("insight"), settings),
        analytical_result=state.get("analytical_result"),
        forecast_result=_forecast_result_out(state.get("forecast_result")),
        recommendations=state.get("recommendations") or [],
        cost_notice=state.get("cost_notice"),
        low_confidence_notice=_redact_text(state.get("low_confidence_notice"), settings),
        rejection_reason=state.get("rejection_reason"),
        rejection_message=_redact_text(state.get("rejection_message"), settings),
        rate_limit_message=_redact_text(state.get("rate_limit_message"), settings),
        clarification_message=_redact_text(state.get("clarification_message"), settings),
        failure_explanation=_redact_text(state.get("failure_explanation"), settings),
        error_history=[redact_configured_secrets(e, settings) for e in error_history],
        sources_used=state.get("sources_used", []),
        synthesized_answer=_redact_text(state.get("synthesized_answer"), settings),
        document_result=_source_answer_out(state.get("document_result"), settings),
        policy_result=_source_answer_out(state.get("policy_result"), settings),
        web_result=_source_answer_out(state.get("web_result"), settings),
        generation_result=_media_generation_result_out(state.get("generation_result"), settings),
        media_search_result=_media_search_result_out(state.get("media_search_result"), settings),
        attachment_result=_attachment_result_out(state.get("attachment_result"), settings),
        query_plan=(
            [redact_configured_secrets(step, settings) for step in query_plan]
            if query_plan is not None
            else None
        ),
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
        permission_denied_notice=_redact_text(state.get("permission_denied_notice"), settings),
        analytical_intent=_analytical_intent_out(state.get("analytical_intent")),
        analytical_plan=_analytical_plan_out(state.get("analytical_plan")),
        governing_metrics=_governing_metrics_out(state.get("governing_metrics")),
        restricted_field_notice=(
            _RESTRICTED_FIELD_NOTICE
            if state.get("last_error_category") == "restricted_column"
            and state.get("status") == "failed"
            else None
        ),
    )


@app.get("/live", include_in_schema=False)
def live() -> dict[str, str]:
    """Liveness probe: "is this process alive and able to answer HTTP at
    all" -- nothing more. Enterprise scalability assessment (2026-09-27):
    `GET /health` right below is a real, non-cached *readiness* check (a
    live DB `SELECT 1` + Chroma collection count per configured database,
    plus an Ollama `.list()` call) -- correct for readiness, but the wrong
    thing to poll frequently as a liveness probe once this runs as N
    replicas behind an orchestrator: a liveness check firing every few
    seconds per replica would otherwise multiply into constant DB/Chroma/
    Ollama load purely from health-checking, not real traffic. This
    endpoint does none of that -- it never touches a database, Chroma, or
    Ollama, so its cost/latency is independent of how many of those are
    configured or how they're behaving. A process that can return this
    response is, by definition, able to accept and complete an HTTP
    request; whether its *dependencies* are healthy is `/health`'s job,
    meant to be polled far less frequently (an orchestrator's own
    *readiness* probe, or a human/dashboard check), not this one's.
    """
    return {"status": "alive"}


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

    # vision_model_available reuses this same `.list()` call rather than a
    # second round-trip to Ollama -- see HealthResponse.vision_model_available's
    # own docstring for why this specific check exists (a real, reported bug:
    # no way to tell "vision never configured" from "configured but the
    # model was never actually pulled" until a user's image question failed).
    # installed_model_names is shared with GET /models (agent/model_registry.py)
    # rather than kept as a private copy here.
    pulled_model_names: set[str] = set()
    try:
        pulled_model_names = installed_model_names(get_ollama_client(settings).list())
        ollama_health = ComponentHealth(ok=True, detail=f"Reachable at {settings.ollama_host}.")
    except Exception as exc:  # noqa: BLE001 - health check must never crash the endpoint
        ollama_health = ComponentHealth(ok=False, detail=f"Unreachable: {redact_secrets(str(exc))}")

    vision_enabled = bool(settings.media_vision_model)
    try:
        import pytesseract  # noqa: F401

        ocr_enabled = True
    except ImportError:
        ocr_enabled = False

    overall_ok = ollama_health.ok and all(
        db.connection.ok and db.schema_index.ok for db in databases
    )
    response.status_code = status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if overall_ok else "degraded",
        databases=databases,
        ollama=ollama_health,
        voice_enabled=settings.enable_voice_mode,
        share_public_links_enabled=bool(
            settings.enable_conversation_sharing and settings.share_public_links_enabled
        ),
        media_search_enabled=settings.enable_media_search,
        local_auth_enabled=settings.local_auth_enabled,
        # `google_client_id` is a PUBLIC identifier, never a secret (see
        # `security/google_oidc.py`'s own module docstring for why this
        # flow never holds a client secret at all) -- safe to serve from
        # this unauthenticated endpoint by design, the same "sanitized
        # capability endpoint" pattern this field itself follows: a
        # boolean availability flag plus the one public value the
        # frontend actually needs, never the rest of `Settings`.
        google_signin_enabled=settings.google_oauth_client_id is not None
        and settings.local_auth_enabled,
        google_client_id=(
            settings.google_oauth_client_id
            if settings.google_oauth_client_id is not None and settings.local_auth_enabled
            else None
        ),
        vision_enabled=vision_enabled,
        vision_provider="ollama" if vision_enabled else None,
        vision_model=settings.media_vision_model or None,
        vision_model_available=(
            settings.media_vision_model in pulled_model_names if vision_enabled else None
        ),
        ocr_enabled=ocr_enabled,
    )


class _AskRequestTimedOut(Exception):
    """Raised by `_run_orchestrated_with_timeout` when `run_orchestrated`
    hasn't returned within `Settings.request_timeout_seconds` -- see that
    setting's docstring for the reliability gap this closes and its
    "stop waiting, not true cancellation" caveat."""


@cache
def _get_ask_executor(max_workers: int) -> ThreadPoolExecutor:
    """Process-wide bounded thread pool for `/ask` graph executions --
    scale-out hardening pass (`docs/SCALE_OUT_PROMPT.md` bottleneck #1):
    this replaced a raw `threading.Thread()` spawned fresh per request,
    which under load could create an unbounded number of real OS threads
    (each holding its own stack, DB connections, and an in-flight Ollama
    call) with nothing to stop it. Sized to `Settings
    .max_concurrent_ask_requests` -- `api/main.py`'s `ask()` admits at most
    that many concurrent requests via `agent.rate_limit
    .get_ask_concurrency_limiter` *before* ever submitting here, so this
    pool is never oversubscribed by admitted work. `@cache`, same
    process-lifetime-singleton pattern as `agent.llm_client
    ._get_ollama_client` -- a `Settings` change takes effect on restart,
    like every other cached-until-restart singleton in this codebase.

    Still not true cancellation (see `_run_orchestrated_with_timeout`'s own
    docstring) -- an abandoned task keeps running to completion inside this
    pool. What bounding the pool buys is that an abandoned task now visibly
    occupies one of a *fixed* number of slots (correct backpressure: fewer
    slots free for new work under sustained overload, surfacing as more
    429s) instead of an invisible, unbounded extra OS thread.
    """
    return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ask-worker")


def _run_orchestrated_with_timeout(
    question: str,
    conversation_history: list[ConversationExchange],
    *,
    enable_insight: bool,
    session_id: str,
    caller_roles: tuple[str, ...],
    caller_subject: str | None,
    timeout_seconds: int,
    max_workers: int,
    on_done: Callable[[], None],
    attachment_ids: list[str] | None = None,
    model: str | None = None,
    tenant_id: str | None = None,
    forecast_horizon: int | None = None,
) -> Mapping[str, Any]:
    """Runs `run_orchestrated` on `_get_ask_executor`'s bounded pool and
    gives up *waiting* past `timeout_seconds`, raising `_AskRequestTimedOut`
    instead of letting the calling (FastAPI request-handling) thread block
    indefinitely.

    `on_done` is called exactly once, when the submitted task actually
    finishes (success or exception) -- not when this function gives up
    waiting on it. `ask()` uses this to release the concurrency-limiter
    slots it acquired before calling this function: those slots represent
    real, still-consumed resources (a pool worker, whatever the graph
    execution itself is holding) for as long as the abandoned task keeps
    running, so releasing them early (the moment the *caller* stops
    waiting) would let a new request be admitted on top of resources the
    old one hasn't actually freed yet -- exactly the unbounded-pileup
    problem this whole change exists to prevent.

    There is still no clean cross-thread cancellation of an in-flight
    Ollama call or DB query once the task is running (same fundamental
    limitation `db.execution._execute_with_timeout`'s own "background
    thread + join" idiom has) -- what bounding the executor buys is
    described in `_get_ask_executor`'s own docstring.
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
                attachment_ids=attachment_ids,
                model=model,
                tenant_id=tenant_id,
                forecast_horizon=forecast_horizon,
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error["error"] = exc

    try:
        future = _get_ask_executor(max_workers).submit(_run)
        future.add_done_callback(lambda _f: on_done())
    except BaseException:
        # Defensive: submission itself failing (the executor singleton
        # being unusable) is not expected in practice, but `on_done` must
        # still fire exactly once so its caller's concurrency-limiter slots
        # are never leaked permanently.
        on_done()
        raise

    try:
        future.result(timeout_seconds)
    except FuturesTimeoutError:
        raise _AskRequestTimedOut(timeout_seconds) from None
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
    started_at = time.perf_counter()
    session_id = payload.session_id or str(uuid.uuid4())
    settings = get_settings()
    caller_key = _rate_limit_key(identity, request, settings)

    rate_limit_result = _limiter_for(caller_key).check()
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

    # Validated before any admission-control slot is acquired or any LLM/DB
    # work starts -- an invalid/disallowed AskRequest.model is a malformed
    # request (HTTP 400), not a reason to spend a concurrency slot or a
    # graceful "failed" AgentState the way a genuinely-uninstalled-but-
    # allowed model does (that failure surfaces naturally from the real
    # Ollama call instead -- see agent/model_registry.py's module docstring
    # for why this is a deliberate, cheap, allowlist-only check with no
    # extra Ollama round trip). Never allows an arbitrary string through to
    # `ollama.Client.chat(model=...)` unchecked.
    try:
        selected_model = validate_model_selection(payload.model, settings)
    except InvalidModelSelectionError as exc:
        # Same audit-trail convention as api.authz.require_permission's
        # "authz_denied" event -- a caller-supplied model name is not a
        # secret, so logging it plainly is what makes this investigable
        # after the fact (e.g. a client integration bug, or someone probing
        # for an unlisted model).
        log_security_event(
            "invalid_model_selection",
            "info",
            "A request specified an Ollama model outside the configured allowlist.",
            subject=identity.subject,
            requested_model=payload.model,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Prompt 16 (16_FORECASTING_CONTRACT.md): the same "validate before any
    # admission-control slot is acquired or LLM/DB work starts" posture as
    # the model-selection check just above -- an out-of-range
    # AskRequest.forecast_horizon is a malformed request (HTTP 400), not a
    # reason to spend a concurrency slot. AskRequest.forecast_horizon's own
    # `Field(gt=0)` already rejects a non-positive value at the schema
    # layer; only the configurable upper bound (Settings.forecast_max_horizon,
    # not knowable at schema-definition time) needs a runtime check here.
    if (
        payload.forecast_horizon is not None
        and payload.forecast_horizon > settings.forecast_max_horizon
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"forecast_horizon {payload.forecast_horizon} exceeds the configured "
                f"maximum of {settings.forecast_max_horizon}."
            ),
        )

    # Admission control (scale-out hardening pass, docs/SCALE_OUT_PROMPT.md
    # bottleneck #1/#4): reject fast, before any LLM/DB work starts, once
    # either the global or this caller's own concurrency budget is
    # exhausted -- see agent.rate_limit.ConcurrencyLimiter's own docstring
    # for why this is a separate control from the per-minute rate limit
    # just above. Global is checked first (cheaper to release immediately
    # if the per-caller check then fails) and both are only ever released
    # by `_release_ask_slots`, called exactly once by
    # `_run_orchestrated_with_timeout`'s done-callback -- never here on the
    # happy path, and never twice.
    global_limiter = get_ask_concurrency_limiter(settings.max_concurrent_ask_requests)
    if not global_limiter.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ASK_CONCURRENCY_LIMIT_MESSAGE,
            headers={"Retry-After": "2"},
        )
    per_caller_limiter = get_per_caller_ask_concurrency_limiter(
        caller_key, settings.max_concurrent_ask_requests_per_caller
    )
    if not per_caller_limiter.try_acquire():
        global_limiter.release()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=PER_CALLER_ASK_CONCURRENCY_LIMIT_MESSAGE,
            headers={"Retry-After": "2"},
        )

    _released = False

    def _release_ask_slots() -> None:
        nonlocal _released
        if _released:
            return
        _released = True
        per_caller_limiter.release()
        global_limiter.release()

    try:
        final_state = _run_orchestrated_with_timeout(
            payload.question,
            conversation_history,
            enable_insight=payload.enable_insight,
            session_id=session_id,
            caller_roles=identity.roles,
            caller_subject=real_caller_subject(identity),
            timeout_seconds=settings.request_timeout_seconds,
            max_workers=settings.max_concurrent_ask_requests,
            on_done=_release_ask_slots,
            attachment_ids=payload.attachment_ids,
            model=selected_model,
            tenant_id=resolve_tenant_id_for_identity(identity),
            forecast_horizon=payload.forecast_horizon,
        )
    except _AskRequestTimedOut:
        # See Settings.request_timeout_seconds's docstring: this is "stop
        # waiting," not true cancellation -- the abandoned background thread
        # keeps running and its result is discarded, but the caller gets a
        # timely response instead of an indefinitely blocked request. Note
        # the concurrency slots acquired above are NOT released here --
        # `_release_ask_slots` only runs once the abandoned work actually
        # finishes (see `_run_orchestrated_with_timeout`'s own docstring for
        # why releasing early here would defeat the whole point of bounding
        # concurrency in the first place).
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

    # Built once, with conversation_id/message_id still unset, so
    # `persist_ask_turn` can persist exactly what this response already
    # contains (redacted, bounded) -- see api/chat_persistence.py's own
    # docstring for why persistence is built from this already-computed
    # response rather than re-deriving anything from raw `final_state`.
    ask_response = _ask_response_from_state(final_state, session_id)
    ask_response = ask_response.model_copy(
        update={"answer_duration_ms": (time.perf_counter() - started_at) * 1000}
    )

    persisted_conversation_id: str | None = None
    persisted_message_id: str | None = None
    try:
        persisted_conversation_id, persisted_message_id = persist_ask_turn(
            identity=identity,
            conversation_id=payload.conversation_id,
            question=payload.question,
            ask_response=ask_response,
            attachment_ids=payload.attachment_ids,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - see api/chat_persistence.py's own docstring
        logger.warning(
            "[api] /ask: chat-history persistence failed unexpectedly (session_id=%s): %s",
            session_id,
            exc,
        )

    try:
        persist_ask_recommendations(
            identity=identity,
            question=payload.question,
            ask_response=ask_response,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - see api/recommendation_persistence.py's own docstring
        logger.warning(
            "[api] /ask: recommendation-governance persistence failed unexpectedly "
            "(session_id=%s): %s",
            session_id,
            exc,
        )

    return ask_response.model_copy(
        update={"conversation_id": persisted_conversation_id, "message_id": persisted_message_id}
    )


@app.post("/execute", response_model=ExecuteResponse)
def execute(
    payload: ExecuteRequest,
    request: Request,
    response: Response,
    identity: AuthIdentity = Depends(require_permission(Permission.EXECUTE_SQL)),
) -> ExecuteResponse:
    """Validates and executes a specific SQL string read-only -- the exact
    `validate_sql` -> `enforce_row_limit` -> `qualify_table_schema` ->
    `execute_readonly_sql` sequence the React dashboard's "Confirm and Run"
    button calls this route to run. This is the "SQL is untrusted output,
    always" rule applied at this layer too: `sql` is always re-validated
    and re-executed exactly as supplied, never
    trusted because it happens to look like something `/ask` returned.

    Prompt 22 (scale/performance hardening): may serve a cached result
    (`X-Cache: HIT`/`MISS` response header, `db.result_cache`) and is
    protected by a per-database execution concurrency limiter
    (`agent.rate_limit.get_database_execution_limiter`) -- see this
    function's body and `ExecuteResponse`'s own docstring is unchanged,
    since neither feature alters the response schema.

    Rate-limited per client IP (`enforce_api_action_rate_limit`) -- unlike
    `/ask`, this route previously had no rate limit of its own at all.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "execute", settings, identity=identity)
    # Prompt 20: resolved against the connections *this caller's tenant* may
    # query, not the raw `settings.databases` tuple. A tenant-restricted
    # connection a different tenant asks for is reported as 404 "unknown
    # database" -- the same response a genuinely nonexistent name gets, so
    # the route never confirms another tenant's database even exists
    # (anti-enumeration, matching `api/semantic_catalog.py`'s precedent).
    tenant_id = resolve_tenant_id_for_identity(identity) or DEFAULT_TENANT_ID
    visible_databases = settings.databases_for_tenant(tenant_id)
    if not visible_databases:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No database configured."
        )
    database_name = payload.database or visible_databases[0].name
    db_config = next((db for db in visible_databases if db.name == database_name), None)
    if db_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown database {database_name!r}."
        )

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

    # Prompt 22 (scale/performance hardening): an opt-in, short-TTL result
    # cache (see db/result_cache.py's module docstring for the full
    # design/scope) -- never for SQL referencing a restricted column, and
    # computed once up front so the lookup and the later store decision
    # (below) always agree.
    metrics = get_default_metrics()
    # `tenant_id` (resolved above, server-side from the verified identity --
    # never from the request) also partitions the cache: two tenants running
    # byte-identical SQL against the same shared database must never see
    # each other's rows, and this cache sits after every authorization gate,
    # so it cannot rely on one.
    cache_enabled = settings.enable_result_cache
    cacheable = cache_enabled and is_cacheable_sql(safe_sql, dialect)
    cached = (
        get_result_cache(settings.result_cache_max_entries).get(
            tenant_id, database_name, execution_sql, settings.result_cache_ttl_seconds
        )
        if cacheable
        else None
    )

    cache_status: Literal["hit", "miss"] | None = None
    if cached is not None:
        columns, rows = cached
        duration_ms = 0.0  # no DB round trip happened; this is an honest zero, not a measurement
        response.headers["X-Cache"] = "HIT"
        cache_status = "hit"
        metrics.record_result_cache_hit(tenant_id)
    else:
        # Prompt 22: fast-fail once this database's own connection pool is
        # already fully occupied, rather than blocking on `QueuePool`
        # checkout for up to SQLAlchemy's own `pool_timeout` -- see
        # agent.rate_limit.get_database_execution_limiter's docstring.
        database_limiter = None
        if settings.enable_database_concurrency_limit:
            max_concurrent = (
                settings.db_pool_size
                + settings.db_max_overflow
                + settings.database_concurrency_limit_overhead
            )
            database_limiter = get_database_execution_limiter(database_name, max_concurrent)
            if not database_limiter.try_acquire():
                metrics.record_database_concurrency_rejection(tenant_id)
                logger.warning(
                    "[execute] rejected -- database %r execution concurrency limit reached",
                    database_name,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        "This database is handling too many queries right now -- please "
                        "try again shortly."
                    ),
                    headers={"Retry-After": "2"},
                )

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
        finally:
            if database_limiter is not None:
                database_limiter.release()

        if cache_enabled:
            response.headers["X-Cache"] = "MISS"
            cache_status = "miss"
            metrics.record_result_cache_miss(tenant_id)
            if cacheable:
                get_result_cache(settings.result_cache_max_entries).store(
                    tenant_id, database_name, execution_sql, columns, rows
                )

    # Result-level governance (governance/result_policy.py), applied per caller
    # AFTER the result cache: the cache holds raw rows, so a cache hit is masked
    # for this caller's own roles exactly as a fresh execution would be.
    if settings.enable_result_governance:
        rows = govern_rows_for_caller(columns, rows, identity.roles)
    result_df = pd.DataFrame(rows, columns=columns)
    column_types: dict[str, str] = dict(classify_columns(result_df)) if not result_df.empty else {}
    recommendation = recommend_chart(result_df, column_types)

    # Prompt 15: a fuller, deterministic visualization spec -- additive and
    # parallel to chart_recommendation/column_types above, which remain the
    # frontend's actual chart-picker seed (see analytics.visualization's own
    # "must not regress" docstring). Fails open exactly like
    # agent.nodes.compute_analytics_node's identical try/except -- an
    # accuracy aid must never block a successful "Confirm and Run".
    visualization_spec_out: VisualizationSpecOut | None = None
    try:
        analytics_result = compute_analytics_result(columns, rows, settings) if rows else None
        chart_spec = build_chart_spec(
            columns, rows, column_types, analytics_result=analytics_result, settings=settings
        )
        if chart_spec is not None:
            visualization_spec_out = VisualizationSpecOut(
                chart_type=chart_spec.chart_type.value,
                title=chart_spec.title,
                fields=tuple(
                    ChartFieldOut(
                        column=f.column,
                        role=f.role.value,
                        aggregation=f.aggregation,
                        format=f.format,
                    )
                    for f in chart_spec.fields
                ),
                sort=(
                    ChartSortOut(column=chart_spec.sort.column, direction=chart_spec.sort.direction)
                    if chart_spec.sort is not None
                    else None
                ),
                limit=chart_spec.limit,
                is_downsampled=chart_spec.is_downsampled,
                notices=chart_spec.notices,
                accessibility=AccessibilityMetadataOut(
                    alt_text=chart_spec.accessibility.alt_text,
                    summary=chart_spec.accessibility.summary,
                ),
                reason=chart_spec.reason,
                engine_version=chart_spec.engine_version,
            )
    except Exception as exc:  # noqa: BLE001 - an accuracy aid must never block the run
        logger.warning(
            "[execute] visualization spec computation failed, proceeding without: %s", exc
        )
        visualization_spec_out = None

    execute_response = ExecuteResponse(
        status="succeeded",
        database=database_name,
        normalized_sql=safe_sql,
        result_columns=columns,
        result_rows=_rows_to_json(rows),
        row_count=len(rows),
        duration_ms=duration_ms,
        column_types=column_types,
        chart_recommendation=(
            ChartRecommendationOut(**recommendation) if recommendation is not None else None
        ),
        visualization_spec=visualization_spec_out,
        # Best-effort signal, not a real "is there more data" answer -- see
        # ExecuteResponse.truncated's own docstring for why this app never
        # runs a separate COUNT(*) query to know the true total.
        truncated=len(rows) >= settings.max_result_rows,
        cache_status=cache_status,
    )

    if payload.message_id:
        # Best-effort: attaches this confirmed result to the turn it came
        # from (see api/chat_persistence.py::persist_execute_result) so
        # reopening the conversation later shows it without re-running
        # anything -- never a reason a successful "Confirm and Run" fails.
        try:
            persist_execute_result(
                identity=identity,
                conversation_id=payload.conversation_id,
                message_id=payload.message_id,
                sql=payload.sql,
                execute_response=execute_response,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 - see api/chat_persistence.py's own docstring
            logger.warning(
                "[api] /execute: chat-history result persistence failed unexpectedly "
                "(message_id=%s): %s",
                payload.message_id,
                exc,
            )

    return execute_response


@app.post(
    "/feedback/golden-example",
    response_model=GoldenExampleFeedbackResponse,
)
def feedback_golden_example(
    payload: GoldenExampleFeedbackRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.GOLDEN_EXAMPLE_WRITE)),
) -> GoldenExampleFeedbackResponse:
    """Records a human-approved (question, SQL) pair for future few-shot
    retrieval -- the same `embeddings.golden_examples.save_golden_example`
    call the dashboard's thumbs-up widget makes when a user confirms a result.
    `sql` should be the exact SQL that was actually run and confirmed
    correct (e.g. from a prior `/execute` call), not necessarily the
    original `/ask` draft if the caller edited it.

    Prompt 21 (enterprise security & data governance hardening): rate-limited
    per caller (`enforce_api_action_rate_limit`) -- previously, this and
    `/feedback/message` below were the only mutating, store-writing routes
    in this file with no rate limit at all, which made both floodable as a
    data-poisoning vector against the golden-example/feedback stores.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "feedback_golden_example", settings, identity=identity)
    # Prompt 20: stamped with the saving caller's own tenant, resolved
    # server-side -- the pair is only ever retrieved back for that tenant.
    save_golden_example(
        payload.question,
        payload.sql,
        payload.database,
        settings,
        tenant_id=resolve_tenant_id_for_identity(identity) or DEFAULT_TENANT_ID,
    )
    return GoldenExampleFeedbackResponse(saved=True)


@app.post(
    "/feedback/message",
    response_model=MessageFeedbackResponse,
)
def feedback_message(
    payload: MessageFeedbackRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.GOLDEN_EXAMPLE_WRITE)),
) -> MessageFeedbackResponse:
    """Records a like/dislike (plus an optional free-text comment) on any
    assistant answer -- SQL, document/policy RAG, web search, or media --
    not just a confirmed-and-executed SQL result the way `/feedback/
    golden-example` above is scoped. Reuses `Permission.GOLDEN_EXAMPLE_WRITE`
    rather than introducing a new RBAC permission: both endpoints are the
    same underlying capability ("this caller may submit feedback that
    shapes future answers"), just against two different stores
    (`embeddings.golden_examples`, few-shot prompt examples, confirmed SQL
    only; `feedback.store` here, every answer). A thumbs-up on a confirmed
    SQL answer still separately calls `/feedback/golden-example` too --
    this endpoint is purely additive, never a replacement.

    Prompt 21: rate-limited per caller, same reasoning as `/feedback/
    golden-example` above.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "feedback_message", settings, identity=identity)
    save_response_feedback(
        payload.question,
        payload.answer,
        payload.rating,
        sql=payload.sql,
        database=payload.database,
        sources_used=payload.sources_used,
        comment=payload.comment,
        conversation_id=payload.conversation_id,
        settings=settings,
        # Prompt 20: stamped with the submitting caller's own tenant, resolved
        # server-side, so this shared log stays partitionable per tenant when
        # it is read back.
        tenant_id=resolve_tenant_id_for_identity(identity) or DEFAULT_TENANT_ID,
    )
    return MessageFeedbackResponse(saved=True)


@app.post("/schema/refresh", response_model=SchemaRefreshResponse)
def schema_refresh(
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.SCHEMA_REFRESH)),
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

    Each result also reports (Prompt 06, `06_DATABASE_DISCOVERY_CONTRACT
    .md`) what actually changed for that database this call -- read back
    via `get_last_discovery_diff`, which never re-derives anything
    `refresh_all_schema_indexes`/`build_index` didn't already compute and
    persist. A database that was skipped (schema unchanged) still reports
    its most recent diff, which is correctly empty in that case.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "schema_refresh", settings, identity=identity)
    results = refresh_all_schema_indexes(settings)
    databases = []
    for db_name, tables in results.items():
        diff = get_last_discovery_diff(db_name, settings)
        databases.append(
            SchemaRefreshResult(
                database=db_name,
                table_count=len(tables),
                view_count=sum(1 for table in tables if table.is_view),
                added_tables=list(diff.added_tables) if diff else [],
                removed_tables=list(diff.removed_tables) if diff else [],
                changed_tables=list(diff.changed_tables) if diff else [],
                last_discovered_at=diff.last_discovered_at if diff else None,
            )
        )
    return SchemaRefreshResponse(databases=databases)


@app.get("/metrics/performance", response_model=PerformanceMetricsResponse)
def metrics_performance(
    identity: AuthIdentity = Depends(require_permission(Permission.ADMIN_CONFIG)),
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
    # Prompt 20: scoped to the caller's own tenant, not the whole process.
    # This route is admin-gated, but this codebase has no platform-admin
    # versus tenant-admin distinction, so one tenant's admin must not be
    # able to read another tenant's latency/status/cache numbers. The merged
    # process-wide view remains available to an operator calling
    # `PerformanceMetrics.snapshot()` directly, never over HTTP.
    snapshot = get_default_metrics().snapshot(
        resolve_tenant_id_for_identity(identity) or DEFAULT_TENANT_ID
    )
    return PerformanceMetricsResponse(
        started_at=snapshot["started_at"],
        window_requests=snapshot["window_requests"],
        max_window_requests=snapshot["max_window_requests"],
        requests=RequestMetricOut(**snapshot["requests"]),
        stages=[StageMetricOut(**stage) for stage in snapshot["stages"]],
        status_counts=snapshot["status_counts"],
        tenant_id=snapshot["tenant_id"],
        result_cache_hits=snapshot["result_cache_hits"],
        result_cache_misses=snapshot["result_cache_misses"],
        database_concurrency_rejections=snapshot["database_concurrency_rejections"],
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
    # Prompt 20: schema *metadata* is tenant-scoped too -- table and column
    # names are configuration a tenant should not learn about another
    # tenant's database, and this route is the one place they are listed
    # wholesale. Same 404-for-cross-tenant convention as `/execute`.
    tenant_id = resolve_tenant_id_for_identity(_identity) or DEFAULT_TENANT_ID
    visible_databases = settings.databases_for_tenant(tenant_id)
    if database is not None and database not in {config.name for config in visible_databases}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown database {database!r}."
        )

    configs = (
        [config for config in visible_databases if config.name == database]
        if database is not None
        else list(visible_databases)
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


@app.get("/models", response_model=ModelsResponse)
def models(
    _identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> ModelsResponse:
    """The Ollama Text-to-SQL model registry -- every model
    `Settings.ollama_allowed_models` configures, enriched with a live
    "is it actually installed right now" lookup against the connected
    Ollama instance (see `agent.model_registry.build_model_options`).

    Same `Permission.ASK` gate as `/schema/tables` above -- seeing which
    models exist is the same capability level as asking a question, not a
    separate admin concern. Never exposes `OLLAMA_HOST`, secrets, or any
    other environment/connection detail -- only model ids and this
    application's own hand-authored display metadata
    (`config/ollama_models.yaml`).

    Deliberately the only place this application calls Ollama's `list()`
    API for model-*selection* purposes (`GET /health` also calls it, but
    only for its own, narrower vision-model-availability check) -- the
    frontend is expected to cache this response (`useAvailableModels`,
    `staleTime` matching `useHealth`'s own 30s) rather than the backend
    re-querying Ollama on every `/ask`. See `agent/model_registry.py`'s
    module docstring for the full online-catalog/configured/installed/
    selectable distinction this response embodies.
    """
    settings = get_settings()
    options = build_model_options(settings)
    return ModelsResponse(
        default_model=settings.ollama_model,
        selection_enabled=settings.ollama_model_selection_enabled,
        models=[
            ModelOut(
                id=option.id,
                display_name=option.display_name,
                is_default=option.is_default,
                installed=option.installed,
                available=option.available,
                recommended=option.recommended,
                parameter_size=option.parameter_size,
                context_length=option.context_length,
                resource_level=option.resource_level,
                capabilities=option.capabilities,
                description=option.description,
            )
            for option in options
        ],
    )


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
