"""Central application settings.

Every configurable path, model name, and limit used anywhere in the project
is defined here and nowhere else, sourced from environment variables (loaded
from a local `.env` file via python-dotenv if present). Import `get_settings()`
rather than reading `os.environ` directly elsewhere in the codebase.

Database connectivity is fully config-driven (see the `db_*` fields below) --
there is no hardcoded connection string or sample schema anywhere in the
project. `db/connection.py` is the only other module allowed to interpret
these `db_*` fields into an actual SQLAlchemy engine/URL.

Built on `pydantic_settings.BaseSettings`, not a hand-rolled `@dataclass` --
this is what gives every field below automatic environment-variable
loading, type coercion (a `.env` value is always a string; pydantic parses
it into the field's real type), and validation (`Field(gt=0)`, `Literal`
types, and the two `model_validator`s below) essentially for free, instead
of the ~80 lines of `_env_int`/`_env_bool`/manual-validation-loop
boilerplate this file used before. See `Settings.__init__`'s docstring for
the one deliberate seam this migration preserves: every construction
failure -- whether from Pydantic's own type coercion or this file's
business-rule validators -- still raises `ConfigurationError`, the same
type this codebase has always caught, not a raw `pydantic.ValidationError`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Project root is the parent of this file's parent (config/settings.py -> repo root).
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Load .env once at import time. Safe to call even if .env doesn't exist yet
# (e.g. a fresh checkout that hasn't run `Copy-Item .env.example .env`).
# Kept as an explicit python-dotenv call (rather than pydantic-settings'
# own `env_file=` support) because `_parse_named_connections` below reads
# `os.environ` directly for dynamically-prefixed `DB_<NAME>_*` vars that
# can't be declared as static Pydantic fields -- both that function and
# every `Settings` field need the same populated `os.environ` regardless
# of which one actually consumes it.
load_dotenv(PROJECT_ROOT / ".env")


class ConfigurationError(Exception):
    """Raised when a config value is present but malformed, or a business
    rule between two values is violated.

    Deliberately distinct from a missing value (which callers may have a
    sensible default for): this means "the user set something, and it's
    wrong" -- e.g. DB_PORT=notanumber, or COST_MODERATE_ROW_THRESHOLD set
    higher than COST_HIGH_ROW_THRESHOLD -- which should fail fast and
    loudly rather than silently falling back to a default or producing a
    security control that doesn't actually do what its name says.

    Raised for *every* `Settings`/`DatabaseConnectionConfig` construction
    failure, including ones Pydantic's own type system catches (a
    non-numeric `MAX_RETRIES`) -- see `Settings.__init__`'s docstring for
    how a raw `pydantic.ValidationError` gets translated into this type
    rather than leaking Pydantic's own exception type to callers that have
    always caught `ConfigurationError` specifically (`db/connection.py`,
    `scripts/test_db_connection.py`, `scripts/integration_test.py`, and
    this module's own `RagStoreNotConfiguredError`/
    `WebSearchNotConfiguredError` subclasses in `rag/store.py`/
    `search/web_search.py`).
    """


def _env_str(name: str, default: str) -> str:
    """Read a string environment variable, falling back to `default`.

    Only used by `_parse_named_connections` below, for dynamically-
    prefixed `DB_<NAME>_*` vars that can't be declared as static Pydantic
    fields -- every *statically* named field on `Settings` itself gets
    this behavior automatically from `pydantic_settings.BaseSettings`
    (see `Settings.model_config`'s `env_ignore_empty=True`).
    """
    return os.environ.get(name, default)


def _env_optional_str(name: str) -> str | None:
    """Read an optional string environment variable; None if unset/blank.

    Same "dynamic prefix only" scope as `_env_str` above.
    """
    raw = os.environ.get(name)
    return raw if raw and raw.strip() else None


def _env_optional_int_strict(name: str) -> int | None:
    """Read an optional integer environment variable for a dynamically-
    prefixed `DB_<NAME>_PORT` var (see `_env_str` above for why this can't
    just be a Pydantic field).

    A *present but invalid* value (e.g. `DB_SALES_PORT=abc`) raises
    `ConfigurationError` immediately, since that's a real mistake in
    `.env`, not an intentional "use the default" signal. Absent/blank is
    fine and returns None (caller decides the fallback, e.g. a per-
    DB_TYPE default port).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name}='{raw}' is not a valid integer. Fix it in .env.") from exc


def _resolve_path(raw: str) -> Path:
    """Resolve a possibly-relative path from .env against the project root."""
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


# A small, practical starter set for OLLAMA_ALLOWED_MODELS when it's left
# unset -- NOT a claim of "every model in the Ollama Library," and
# deliberately not the full online catalog (see agent/model_registry.py's
# module docstring for why hard-coding that would be wrong). Evaluated
# against the live https://ollama.com/library on 2026-09-27 for local
# Text-to-SQL suitability (instruction-following, structured/JSON output,
# a spread of sizes from lightweight to more capable) -- see
# docs/CONFIGURATION.md's "Model selection" section for the full writeup.
# `ollama_model` (the configured default) is always unioned in on top of
# this by `_fill_default_ollama_allowed_models` below, regardless of
# whether it's listed here.
_DEFAULT_OLLAMA_ALLOWED_MODELS: tuple[str, ...] = (
    "llama3.1:8b",
    "qwen2.5:7b",
    "qwen2.5:14b",
    "llama3.2:3b",
    "mistral:7b",
    "deepseek-r1:8b",
)


def _connection_env_prefix(name: str) -> str:
    """Maps a connection name (from `DB_CONNECTIONS`) to its `DB_<PREFIX>_*` env prefix.

    E.g. "sales" -> "SALES", "us-east 1" -> "US_EAST_1" -- any character
    that isn't alphanumeric becomes `_`, uppercased, so a human-friendly
    connection name always has a well-formed env var prefix.
    """
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


class DatabaseConnectionConfig(BaseModel):
    """One named database connection's worth of `DB_*`-shaped fields.

    Mirrors `Settings`' own `db_*` fields exactly (same names, same
    meanings) so both types satisfy `db.connection.DbConnectionLike` and
    every connection-building function there (`build_connection_url`,
    `get_engine`, `test_connection`, ...) works identically whether it's
    handed the legacy single global `Settings` or one entry from
    `Settings.databases`.

    See `config/settings.py`'s module docstring and `Settings.databases`
    for how these are parsed from `.env` (`DB_CONNECTIONS` + per-name
    `DB_<NAME>_*` vars). A plain `BaseModel`, not a `BaseSettings` --
    unlike `Settings`, this is never constructed by reading the process
    environment directly; `_parse_named_connections` below does that
    reading itself (dynamic `DB_<NAME>_*` prefixes aren't expressible as
    static Pydantic fields) and passes already-resolved values in.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    db_type: str
    db_host: str | None = None
    db_port: int | None = None
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_connection_string: SecretStr | None = None
    db_schema: str | None = None
    db_odbc_driver: str = "ODBC Driver 17 for SQL Server"


def _parse_named_connections() -> tuple[DatabaseConnectionConfig, ...]:
    """Parses `DB_CONNECTIONS` + per-name `DB_<NAME>_*` vars into named connections.

    `DB_CONNECTIONS` is a comma-separated list of connection names, e.g.
    `DB_CONNECTIONS=sales,hr,inventory`. Each name `X` then reads
    `DB_<PREFIX>_TYPE`, `DB_<PREFIX>_HOST`, `DB_<PREFIX>_PORT`,
    `DB_<PREFIX>_NAME`, `DB_<PREFIX>_USER`, `DB_<PREFIX>_PASSWORD`,
    `DB_<PREFIX>_CONNECTION_STRING`, `DB_<PREFIX>_SCHEMA`,
    `DB_<PREFIX>_ODBC_DRIVER` (see `_connection_env_prefix` for how `X`
    becomes `<PREFIX>`) -- the exact same field set as the legacy flat
    `DB_*` vars, just namespaced per connection.

    Returns an empty tuple if `DB_CONNECTIONS` is unset/blank -- the
    caller (`get_settings()`, via `Settings`' `_fill_default_database`
    validator) falls back to a single "default" connection built from the
    legacy flat `DB_*` vars in that case, so an existing single-database
    `.env` needs zero changes. Deliberately hand-written rather than
    Pydantic fields: `DB_CONNECTIONS` names an *open-ended* set of env-var
    prefixes only known at runtime, which static field declarations can't
    express -- this is the one part of config loading Pydantic's own
    settings-from-env machinery isn't the right tool for.

    Raises:
        ConfigurationError: on a blank name, a duplicate name (or two
            names colliding on the same env prefix), or a listed name with
            no matching `DB_<PREFIX>_TYPE` set.
    """
    raw_names = _env_str("DB_CONNECTIONS", "")
    names = [n.strip() for n in raw_names.split(",") if n.strip()]
    if not names:
        return ()

    seen_prefixes: dict[str, str] = {}
    connections: list[DatabaseConnectionConfig] = []
    for name in names:
        prefix = _connection_env_prefix(name)
        if prefix in seen_prefixes:
            raise ConfigurationError(
                f"DB_CONNECTIONS names {seen_prefixes[prefix]!r} and {name!r} both map to "
                f"the same env prefix DB_{prefix}_* -- use more distinct connection names."
            )
        seen_prefixes[prefix] = name

        db_type = _env_str(f"DB_{prefix}_TYPE", "").strip().lower()
        if not db_type:
            raise ConfigurationError(
                f"DB_CONNECTIONS includes {name!r} but DB_{prefix}_TYPE is not set in .env. "
                f"Every name listed in DB_CONNECTIONS needs its own DB_<NAME>_TYPE (and the "
                f"other DB_<NAME>_* fields)."
            )

        raw_password = _env_optional_str(f"DB_{prefix}_PASSWORD")
        raw_connection_string = _env_optional_str(f"DB_{prefix}_CONNECTION_STRING")
        connections.append(
            DatabaseConnectionConfig(
                name=name,
                db_type=db_type,
                db_host=_env_optional_str(f"DB_{prefix}_HOST"),
                db_port=_env_optional_int_strict(f"DB_{prefix}_PORT"),
                db_name=_env_optional_str(f"DB_{prefix}_NAME"),
                db_user=_env_optional_str(f"DB_{prefix}_USER"),
                db_password=SecretStr(raw_password) if raw_password is not None else None,
                db_connection_string=(
                    SecretStr(raw_connection_string) if raw_connection_string is not None else None
                ),
                db_schema=_env_optional_str(f"DB_{prefix}_SCHEMA"),
                db_odbc_driver=_env_str(
                    f"DB_{prefix}_ODBC_DRIVER", "ODBC Driver 17 for SQL Server"
                ),
            )
        )

    return tuple(connections)


def _format_validation_error(exc: ValidationError) -> str:
    """Turns a `pydantic.ValidationError` into one `ConfigurationError`-style message.

    Field names are upper-cased to match this project's env-var naming
    convention (`log_redaction_level` -> `LOG_REDACTION_LEVEL`) -- every
    field on `Settings` is named identically to its env var, just
    lower-cased, so this mapping is exact, not a guess.
    """
    parts = []
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error["loc"]).upper()
        parts.append(f"{field}: {error['msg']} (got {error['input']!r}).")
    return " ".join(parts) + " Fix it in .env (or remove it to use the default)."


class Settings(BaseSettings):
    """Immutable snapshot of application configuration, loaded from the process
    environment (and `.env`, via the `load_dotenv()` call at module import
    time above).

    Attributes:
        ollama_host: Base URL of the local Ollama server.
        ollama_model: Name of the Ollama model to use for SQL generation.
            Swap this (via OLLAMA_MODEL in .env) to try sqlcoder, duckdb-nsql, etc.
        ollama_request_timeout_seconds: Per-request timeout for calls to Ollama.
            Defaults to 300s -- local models on modest hardware (no GPU, a
            large context from schema retrieval, etc.) can take well over
            the old 60s default to finish a single generation, which
            surfaced as httpx.ReadTimeout/ConnectTimeout errors bubbling up
            as ollama.ResponseError in agent/llm_client.py and rag/llm.py.
            Override via OLLAMA_REQUEST_TIMEOUT_SECONDS in .env.
        ollama_model_selection_enabled: Whether a caller may override
            `ollama_model` on a per-question basis (`AskRequest.model`, the
            dashboard's "AI Model" picker). True by default -- selection is
            opt-in *per request* (omitting `model` always uses `ollama_model`
            unchanged, so this defaults on with zero behavior change for any
            existing caller). Set to `false` to hard-disable the feature
            entirely (e.g. a locked-down deployment) -- when off,
            `agent.model_registry.get_allowed_models` collapses to exactly
            `(ollama_model,)` regardless of `ollama_allowed_models` below, so
            a caller-supplied `model` other than the configured default is
            rejected the same way an unconfigured one always is. See
            `agent/model_registry.py`.
        ollama_allowed_models: Comma-separated list of additional Ollama
            model names selectable for Text-to-SQL generation, e.g.
            `OLLAMA_ALLOWED_MODELS=qwen2.5:7b,llama3.2:3b,mistral:7b`
            (mirrors `DB_CONNECTIONS`'/`CORS_ALLOWED_ORIGINS`' own
            comma-separated-string convention -- see `_split_ollama_allowed_models`
            below). `ollama_model` is always implicitly included even if
            omitted here, so the configured default is never made
            unselectable by an operator's own `.env` edit. Leaving this
            unset does NOT mean "only the default model" -- it falls back to
            a small, practical starter set (`_DEFAULT_OLLAMA_ALLOWED_MODELS`
            below, evaluated against the live Ollama Library on 2026-09-27 --
            see `docs/CONFIGURATION.md`'s "Model selection" section) so a
            fresh clone shows a useful picker with
            no `.env` edit required. Every listed model still shows as
            "not installed" in the UI until it's actually been `ollama
            pull`ed -- this setting only controls what's *offered*, never
            what's *installed* (see `agent.model_registry.discover_installed_models`).
            None of this is a permanent, exhaustive list of "every Ollama
            model" -- see that module's own docstring for why hard-coding
            the whole online catalog was deliberately avoided.
        db_type: Target database engine, e.g. "postgresql", "mssql", "mysql",
            "oracle". Interpreted by `db/connection.py` -- see
            `db.connection.SUPPORTED_DB_TYPES` for the full list.
        db_host: Database host/server name.
        db_port: Database port. None means "use the DB_TYPE's default port".
        db_name: Database (catalog) name.
        db_user: Database login username. Should be a dedicated read-only
            account -- see README's "Security" section.
        db_password: Database login password. Never logged. A
            `pydantic.SecretStr` (coerced automatically by Pydantic
            regardless of whether the caller passed a plain `str` or an
            already-wrapped value) -- `str()`/`repr()` both mask it; only
            `.get_secret_value()` returns the real value, so an accidental
            `logger.debug("%r", settings)`, a traceback's local-variable
            dump, or a naive `settings.model_dump()` can't leak it.
        db_connection_string: Optional full SQLAlchemy connection string,
            used as-is instead of building one from the discrete db_* fields
            above if set. Also a `SecretStr` -- it may itself embed a password.
        db_schema: Optional schema name to restrict introspection to (so
            only that schema's tables are exposed to the LLM). None means
            "use the database's default schema".
        db_odbc_driver: ODBC driver name for DB_TYPE=mssql, e.g.
            "ODBC Driver 17 for SQL Server". Ignored for other DB_TYPEs.
        db_pool_size: `QueuePool`'s `pool_size` for every configured
            database's engine (`db/connection.py::_cached_engine`). 2026
            Phase 3 performance review: previously left on SQLAlchemy's
            own default (5) -- fine for this app's original single-user
            target, a real concurrency ceiling once more than a handful of
            callers query the same database at once (each held connection
            is one `execute_sql_node`/`POST /execute` call in flight).
            Mirrors `moderation_store_pool_size`'s existing, already-
            explicit pattern below, now applied consistently to the
            primary database engine too.
        db_max_overflow: `QueuePool`'s `max_overflow` for the same engine
            -- the burst ceiling above `db_pool_size` before a caller
            waits for a connection, same meaning as
            `moderation_store_max_overflow`.
        chroma_persist_dir: Directory where ChromaDB persists its index.
        chroma_collection_name: Name of the Chroma collection holding schema DDL.
        embedding_model_name: sentence-transformers model used for embeddings.
        schema_top_k: Number of most-relevant tables to retrieve per question.
        max_retries: Max self-correction retries in the LangGraph agent loop.
        complex_query_max_retry_bonus: Extra retries granted on top of
            `max_retries`, for questions `agent.complexity.
            detect_complexity_signals` judges likely to need more than one
            or two self-correction cycles (e.g. "top N per group" phrasing,
            year-over-year/period-over-period comparisons, several distinct
            metrics requested at once) -- one extra retry per distinct
            signal matched, capped at this value. Zero disables the
            adaptive budget entirely (every question gets exactly
            `max_retries`, the pre-existing behavior).
        max_result_rows: Row cap applied to every executed query.
        query_timeout_seconds: Wall-clock timeout for query execution.
        request_timeout_seconds: Wall-clock ceiling on one whole `POST /ask`
            request (the entire agent graph -- schema retrieval through every
            self-correction retry through insight generation), enforced by
            `api/main.py`'s own thread+join wrapper around `run_orchestrated`
            (the same "run on a background thread, give up waiting past the
            deadline" idiom `db.execution._execute_with_timeout` already uses
            for query execution -- see that function's docstring). 2026 Phase
            3 reliability fix: before this existed, nothing bounded how long
            a single request could occupy a FastAPI worker thread -- this
            project's own Phase 3 benchmark run measured one real case that
            took ~5,502 seconds (~92 minutes, almost certainly abnormal
            shared-machine contention, not steady-state behavior -- see
            `docs/PERFORMANCE_RESULTS.md`) with nothing to cut it off. The
            default (600s / 10 minutes) is chosen from that same benchmark's
            *normal* distribution, not the outlier: it comfortably covers the
            Phase 2 baseline's P99 (~440s) while still bounding the
            pathological case. Note this is a "stop waiting" timeout, not a
            true cancellation -- Ollama has no clean way to abort an in-flight
            generation call, so the abandoned background thread still runs to
            completion and its (now-unread) result is simply discarded; what
            this buys is a timely response to the caller and a freed-up
            request-handling thread, not reduced backend load from the
            abandoned call itself. Deliberately scoped to the API layer only
            -- `agent.graph.run_agent`/`run_orchestrated` themselves stay
            unbounded by this setting so `eval/runner.py`'s benchmark
            continues measuring real, untruncated completion times.
        llm_max_tokens: Max tokens the LLM may generate per call (sandboxing).
        insight_max_tokens: Max tokens the LLM may generate for the post-query
            plain-English insight sentence (see `agent.llm_client.
            generate_insight_from_llm`) -- deliberately small and separate
            from `llm_max_tokens`, since this is 1-2 sentences, not SQL.
        max_question_length: Maximum accepted raw length (characters) of a
            user's typed question, enforced by `agent.input_guard.
            check_input` before any normalization or LLM call -- see
            CLAUDE.md's adversarial-input-hardening notes.
        max_conversation_history_turns: Maximum number of prior turns from a
            caller-supplied `conversation_history` that are ever processed,
            enforced by `agent.input_guard.sanitize_conversation_history`
            (the oldest turns beyond this count are dropped, keeping only
            the most recent ones). 2026 Phase 3 resource-governance finding:
            `api.schemas.AskRequest.conversation_history` has no
            schema-level length cap (matching `question`'s own
            deliberately-downstream-enforced convention -- see that field's
            docstring), and only the single most recent entry is ever
            functionally read (`agent.nodes.retrieve_schema_node`'s
            follow-up resolution uses `conversation_history[-1]`;
            `classify_followup_node` only checks whether the list is
            non-empty) -- every entry beyond that was being fully
            normalized and regex-scanned for injection patterns
            (`sanitize_conversation_history`) for zero functional benefit,
            a real, unbounded-by-anything CPU/memory cost a caller could
            trivially inflate by sending an arbitrarily long array. The
            default (20) is deliberately generous relative to the "only the
            last entry matters" reality above -- it exists to bound the
            worst case, not to constrain any real conversation.
        question_rate_limit_per_minute: Max question submissions per minute,
            per client IP -- see `agent.rate_limit`. A basic, in-memory
            safeguard for local/single-user use, not a multi-tenant rate
            limiter (see SECURITY.md).
        llm_call_rate_limit_per_minute: Max LLM *generation* calls per
            minute, process-wide -- deliberately stricter than and separate
            from `question_rate_limit_per_minute`, since a single
            question's self-correction retries (up to `max_retries + 1`
            calls) could otherwise multiply load well past what the
            question-level limit alone would suggest.
        max_concurrent_ask_requests: Max `POST /ask` graph executions
            allowed to run at once, process-wide -- an *in-flight*
            concurrency cap, not a per-minute rate limit (see
            `agent.rate_limit.ConcurrencyLimiter`'s own docstring for why
            the two are complementary). This is also what sizes
            `api/main.py`'s bounded ask-worker thread pool (one worker per
            admitted concurrent request), which replaced an unbounded
            raw-`threading.Thread`-per-request pattern -- scale-out hardening
            pass, see `docs/SCALE_OUT_PROMPT.md`'s bottleneck #1. Sized
            generously above this app's own measured per-question latency
            profile (`docs/PERFORMANCE_BASELINE.md`'s P95 ~158s) so ordinary
            multi-tab/multi-caller use is never throttled; a caller past
            this limit gets an immediate 429, not a queued wait -- there is
            no work queue yet (Phase 4 of the scale-out program).
        max_concurrent_ask_requests_per_caller: Max `POST /ask` executions
            one caller (an authenticated subject when available, else
            client IP -- see `api/main.py`'s `_rate_limit_key`) may have in
            flight at once. Deliberately small (2) -- this is a fairness/
            isolation bound (one chatty caller can't consume a large share
            of `max_concurrent_ask_requests`'s shared budget), not a
            throughput control; a real "wait for my last answer before
            asking again" UI never needs more than one or two in flight per
            caller (a second tab, or a retry racing a slow first attempt).
        api_action_rate_limit_per_minute: Max calls per minute, per client
            IP, to `POST /execute`, `POST /schema/refresh`, the mutating
            `/documents` routes (upload/delete), and `POST /generate/confirm`
            -- every state-changing or resource-intensive API action that
            previously had no rate limit of its own (`/ask` already had one;
            these didn't). Deliberately one shared setting rather than a
            separate knob per route -- these are all the same class of
            "a human clicks a button" action with a similar expected
            frequency, and splitting them out can be revisited if one
            route's real usage pattern needs a different budget.
        cost_estimation_enabled: Whether `db.query_cost` runs a proactive,
            non-executing cost estimate (EXPLAIN/SHOWPLAN) before running a
            validated query. Fails open regardless (see
            `cost_estimation_timeout_seconds`); this flag is an extra,
            simpler off-switch if it's ever undesirable for a given setup.
        cost_estimation_timeout_seconds: Short timeout for the plan-only
            EXPLAIN/SHOWPLAN call itself -- getting an execution plan
            should be fast; if it isn't, the check is abandoned and the
            query proceeds to the existing timeout-based protection rather
            than blocking the pipeline on plan estimation.
        cost_moderate_row_threshold: Estimated row count above which a
            query gets a "this may take a moment" notice but still runs.
        cost_high_row_threshold: Estimated row count above which a query is
            not run at all -- treated as a retryable error fed back to
            `generate_sql`, same as any other correctable mistake.
        log_level: Root logging level, e.g. "INFO", "DEBUG".
        log_redaction_level: How much result-set shape `observability.
            redaction.summarize_result_for_log` includes when logging a
            query result -- "standard" (default) logs row/column counts and
            column *names* (never cell values, which are never logged at
            any level); "strict" drops column names too, leaving only the
            counts. Column names can themselves be sensitive in some
            schemas (e.g. `ssn`, `salary`) even though the data isn't
            logged, hence the stricter option rather than treating
            "no cell values" as sufficient on its own.
        api_auth_token: Optional static bearer token required on every
            `api/` request (`Authorization: Bearer <token>`) when set. None
            (default, unset in `.env`) means this specific check is a
            no-op -- kept unchanged from before `oidc_issuer` existed (see
            below) as a deliberately simple option for
            machine-to-machine/CI callers even once OIDC is configured for
            interactive users; see `docs/AUTHENTICATION.md` for the full
            picture of how the two combine. A `SecretStr` for the same
            reason `db_password` is.
        environment: "development" (default) or "production" -- an
            explicit, coarse deployment-posture flag with exactly one
            enforced consequence today (see `_require_identity_in_production`
            below): a production deployment with no authentication
            configured at all (`auth_mode == "none"`) fails to start rather
            than silently serving every request unauthenticated. Not a
            general-purpose feature flag -- see `docs/AUTHENTICATION.md`
            for why this is deliberately narrow in scope.
        oidc_issuer: The `iss` claim every validated JWT must exactly
            match, and (together with `oidc_jwks_url`, if that's unset) the
            base URL this app derives `<issuer>/.well-known/openid-configuration`
            from to discover the provider's JWKS endpoint at startup. None
            (default) means OIDC/JWT authentication is off -- see
            `security/oidc.py` and `docs/AUTHENTICATION.md`.
        oidc_audience: The `aud` claim every validated JWT must contain.
            Required whenever `oidc_issuer` is set -- an issuer without an
            audience check would accept a token minted for a *different*
            application at the same identity provider, a real and common
            OIDC misconfiguration this field exists specifically to
            prevent.
        oidc_jwks_url: Explicit JWKS endpoint URL. Optional -- if unset
            while `oidc_issuer` is set, it's discovered once at first use
            via the issuer's own `/.well-known/openid-configuration`
            document (standard OIDC discovery). Set this directly to skip
            that discovery round-trip, or if the provider's discovery
            document lives somewhere non-standard.
        oidc_algorithms: Signing algorithms accepted for JWT verification.
            Deliberately a fixed allowlist read from *this config*, never
            from the token's own (attacker-controlled) `alg` header --
            accepting whatever algorithm a token claims is the classic
            "alg confusion" JWT vulnerability (e.g. an RS256-signed
            provider's public key gets misused as an HS256 shared secret).
            Defaults to `("RS256",)`, the near-universal choice for a real
            OIDC provider (Auth0, Okta, Azure AD, Keycloak, ...); `"none"`
            can never appear here regardless of configuration (see
            `_validate_oidc_algorithms` below).
        oidc_clock_skew_seconds: Leeway applied to `exp`/`iat`/`nbf`
            validation, to tolerate ordinary clock drift between this
            server and the identity provider. Small and bounded
            deliberately -- a large value would meaningfully extend how
            long an expired token stays acceptable.
        oidc_role_claim: Name of the JWT claim `agent.authz` reads the
            caller's role(s) from (a single string or a list of strings,
            both accepted -- see `security/oidc.py::extract_roles`).
            Provider-specific in practice (a raw OIDC ID token has no
            standard claim for this; Auth0/Keycloak/Azure AD each use their
            own custom-claim convention) -- configure this to match
            whatever the identity provider actually issues.
        google_oauth_client_id: This app's Google Cloud OAuth 2.0 Web
            Client ID -- a **public** identifier (safe to ship in a
            frontend response, unlike an OAuth client *secret*, which this
            app never needs -- see `security/google_oidc.py`'s own module
            docstring for why the ID-token-credential flow was chosen
            specifically to avoid ever holding a Google client secret).
            None (default) means Google sign-in is off; `GET /health`
            reports it as `google_signin_enabled: false` and the frontend
            never renders the button. Every verified ID token's `aud`
            claim must exactly equal this value -- see
            `security/google_oidc.py::verify_google_id_token`.
        google_oauth_allowed_hosted_domains: Optional Google Workspace
            domain allowlist (comma-separated, e.g. "example.com") for the
            verified `hd` claim. Empty (default) means no Workspace
            restriction -- any verified Google account may sign in.
            Deliberately never inferred from an `@domain` email suffix
            (a `hd` claim is validated server-side against Google's signed
            token; an email suffix is just string content the token's
            actual sender doesn't have to match).
        google_oauth_clock_skew_seconds: Leeway applied to the Google ID
            token's `iat`/`exp` validation -- the same purpose as
            `oidc_clock_skew_seconds` above, kept as its own separate field
            since Google sign-in doesn't share `security/oidc.py`'s
            validation path at all.
        google_oauth_nonce_ttl_seconds: How long a server-issued sign-in
            nonce (`GET /auth/google/nonce`) stays valid and claimable
            before it expires unused. Short and bounded deliberately -- a
            nonce is single-use and tied to one page load, not a session
            (none exists yet pre-authentication), so this is the only
            lifetime control on it.
        local_auth_enabled: Whether this app's own self-hosted user
            accounts (`identity/`) are active at all -- register/login/
            refresh/logout/password-reset/admin-user-management. False by
            default, mirroring every other optional subsystem in this
            codebase (`enable_document_rag`, `enable_web_search`, ...).
            When true, `api.auth.verify_api_key` tries a locally-issued
            JWT *first* (distinguished from an externally-issued OIDC
            token by its `iss` claim -- `jwt_issuer` here vs. `oidc_issuer`
            above), falling through unchanged to the existing OIDC ->
            static-token -> none chain for any token that isn't locally
            issued -- all three mechanisms can be configured simultaneously.
            A locally-authenticated user's roles are drawn from the same
            base role vocabulary `agent.authz.ROLE_PERMISSIONS` already
            knows (`viewer`/`user`/`analyst`/`admin`), so every existing
            AI/RAG/SQL route's RBAC check works unchanged for a local user
            -- see `identity/rbac.py` for the additional, separate
            permission layer that only gates the new user/history/admin
            endpoints. Requires `auth_database_url` to be set too.
        auth_database_url: Full SQLAlchemy connection string for the
            dedicated identity/history PostgreSQL database
            (`identity/models.py`'s 14 tables: users, roles, permissions,
            sessions, sign-in events, password-reset/email-verification
            tokens, conversations, prompts, AI outputs, voice transcripts,
            audit logs). Deliberately a *separate* connection from
            `DB_CONNECTIONS`/`Settings.databases` (the business/HR
            database(s) the text-to-SQL agent queries) -- this app must
            never store authentication data, password hashes, or activity
            history in the same database as the data it answers questions
            about. A `SecretStr` for the same reason `db_password` is.
            None (default) means local auth is entirely unavailable
            regardless of `local_auth_enabled`.
        auth_database_pool_size: `QueuePool`'s `pool_size` for the identity
            database engine (`identity/db.py`) -- explicit here (mirrors
            `moderation_store_pool_size`'s reasoning) since register/login/
            refresh/history-write traffic can be frequent relative to this
            app's other, mostly-read database traffic.
        auth_database_max_overflow: `QueuePool`'s `max_overflow` for the
            same engine.
        auth_database_pool_recycle_seconds: Seconds before a pooled
            identity-database connection is discarded and replaced
            regardless of use -- same reasoning as
            `moderation_store_pool_recycle_seconds`.
        allow_public_registration: Whether `POST /auth/register` accepts
            unauthenticated self-service sign-ups at all. False by default
            -- a new deployment should default to admin-provisioned
            accounts (`POST /admin/users`) unless the operator explicitly
            wants an open sign-up flow.
        require_email_verification: Whether a newly-registered account
            starts in `pending_verification` status (blocked from signing
            in until `POST /auth/verify-email` succeeds) rather than
            `active` immediately. False by default -- email verification
            requires an outbound mail-sending integration this codebase
            does not itself provide (see `identity/`'s own docs for the
            integration point), so leaving this off keeps registration
            fully self-contained for a fresh clone.
        password_min_length: Minimum accepted password length, enforced by
            `identity/password_policy.py::validate_password_strength`
            before hashing.
        password_max_length: Maximum accepted password length -- a real
            passphrase should never be silently truncated, so an
            over-length password is rejected outright with a clear message
            instead (`validate_password_strength`), never cut down to fit.
        max_login_attempts: Consecutive failed sign-in attempts (per
            account, within `login_lockout_minutes`) before
            `identity.repositories.users` locks the account temporarily
            (`users.locked_until`) -- closes the classic credential-
            stuffing/brute-force gap a login endpoint with no lockout has.
        login_lockout_minutes: How long an account stays locked after
            tripping `max_login_attempts` -- a fixed cooldown window, not
            an escalating one, kept simple by design.
        jwt_algorithm: Signing algorithm for locally-issued access tokens.
            `HS256` (the default, a single symmetric shared secret --
            `jwt_secret_key` below) is the zero-friction local-dev choice
            your own deployment doc should note when to graduate off of;
            `RS256`/`ES256` (asymmetric, `jwt_private_key_path`/
            `jwt_public_key_path` below) are the production-grade options,
            letting this process's public key be distributed for
            verification elsewhere without ever exposing the private
            signing key. Already-installed `pyjwt[crypto]` supports all
            three -- no new dependency needed either way.
        jwt_secret_key: The shared secret for `jwt_algorithm="HS256"`.
            Required (and length-validated -- see
            `_validate_local_auth_signing_material` below) whenever local
            auth is enabled with HS256. A `SecretStr` for the same reason
            `db_password` is.
        jwt_private_key_path: Path to a PEM-encoded RSA/EC private key,
            required for `jwt_algorithm="RS256"`/`"ES256"`. Loaded lazily
            by `identity/security.py` at first use (this field only
            validates that *a path was configured*, not that the file
            exists/parses -- the same "config validates shape, first use
            validates reality" split `db/connection.py` already follows).
        jwt_public_key_path: Path to the matching PEM-encoded public key,
            used for verification -- distributable to another service that
            only needs to *validate* tokens this process issues, never the
            private key itself.
        jwt_issuer: The `iss` claim stamped into (and required on) every
            locally-issued token -- also what `api.auth.verify_api_key`
            uses to recognize a locally-issued token before attempting
            local validation at all, distinguishing it from an externally-
            issued OIDC token without needing to guess from the token's
            shape.
        jwt_audience: The `aud` claim stamped into (and required on) every
            locally-issued access token -- same purpose as `oidc_audience`
            above, scoped to this app's own token issuance instead of an
            external provider's.
        access_token_expire_minutes: Access-token lifetime. Short by
            design (15 minutes, matching this feature's own security
            requirements) -- a compromised access token has a small,
            bounded window of usefulness; long-lived sessions are carried
            by the separate, revocable refresh token instead.
        refresh_token_expire_days: Refresh-token (opaque, hashed-at-rest --
            see `identity/security.py` and `identity/models.py`'s
            `AuthSession.refresh_token_hash`) lifetime. Rotated on every
            `POST /auth/refresh` call (a new opaque token issued, the old
            one's row marked revoked with `replaced_by_session_id` set) --
            reuse of an already-rotated/revoked refresh token is treated as
            a signal to revoke the whole session family, not just log a
            warning.
        cookie_secure: Whether the refresh-token cookie
            (`POST /auth/login`/`POST /auth/refresh` responses) is sent
            with the `Secure` attribute -- should only ever be false for
            plain-HTTP local development, never in a real deployment (a
            `Secure` cookie is only ever sent over HTTPS).
        cookie_samesite: `SameSite` attribute for the refresh-token cookie
            -- `lax` (the default) works for this app's same-origin-by-
            default deployment shape (`frontend/dist` served by this same
            FastAPI process); a cross-subdomain deployment may need
            `none` (which also requires `cookie_secure=true`, enforced by
            browsers regardless of this app's own config).
        cookie_domain: Explicit cookie `Domain` attribute override. None
            (default) lets the browser default to the exact host that set
            the cookie -- only set this for a deployment deliberately
            sharing the refresh cookie across subdomains.
        login_rate_limit_per_minute: Max `POST /auth/login` attempts per
            minute, per client IP (`agent.rate_limit.SlidingWindowRateLimiter`,
            the same primitive `api/rate_limit.py` already wraps for other
            routes) -- a first line of defense against credential-stuffing
            independent of the per-account lockout above (that one is
            keyed on the account being attacked; this one is keyed on the
            caller, so it also throttles an attacker probing many
            different accounts from one IP).
        register_rate_limit_per_hour: Max `POST /auth/register` attempts
            per hour, per client IP -- guards against automated mass
            account creation.
        password_reset_rate_limit_per_hour: Max `POST /auth/forgot-password`
            attempts per hour, per client IP -- guards against using the
            reset-request endpoint as an account-enumeration or mail-
            bombing vector.
        password_reset_token_expire_minutes: How long a `POST
            /auth/forgot-password`-issued reset token stays redeemable
            (`identity.repositories.tokens`). Single-use regardless --
            redeeming it (or letting it expire) both permanently retire it.
        email_verification_token_expire_minutes: Same meaning as
            `password_reset_token_expire_minutes`, for `POST
            /auth/verify-email` tokens -- deliberately longer-lived (a day,
            not an hour) since a verification email is lower-urgency than a
            password-reset request.
        app_base_url: This app's own externally-reachable origin, used only
            to build the link inside a password-reset/verification "email"
            (`identity/email.py` -- see that module's own docstring for why
            it's a disclosed console-log placeholder, not real mail
            delivery, today).
        bootstrap_admin_enabled: Whether `scripts/bootstrap_admin.py`
            (or `identity.bootstrap.bootstrap_admin_user`, called directly)
            is permitted to create the very first admin account at all.
            False by default -- deliberately requires an explicit,
            separate opt-in beyond just having `bootstrap_admin_email`/
            `bootstrap_admin_password` set, so a production `.env` that
            still has leftover bootstrap credentials from initial setup
            can't accidentally re-provision an admin account on a later,
            unrelated run. Bootstrapping is also idempotent regardless
            (see `identity/bootstrap.py`'s own docstring) -- this flag is
            an extra, explicit safety rail on top of that, not a
            substitute for it.
        bootstrap_admin_email: Email address for the bootstrap admin
            account. Never hardcoded in source -- must come from `.env`
            (or process environment) whenever `bootstrap_admin_enabled` is
            true.
        bootstrap_admin_password: Password for the bootstrap admin account,
            hashed (never stored raw) the same way any other user's
            password is (`identity.security.hash_password`). A `SecretStr`
            for the same reason `db_password` is.
        log_prompt_content: Whether the *content* of a saved prompt/AI
            output (as opposed to its metadata -- status, token counts,
            latency, feature type) may ever appear in an ordinary log line
            (never in `security.audit_log` events regardless, which only
            ever log metadata). False by default -- prompt/answer content
            can carry real user/business data, so verbose content logging
            is opt-in-for-development only, never a production default.
        log_voice_transcript_content: Same meaning as `log_prompt_content`,
            for raw/corrected voice transcript text specifically -- kept as
            its own separate flag since voice transcripts are a
            particularly privacy-sensitive content type (recorded speech)
            an operator may want to reason about independently of typed
            prompt logging.
        enable_multi_source_router: Whether `api/main.py` routes
            questions through `agent.orchestrator.graph.run_orchestrated`
            (the multi-source router) instead of calling
            `agent.graph.run_agent` directly. False by default -- with it
            off, `run_orchestrated` itself just calls `run_agent` and
            returns its result unwrapped, so a fresh clone with no other
            `.env` changes behaves identically to the app before this
            existed. See `agent/orchestrator/graph.py`'s module docstring.
        enable_query_planning: Whether `agent.nodes.plan_query_node` makes an
            up-front planning LLM call for a question `agent.complexity.
            detect_complexity_signals` judges non-trivial (top-N-per-group,
            period-over-period growth, several metrics at once -- the same
            signals that widen the retry budget, see
            `complex_query_max_retry_bonus` above). True by default. A
            *simple* question never triggers a planning call regardless of
            this flag -- it only gates the complex-question path, so leaving
            this on has zero effect on the common case. See
            `agent/nodes.py::plan_query_node`.
        query_plan_max_tokens: Max tokens the LLM may generate for the
            query plan (a short JSON array of step strings) -- deliberately
            small and separate from `llm_max_tokens`, mirroring
            `insight_max_tokens`'s reasoning.
        sql_review_max_tokens: Max tokens the LLM may generate for the
            plan-conformance review verdict (`agent.nodes.review_sql_node`)
            -- "PASS" or a one-sentence "FAIL: <reason>", never more.
        enable_golden_examples: Whether `agent.nodes
            .retrieve_golden_examples_node` looks up human-approved past
            (question, SQL) pairs (see `embeddings.golden_examples`) and
            injects the best-matching ones into the generation prompt as
            few-shot examples. True by default -- safe even with an empty
            golden dataset (the common case until a user has saved any),
            since retrieval on an empty/missing collection simply returns
            no examples, the same fail-open contract `plan_query_node`
            already has. See `agent/nodes.py::retrieve_golden_examples_node`.
        golden_examples_top_k: Max golden examples retrieved per question
            (before the similarity filter below is applied).
        golden_examples_min_similarity: Minimum cosine similarity
            (0.0-1.0) a saved example must clear to actually be injected --
            a poor match is worse than no example at all (it can mislead
            the model toward an unrelated pattern), so this is a real
            quality gate, not just a top-k cap.
        rag_store_connection_string: Full SQLAlchemy connection string for
            the document/policy RAG store (`rag/store.py`) -- a SQL Server
            2025+/Azure SQL database using the native `VECTOR` column type
            (see `rag/store.py`'s module docstring for the schema). Deliberately
            a *separate* connection from `DB_CONNECTIONS`/`Settings.databases`
            -- chunk/embedding storage is not business data and shouldn't
            share a schema (or a connection pool) with a configured SQL
            database. None (default, unset) means document/policy RAG is
            entirely unavailable regardless of `enable_document_rag`/
            `enable_policy_rag` below -- `rag/store.py` fails fast with a
            clear message if either is on but this isn't set.
        rag_store_odbc_driver: ODBC driver name for the RAG store connection,
            same meaning as `db_odbc_driver`.
        enable_document_rag: Whether the general "documents" collection is
            offered to the orchestrator's router at all. False by default.
        enable_policy_rag: Whether the separate, more access-sensitive
            "policies" collection is offered to the router. False by default,
            and independent of `enable_document_rag` -- either can be on
            without the other.
        rag_top_k: Number of most-relevant chunks retrieved per question,
            per collection (documents/policies each retrieve their own top-k
            independently).
        rag_max_retries: Max query-rewrite retries in the agentic RAG
            subgraph (`rag/graph.py`) before falling back to "insufficient
            information" -- same bounded-retry philosophy as `max_retries`
            for the SQL loop, a separate knob since a bad chunk retrieval
            and a bad SQL parse aren't the same kind of budget.
        rag_chunk_size: Target chunk length in characters when splitting an
            ingested PDF's extracted text (`rag/ingestion.py`).
        rag_chunk_overlap: Character overlap between consecutive chunks, so
            a sentence spanning a chunk boundary isn't lost to either chunk
            alone.
        rag_embedding_model_name: Embedding model for document/policy
            chunks. Blank (default) reuses `embedding_model_name` (the same
            model already embedding schema DDL) -- set this only if
            documents genuinely need a different model than schema
            retrieval does.
        enable_pdf_download: Whether `rag/ingestion.py::ingest_pdf` stores
            the original uploaded PDF's raw bytes (a new `pdf_bytes`
            column on `rag.documents` -- see `rag/store.py::ensure_schema`)
            so it can later be downloaded from a chat answer's citation or
            the Knowledge Sources management page. True by default because
            that's a real, requested capability and PDFs are typically not
            huge, but unlike the compute-only `enable_query_planning`/
            `enable_golden_examples` flags this is a genuine, visible
            storage-growth cost (every ingested PDF's full bytes, on top of
            its extracted chunks) -- turn this off if that matters for your
            deployment. Only ever applies to newly-ingested documents from
            the point this is enabled; a document ingested before this
            existed (or while it was off) has no bytes to serve and simply
            never shows a download option (`DocumentRecord.has_pdf_bytes`/
            `rag.store.ChunkResult.has_pdf_bytes` are both False for it) --
            re-uploading it is the only way to make it downloadable.
        max_document_upload_mb: Upper bound on one `POST /documents`
            upload, enforced by reading at most this many bytes (+1, to
            detect an over-limit upload) regardless of what the request
            claims its size is -- `api/documents.py::upload_document`
            previously called `file.read()` with no cap at all, an
            unbounded-memory-read risk from a single oversized upload.
        max_document_pages: Upper bound on one uploaded PDF's page count,
            enforced by `rag.ingestion.extract_pdf_pages` before extracting
            text from any page. 2026 Phase 2 security review: closes a
            decompression-bomb-shaped gap `max_document_upload_mb` alone
            doesn't -- a PDF's on-disk size says little about how many
            pages (and therefore how much CPU/memory) extracting its text
            actually costs, so a small-but-absurdly-high-page-count file
            could still be expensive relative to what its byte size
            suggested. Default (2000) is generous for a legitimate
            document while still bounding the worst case.
        enable_web_search: Whether the web_search node is offered to the
            router at all. False by default, and independent of
            `web_search_api_key` being set -- both must be true/present for
            the node to actually run (see `search/web_search.py`).
        web_search_provider: Which provider `search/web_search.py` calls --
            see `search.web_search.SUPPORTED_SEARCH_PROVIDERS` for the
            supported values. Swapping providers is a config change, not a
            code change, mirroring `DB_TYPE`'s own pattern.
        web_search_api_key: API key for the configured provider. A
            `SecretStr` for the same reason `db_password` is.
        web_search_max_results: Max results requested per web search call.
        web_search_answer_max_tokens: Max tokens for the LLM call that drafts
            the web-search answer (`agent.orchestrator.nodes.web_search_node`).
            Deliberately its own, larger-than-`llm_max_tokens` setting rather
            than reusing that one -- a web answer is expected to synthesize
            several external sources into a structured, in-depth response
            (headings, lists, an inline citation per claim), not a single
            short SQL-adjacent answer, so it needs materially more budget.
        enable_media_generation: Whether IMA Studio image/video/audio
            generation (`media_gen/`, routed as the orchestrator's
            "generation" source -- see `agent.orchestrator.nodes
            .generation_node`) is offered to the router at all. False by
            default, and independent of `ima_api_key` being set -- both
            must be true/present for `get_available_sources` to include
            it, mirroring `enable_web_search`/`web_search_api_key`'s own
            pair. `media_gen/client.py`'s `IMAEndpoints` paths are
            confirmed working against a real IMA account (a live
            text-to-image call succeeded end-to-end: generation, download,
            and serving via `GET /media/{media_id}`) -- video generation
            shares the same client/task-creation code path but has not yet
            been separately confirmed with a live call. Still off by
            default so a fresh clone never spends real IMA credits without
            the operator deliberately opting in.
        ima_api_key: IMA Studio API key. A `SecretStr` for the same reason
            `web_search_api_key` is.
        ima_api_base_url: IMA Studio API base URL. Kept a separate,
            explicit field (not hardcoded in `media_gen/`) in case IMA's
            own docs specify a different host than this default once
            actually confirmed.
        media_gen_rate_limit: Max media generation calls allowed within
            `media_gen_rate_window_seconds`, checked by
            `agent.orchestrator.nodes.generation_node` via
            `agent.rate_limit.get_media_generation_limiter` before every
            call into `media_gen`. Deliberately its own, tighter budget --
            not `llm_call_rate_limit_per_minute` reused -- since image/
            video/audio generation costs meaningfully more per call than a
            text LLM turn.
        media_gen_rate_window_seconds: Window width for the limiter above.
        media_gen_video_duration_seconds: Overrides the video model's own
            default "duration" form field (`media_gen.video.generate_video`'s
            `duration_seconds`) -- `None` (the default) leaves the model's
            own default alone. IMPORTANT: this is NOT a general "make
            videos any length" knob -- verified live against this
            account's actual auto-selected model (a read-only, no-cost
            `GET /open/v1/product/list?category=text_to_video` call): the
            current model ("Seedance 2.0") only accepts an integer 4-15
            (its own declared `form_config` min/max), default 5. No IMA
            video model available on this account (or, as far as this
            project has confirmed, offered by IMA at all) supports a
            single multi-minute generation -- that's a real model-capability
            ceiling, not a restriction this app imposes. The `le=60` bound
            here is a generous top-level sanity guard, not a claim that 60
            is achievable; a value the actual selected model rejects
            surfaces as a clean provider-failure `MediaGenerationResult`,
            same as any other IMA business-error rejection.
        require_generation_approval: Whether `agent.orchestrator.nodes
            .generation_node` requires an explicit human confirmation
            (`POST /generate/confirm`) before it actually calls IMA --
            secure by default (`true`): generation is a real, metered
            third-party API call, the only source in the orchestrator that
            spends money, so it gets the same human-in-the-loop gate the
            SQL pipeline's "Confirm and Run" already applies to query
            execution. When true, `generation_node` only proposes what
            would be generated (`status="pending_approval"`, no `media_id`,
            nothing charged) until confirmed. Set `false` only for a
            trusted automation context that has already reviewed this
            tradeoff and wants the previous fully-autonomous behavior.
        enable_image_editing: Whether the local image editor's "AI-guided
            editing" panel (`components/image/ImageEditor.tsx`) may call a
            real, generative image-editing backend at all -- see this
            field's own inline comment near its declaration for the full
            design (reuses `ima_api_key`/the `image_to_image` IMA task
            category, a deliberately separate flag from
            `enable_media_generation` since this sends a user's own
            uploaded image externally). False by default.
        image_edit_timeout_seconds: Hard ceiling on one synchronous AI
            image-edit request's wall-clock time.
        image_edit_poll_interval_seconds: How often to poll IMA for a
            submitted image-edit task's completion.
        image_edit_max_prompt_length: Max length of the free-text edit
            instruction a caller may submit to `POST /attachments/{id}
            /ai-edit`.
        enable_voice_mode: Whether voice input/output (`voice/`, spoken
            questions transcribed via `faster-whisper`, spoken answers
            synthesized via Piper -- both local, no cloud API, same
            "fully local" posture as Ollama) is offered at all. True by
            default -- unlike media generation, this feature spends no
            money and makes no network call once its one-time local model
            downloads are done, so there's no cost-control reason to make
            it opt-in. Exposed to the frontend as `GET /health`'s
            `voice_enabled` field; the React dashboard only shows the mic
            button and its own settings toggle when this is true. A
            transcribed question is never treated specially by the agent
            -- it reaches `POST /ask` as plain text through the exact same
            path a typed question does, so `agent.input_guard.check_input`
            still applies unconditionally.
        stt_model_size: `faster-whisper` model size (`tiny`/`base`/`small`/
            `medium`/`large-v3`). `base` is the default accuracy/latency
            tradeoff for short spoken questions on CPU; auto-downloaded
            from Hugging Face Hub on first use and cached locally, the
            same "pull once, run offline after" shape as `ollama pull`.
        stt_device: `cpu` or `cuda`, passed straight to `WhisperModel`.
            Defaults to `cpu` -- nothing in this project's deployment
            (`docker-compose.yml`, `Dockerfile`) assumes a GPU is present;
            `cuda` is a manual opt-in for a machine already confirmed to
            have a working CUDA + cuDNN setup for `ctranslate2`.
        stt_vocabulary_max_chars: Caps the length of the schema-derived
            vocabulary hint `voice.stt._build_vocabulary_hint` feeds
            Whisper's `initial_prompt` (table/column names from every
            configured database, so "branch_id"/"dispute" are less likely
            to be misheard as similar-sounding common words) -- Whisper's
            own prompt-biasing works best short, not as a full schema dump.
        enable_voice_correction: Whether a raw `voice/stt.py` transcript is
            run through one extra Ollama call (`voice/correction.py`) to fix
            misrecognized words, strip filler words/false starts, resolve
            self-corrections, and normalize punctuation before it's
            returned to the caller -- the same accuracy-aid shape as
            `enable_query_planning`/`enable_golden_examples`, applied to STT
            output instead of SQL generation. True by default -- like the
            rest of voice mode, this is a local Ollama call, so it costs no
            money and makes no external network call. Fails open on any
            Ollama error (`voice.correction.correct_transcript` never
            raises): the raw transcript is used unchanged rather than
            failing the request.
        voice_correction_max_tokens: Caps the correction call's `num_predict`
            -- the corrected sentence is normally about as long as the raw
            transcript, so this only needs headroom over
            `stt_vocabulary_max_chars`-sized input, not a large budget like
            `llm_max_tokens`.
        voice_max_upload_mb: Max accepted size of one recorded-question
            upload to `POST /voice/transcribe`, enforced the same way
            `max_document_upload_mb` already caps `POST /documents`
            (read-and-reject-if-over, never trust `Content-Length` alone).
        voice_max_duration_seconds: Max accepted audio duration, checked
            by probing the decoded audio's length *before* running the
            (comparatively expensive) Whisper model, not after -- an
            oversized recording is rejected cheaply.
        tts_voice: Piper voice model name (e.g. `en_US-lessac-medium`),
            downloaded once via `scripts/download_voice_model.py` into
            `voice/models/` (or `tts_voice_model_path`, if set).
        tts_voice_model_path: Explicit override for where the Piper
            `.onnx`/`.onnx.json` pair lives, if not the default
            `voice/models/<tts_voice>.onnx`. `None` (the default) uses
            that default location.
        enable_media_search: Whether the "media_search" orchestrator
            source (`media/`, searching an untagged local image/video
            library by content -- see `agent.orchestrator.nodes
            .media_search_node`) is offered at all. False by default, and
            independent of `media_library_path` being set -- both must be
            true/present for `get_available_sources` to include it,
            mirroring `enable_web_search`/`web_search_api_key`'s pair.
            Unlike every other optional source, this needs no API key for
            its default embedding provider (`local_clip`, fully on-device)
            -- but does pull in `torch` (via `sentence-transformers`) and
            `opencv-python` (via `scenedetect`), a real, meaningfully
            larger dependency footprint than this project's other optional
            features, which is why it stays off by default rather than
            joining voice mode as an on-by-default feature.
        media_library_path: Root folder `scripts/build_media_index.py`
            walks to ingest images/videos, and the same root
            `api/media_library.py`'s serving route re-validates a resolved
            file still lives under before ever opening it (a path-
            traversal guard, in case a stored path were ever stale/
            manipulated). `None` (the default) means media search has
            nothing to index -- `get_available_sources` treats that the
            same as the flag being off.
        media_embedding_provider: Which embedding backend `media/embedding
            .py` uses. Only `local_clip` (on-device, via
            `sentence-transformers`, no API key, no per-item cost, no data
            leaving the machine) is implemented today -- the `Literal` type
            (rather than a bare `str`, unlike `web_search_provider`) is
            deliberate: there's exactly one real option right now, and a
            typo should fail loudly at config-load time, not silently at
            first use. Structured the same way `search/web_search.py`'s
            `SUPPORTED_SEARCH_PROVIDERS` dict is, so adding a hosted
            provider (Voyage/Vertex/OpenAI multimodal embeddings) later is
            a new dict entry, not a redesign.
        media_clip_model_name: The `sentence-transformers` CLIP checkpoint
            used for both image and query-text embedding (the same model
            embeds both sides, guaranteeing they share one vector space --
            see `media/embedding.py`). `clip-ViT-B-32` is a small, current,
            well-documented default; confirmed to still be the right
            general-purpose pick (not superseded by something bundled by
            default) as of this feature's implementation.
        media_vision_model: An Ollama vision-capable model name (e.g.
            `llava`) used by `media/captioning.py` to generate a dense
            caption per video segment from its keyframe + transcript
            context. Blank (the default) means captioning is skipped --
            `media/ingest.py` still indexes a video segment via its ASR
            transcript and OCR text alone, just with less signal. This is
            a fail-open accuracy aid, the same posture as
            `agent.llm_client._build_golden_examples_block`, never a
            reason ingestion can't proceed. Requires a one-time `ollama
            pull <model>`, the same kind of setup step
            `scripts/download_voice_model.py` already requires for Piper.
        media_max_file_mb: Max size of one file `media/ingest.py` will
            process, enforced by reading and rejecting-if-over before any
            decoding -- the same read-and-reject-if-over pattern
            `max_document_upload_mb`/`voice_max_upload_mb` already use.
        media_search_top_k: Max hits returned by `media.search.search_media`
            per query, across both the image and video-segment collections
            combined.
        media_scene_detect_threshold: `PySceneDetect`'s `ContentDetector`
            sensitivity used by `media/keyframes.py` -- lower values
            detect more (subtler) scene changes, producing more segments
            per video. 27.0 is `PySceneDetect`'s own documented default.
            `POST /search/media` reuses the existing shared
            `api_action_rate_limit_per_minute` (`api.rate_limit
            .enforce_api_action_rate_limit`) rather than a dedicated
            limiter -- a query is cheap (embeds text, searches a local
            Chroma collection) and orchestrator-routed media_search
            questions are already bounded by `/ask`'s own limiter, the
            same reasoning document/policy RAG have no dedicated limiter
            of their own either.
        enable_chat_attachments: Whether a question submitted to `POST /ask`
            may carry `attachment_ids` (files uploaded via `POST
            /attachments/upload` -- images, PDFs, Office documents, plain
            text/CSV/JSON) that get processed and given to the model as
            extra context/multimodal content, via a new
            `agent.orchestrator.nodes.attachment_node` orchestrator source
            (`attachments/graph.py`). On by default -- like
            `enable_voice_mode`, this spends no money and makes no outbound
            network call (image description reuses the already-local
            `media_vision_model` Ollama call, exactly like
            `media/captioning.py`) and previously "attaching" a file in the
            React dashboard only held it in browser memory for local
            preview/editing with no way to reach the model at all (see
            `docs/image-editing-architecture.md`'s "no backend endpoint
            accepts a chat image attachment" disclosure -- that's the gap
            this closes). Deliberately checked without also requiring
            `ENABLE_MULTI_SOURCE_ROUTER`: attaching a file to a question is
            a per-request capability the caller opts into by attaching a
            file at all, not a standing multi-source-routing decision --
            `agent.orchestrator.graph.run_orchestrated` runs its graph
            whenever attachments are present even with the router flag off,
            mirroring `attachments` into the orchestrator's normal
            single-branch short-circuit instead of a second code path.
        max_attachment_image_bytes: Upper bound on one image attachment's
            size, read-and-reject-if-over before decoding -- mirrors
            `Settings.max_document_upload_mb`'s own "reject before doing any
            real work" pattern, scaled down since an image this app embeds
            as a base64 data URL costs real prompt/context budget per byte,
            unlike a PDF whose bytes are only ever parsed, never sent whole.
        max_attachment_document_bytes: Upper bound on one non-image
            attachment's size (PDF/DOCX/XLSX/PPTX/TXT/MD/JSON/CSV),
            enforced the same read-and-reject-if-over way.
        max_attachments_per_message: Max number of files one `/ask` call may
            attach at once (`AskRequest.attachment_ids`) -- bounds both
            upload abuse and how much attachment context a single question
            can inject into one prompt.
        max_total_attachment_bytes: Upper bound on the *combined* size of
            every attachment on one message -- closes the gap
            `max_attachment_document_bytes` alone leaves open (many
            individually-small files summing to something huge).
        max_attachment_text_chars: Upper bound on how many characters of
            extracted text `attachments.context_builder.build_attachment_context`
            will inject into one prompt, across all attachments combined --
            the single hard ceiling that keeps one huge PDF/spreadsheet from
            consuming the entire model context window. See that function's
            own docstring for the truncation/notice behavior once this is
            hit.
        max_attachment_document_pages: Upper bound on one attached PDF's
            page count, mirroring `Settings.max_document_pages`'s own
            decompression-bomb-shaped rationale (a PDF's byte size alone
            says little about how many pages -- and therefore how much
            CPU/memory extracting its text costs -- it declares). A smaller
            default than the persistent-knowledge-base PDF pipeline's own
            limit, since a chat attachment's content is re-sent to the
            model on every follow-up turn in the same conversation, not
            embedded once into a vector store.
        max_attachment_spreadsheet_rows: Upper bound on how many rows
            `attachments.processors.xlsx_processor`/`csv_processor` will
            read from one sheet/file before truncating (with an explicit
            "truncated" notice in the extracted text, never a silent cut).
        attachment_storage_dir: Directory attachment bytes are saved under,
            one file per `Attachment.attachment_id` (never the caller's
            original filename -- see `attachments.storage.sanitize_filename`)
            -- outside any directory this app serves as static content, the
            same "store files outside the public web root" rule
            `Settings.chroma_persist_dir`/`voice/models/` already follow.
            Gitignored and regenerated, like every other local-state
            directory this app creates.
        max_attachment_image_dimension_px: An attached image wider or taller
            than this is downscaled (preserving aspect ratio) before it's
            ever turned into a base64 data URL for the vision model --
            bounds both the prompt-size cost of embedding it and, for a
            local vision model with a fixed effective input resolution
            (`Settings.media_vision_model`), wasted bytes the model
            couldn't use anyway. 1568px matches the long-edge figure
            several hosted vision APIs document as their own effective
            resolution ceiling -- a reasonable default even though this
            app's actual (local Ollama) vision model's own ceiling varies
            by checkpoint.
        max_attachment_resize_dimension_px: Upper bound on a requested
            `POST /attachments/{id}/resize` target width/height, and the
            working long-edge cap `attachments.ocr_extract`/
            `attachments.inpaint` downscale an oversized source image to
            before running OCR/inpainting -- both a decompression-bomb-style
            guard (an on-disk attachment's original bytes are never
            downscaled at upload time, unlike the vision-model data URL
            path -- see `max_attachment_image_dimension_px`) and a sane UX
            ceiling for the resize dialog's own numeric inputs.
        attachment_ocr_timeout_seconds: Per-call timeout for a `pytesseract`
            OCR pass (`attachments.ocr_extract`, and the text-region
            detection step `attachments.inpaint` reuses) -- passed straight
            through to `pytesseract`'s own `timeout=` kwarg, which raises on
            expiry rather than hanging the request indefinitely on a
            pathological image.
        max_text_removal_regions: Upper bound on how many text regions one
            `POST /attachments/{id}/remove-text` call will mask and inpaint
            in a single request -- bounds both OCR-detection cost and how
            large the generated inpainting mask (and therefore the
            `cv2.inpaint` call) can get.
        attachment_retention_hours: How long an uploaded attachment's bytes
            and in-memory processed record are kept before
            `attachments.storage.purge_expired_attachments` considers them
            eligible for deletion -- bounds how long a stale chat upload
            lingers on disk. This app has no background scheduler (a
            deliberate, disclosed limitation shared with
            `media_gen.cache.MediaCache`'s own FIFO-only eviction); the
            purge function is called opportunistically (on each new upload)
            rather than on a timer.
        moderation_provider: Which content-moderation backend
            `moderation/provider.py` uses to check a chunk before it's ever
            embedded/stored. Only `azure_content_safety` is implemented
            today -- structured the same `SUPPORTED_..._PROVIDERS`-dict
            pattern as `web_search_provider`/`media_embedding_provider` so
            a second provider is a new dict entry, not a redesign. This
            gate is mandatory (not a feature flag) whenever
            `enable_media_search` or `enable_document_rag`/
            `enable_policy_rag` is on -- there is deliberately no
            `ENABLE_CONTENT_MODERATION` toggle that could silently disable
            it while leaving content ingestion on; missing provider/store
            config fails closed at first ingestion attempt
            (`moderation.exceptions.ModerationNotConfiguredError`), the
            same "fail at point of use" posture
            `rag.store.RagStoreNotConfiguredError` already has.
        azure_content_safety_endpoint: The Content Safety resource's REST
            endpoint (e.g. `https://<resource>.cognitiveservices.azure.com`).
            Called directly via `httpx` (`.../contentsafety/image:analyze`,
            `.../contentsafety/text:analyze`) -- no `azure-ai-contentsafety`
            SDK dependency, matching how `search/web_search.py` (Tavily)
            and `media_gen/client.py` (IMA) both call their provider's REST
            API directly rather than pulling in a vendor SDK.
        azure_content_safety_key: Subscription key for the endpoint above.
            A `SecretStr` for the same reason `ima_api_key`/
            `web_search_api_key` are.
        moderation_severity_threshold: Minimum Azure Content Safety
            severity (0/2/4/6 on its standard four-level scale) for the
            Hate/SelfHarm/Sexual/Violence categories that triggers a
            hard-reject. Verify this against your specific Content Safety
            API version before relying on it -- Azure has more than one
            severity-scale revision across API versions.
        moderation_blocklist_path: Override for the weapons/drugs text-term
            blocklist YAML `moderation/gate.py` checks OCR/caption/
            transcript/extracted text against -- mirrors
            `config.sensitive_columns`'s `path` override, mainly for tests.
            `None` (the default) uses `config/moderation_blocklist.yaml`.
            Azure Content Safety has no dedicated "weapons"/"drugs" harm
            category (only Hate/SelfHarm/Sexual/Violence) -- this blocklist
            plus the `Violence` category (a coarse proxy for weapons
            imagery specifically) is how those two are covered; drug
            imagery has no visual signal in this implementation at all,
            text-mentions only.
        malware_scan_provider: Which binary-malware scanner
            `security/malware_scanner.py` uses to check an upload's raw
            bytes before any parser (`pypdf`/`pymupdf`/Pillow/PySceneDetect)
            ever touches them -- structured the same `SUPPORTED_..._PROVIDERS`
            pattern as `moderation_provider`/`web_search_provider`. Unlike
            `moderation_provider`, this is **not** mandatory-by-default:
            `"disabled"` (the default) preserves this codebase's pre-existing
            upload behavior for every deployment that hasn't stood up a
            scanner yet -- no `MalwareScanner` abstraction existed at all
            before this setting was added, so defaulting it to "on" would
            silently break every existing document/media upload path the
            moment this shipped, for infrastructure (a ClamAV daemon) most
            deployments don't have. When set to `"clamav"`, scanning is
            genuinely fail-closed: an infected result, an unreachable/timed-
            out daemon, and a malformed response are all treated as a
            rejection (see `security.malware_scanner.ScanResult.blocked`) --
            never silently treated as "clean". `"disabled"` is audit-logged
            per upload (`malware_scan_skipped`) rather than silently
            skipped, so the gap is visible in the security event log even
            while off.
        clamav_host: Hostname/IP of the `clamd` daemon (ClamAV's scanning
            daemon), spoken to directly over its own `INSTREAM` wire
            protocol via a plain socket -- no `pyclamd`/vendor SDK
            dependency, matching how `moderation/provider.py`/
            `search/web_search.py`/`media_gen/client.py` all call their own
            provider's protocol directly rather than pulling in a client
            library. Only read when `malware_scan_provider="clamav"`.
        clamav_port: TCP port `clamd` listens on. ClamAV's own documented
            default (3310).
        malware_scan_timeout_seconds: Socket timeout for one `clamd`
            INSTREAM round trip. A large upload against an overloaded
            daemon timing out is treated as a scan **error**, not "clean" --
            see `malware_scan_provider`'s fail-closed note above.
        moderation_store_connection_string: Full SQLAlchemy connection
            string for the moderation decision/metadata store
            (`moderation/store.py`'s `moderation.media_assets` table) -- a
            SQL Server database, deliberately a *separate*, dedicated
            connection from both `DB_CONNECTIONS` and
            `rag_store_connection_string` (not reused, even though this
            table also covers PDF assets): media search is independently
            toggleable from document/policy RAG, and naming the shared
            setting after RAG specifically would be confusing when
            media-only search needs it too. Point both connection strings
            at the same physical database if you want one metadata store --
            that's a deployment choice, not a code-level coupling. None
            (default, unset) means ingestion for either pipeline fails
            closed with `ModerationNotConfiguredError`.
        moderation_store_odbc_driver: ODBC driver name for the moderation
            store connection, same meaning as `db_odbc_driver`.
        moderation_store_pool_size: `QueuePool`'s `pool_size` for the
            moderation store engine (`moderation/store.py`). Explicit here
            (unlike `db/connection.py`/`rag/store.py`, which both rely on
            SQLAlchemy's default of 5) because ingestion can run many
            concurrent DB writes -- sized against `media_ingest_workers`
            below plus normal concurrent PDF-upload request traffic sharing
            this same pool.
        moderation_store_max_overflow: `QueuePool`'s `max_overflow` for the
            same engine -- the burst ceiling above `moderation_store_pool_size`
            before a caller waits for a connection.
        moderation_store_pool_recycle_seconds: Seconds before a pooled
            connection is discarded and replaced, regardless of use --
            SQL Server (and many firewalls/load balancers) silently drop
            idle connections after some timeout; this should be set below
            whatever timeout your SQL Server instance/network enforces.
            1800 matches the value `db/connection.py`/`rag/store.py` both
            hardcode, now made configurable specifically for this
            higher-write-volume connection.
        media_ingest_workers: Max concurrent worker threads
            `scripts/build_media_index.py` uses to ingest files (each doing
            moderation + embedding + DB writes) -- this repo's first use of
            `concurrent.futures.ThreadPoolExecutor`; previously a plain
            sequential loop. I/O-bound work (moderation API calls, DB
            writes) benefits from threads despite the GIL. Sized alongside
            `moderation_store_pool_size` above -- a worker count exceeding
            the pool's real capacity just serializes ingestion behind pool
            exhaustion instead of the sequential loop it replaced.
        media_image_tile_threshold_px: An ingested image wider or taller
            than this (pixels) is split into a grid of tiles before
            moderation, on the theory that a moderation classifier's own
            internal downsampling could otherwise shrink away a small
            region of concern in a very large image. Images at or under
            this size are moderated as a single chunk.
        session_expensive_source_limit: Max combined "generation"/"web"
            source invocations allowed per `session_id` within
            `session_expensive_source_window_seconds`, checked in
            `agent.orchestrator.nodes.router_node` via `agent.rate_limit
            .get_session_expensive_source_limiter`. Each of those two
            sources already has its own call-rate limiter (media
            generation's, and the implicit one-request-per-question cost
            of a Tavily call), but nothing previously capped how many
            times *one session* could keep triggering either or both
            together across many questions -- a single question can
            already fan out to both at once (observed, real router
            behavior). `session_id` is a real per-conversation
            correlation token, not a substitute for authentication (see
            `get_session_expensive_source_limiter`'s own docstring) -- a
            cost-control speed bump, not an access-control boundary.
        session_expensive_source_window_seconds: Window width for the
            limiter above. An hour by default -- deliberately wider than
            the per-minute limiters elsewhere, since this is a
            session-lifetime cost ceiling, not a burst-rate control.
        project_root: Absolute path to the repository root.
        databases: Every configured database connection, parsed from
            `DB_CONNECTIONS` + per-name `DB_<NAME>_*` vars (see
            `_parse_named_connections`). Always has at least one entry:
            when `DB_CONNECTIONS` is unset, `_fill_default_database` below
            synthesizes a single connection named `"default"` from this
            same instance's flat `db_*` fields, so `db_type`/`db_host`/...
            above stay the single source of truth for a plain
            single-database setup, and `databases` is the one multi-
            database-aware code (`embeddings.retriever.select_database`,
            the React dashboard, the scripts) should read instead.
            `db.connection.get_connection(settings, name)` looks one up by
            name.
    """

    model_config = SettingsConfigDict(
        frozen=True,
        case_sensitive=False,
        # A blank env var ("FOO=" with nothing after it) behaves like unset
        # -- falls back to the field's default -- matching this file's old
        # `_env_str`/`_env_int` helpers exactly, rather than Pydantic's own
        # default of treating "" as a real, explicit empty-string value.
        env_ignore_empty=True,
        # Env vars this model doesn't declare a field for (PATH, a
        # completely unrelated tool's own env vars, ...) must never cause a
        # validation error -- pydantic-settings already ignores these by
        # default for env-var loading; stated explicitly since `extra`
        # would otherwise also govern (and reject) unexpected constructor
        # kwargs, which several call sites below pass deliberately
        # (`databases=`, in tests).
        extra="ignore",
    )

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_request_timeout_seconds: int = Field(default=300, gt=0)
    ollama_model_selection_enabled: bool = True
    # `NoDecode` tells pydantic-settings not to attempt its own default
    # JSON-array decoding of a compound-typed env var -- without it, an env
    # var actually set via OLLAMA_ALLOWED_MODELS=a,b,c raises
    # `pydantic_settings.exceptions.SettingsError` before
    # `_split_ollama_allowed_models` below (a field_validator(mode="before"))
    # ever runs, since env-source decoding happens in an earlier layer than
    # field validators. A real, previously-latent bug found while building
    # this: `cors_allowed_origins` right below uses the identical
    # comma-separated-string convention and had the exact same gap (no
    # existing test ever set CORS_ALLOWED_ORIGINS via a real env var to
    # catch it) -- fixed there too, same annotation.
    ollama_allowed_models: Annotated[tuple[str, ...], NoDecode] = ()

    db_type: str = ""
    db_host: str | None = None
    db_port: int | None = None
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_connection_string: SecretStr | None = None
    db_schema: str | None = None
    db_odbc_driver: str = "ODBC Driver 17 for SQL Server"
    db_pool_size: int = Field(default=10, gt=0)
    db_max_overflow: int = Field(default=20, ge=0)

    chroma_persist_dir: Path = Path("./embeddings/.chroma")
    chroma_collection_name: str = "schema_ddl"
    embedding_model_name: str = "all-MiniLM-L6-v2"
    schema_top_k: int = 4
    max_retries: int = Field(default=3, gt=0)
    complex_query_max_retry_bonus: int = Field(default=2, ge=0)
    max_result_rows: int = Field(default=1000, gt=0)
    query_timeout_seconds: int = Field(default=15, gt=0)
    request_timeout_seconds: int = Field(default=600, gt=0)
    llm_max_tokens: int = Field(default=1024, gt=0)
    insight_max_tokens: int = Field(default=120, gt=0)
    max_question_length: int = Field(default=500, gt=0)
    max_conversation_history_turns: int = Field(default=20, gt=0)
    question_rate_limit_per_minute: int = Field(default=10, gt=0)
    llm_call_rate_limit_per_minute: int = Field(default=20, gt=0)
    max_concurrent_ask_requests: int = Field(default=50, gt=0)
    max_concurrent_ask_requests_per_caller: int = Field(default=2, gt=0)
    api_action_rate_limit_per_minute: int = Field(default=20, gt=0)
    cost_estimation_enabled: bool = True
    cost_estimation_timeout_seconds: int = Field(default=3, gt=0)
    cost_moderate_row_threshold: int = Field(default=50_000, gt=0)
    cost_high_row_threshold: int = Field(default=1_000_000, gt=0)
    log_level: str = "INFO"
    log_redaction_level: Literal["standard", "strict"] = "standard"
    # Enterprise scalability/security assessment (2026-09-27): "text" (the
    # default) is byte-for-byte this codebase's existing terminal-friendly
    # format -- zero behavior change for local dev or any deployment that
    # doesn't opt in. "json" emits one JSON object per log line instead
    # (timestamp/level/logger/correlation_id/message, plus exception info
    # when present) -- the shape a real log-aggregation pipeline (ELK,
    # CloudWatch, Datadog, Loki) can actually parse/query/alert on, unlike
    # the existing pipe-delimited text format. See
    # `config.settings.configure_logging`/`_JsonLogFormatter`.
    log_format: Literal["text", "json"] = "text"
    enable_multi_source_router: bool = False
    enable_query_planning: bool = True
    query_plan_max_tokens: int = Field(default=300, gt=0)
    sql_review_max_tokens: int = Field(default=200, gt=0)
    enable_golden_examples: bool = True
    golden_examples_top_k: int = Field(default=3, gt=0)
    golden_examples_min_similarity: float = Field(default=0.75, ge=0.0, le=1.0)

    # --- Business-context vector retrieval (retrieval/, scripts/ingest_schema.py,
    # scripts/rebuild_index.py -- see docs/vector-retrieval-design.md). Adds
    # semantic retrieval of table/column/relationship descriptions, business
    # glossary terms, metric definitions, curated SQL examples, and
    # documentation on top of (never instead of) the live schema
    # introspection `embeddings/schema_indexer.py` already scopes SQL
    # generation with -- see agent.nodes.retrieve_business_context_node,
    # which runs between retrieve_golden_examples and plan_query and fails
    # open exactly like that node does. Reuses this project's existing
    # ChromaDB PersistentClient (retrieval_collection_name is suffixed
    # "__<database_id>", the same per-database-collection convention
    # chroma_collection_name/golden_examples already use) rather than a
    # second vector database -- see the design doc's "Selected vector
    # database" section for why.
    enable_business_context_retrieval: bool = True
    retrieval_collection_name: str = "knowledge_base"
    retrieval_embedding_provider: Literal["local", "fake"] = "local"
    # Only used when retrieval_embedding_provider="fake" (tests, or a
    # smoke-test environment with no real embedding runtime installed).
    retrieval_fake_embedding_dimensions: int = Field(default=32, gt=0)
    # If set, validated against the configured provider's actual output
    # width at ingestion/retrieval time (retrieval.embeddings.validate_dimensions)
    # -- a clear ConfigurationError-shaped failure instead of a cryptic
    # Chroma dimension-mismatch exception. Unset (the default) accepts
    # whatever the configured provider produces.
    retrieval_embedding_dimensions: int | None = None
    retrieval_similarity_metric: Literal["cosine", "l2", "ip"] = "cosine"
    retrieval_embedding_batch_size: int = Field(default=32, gt=0)
    retrieval_embedding_timeout_seconds: int = Field(default=30, gt=0)
    retrieval_embedding_retry_count: int = Field(default=2, ge=0)
    # Per-chunk-type retrieval limits -- see retrieval/retriever.py's
    # docstring for why these are separate, bounded knobs rather than one
    # flat top-k: an unbounded/shared limit would let one chunk type (most
    # often `column`, since there's one per table column) crowd out every
    # other type before reranking/diversity ever gets a chance to balance it.
    retrieval_top_k_tables: int = Field(default=5, ge=0)
    retrieval_top_k_columns: int = Field(default=8, ge=0)
    retrieval_top_k_relationships: int = Field(default=5, ge=0)
    retrieval_top_k_glossary: int = Field(default=3, ge=0)
    retrieval_top_k_metrics: int = Field(default=3, ge=0)
    retrieval_top_k_sql_examples: int = Field(default=3, ge=0)
    retrieval_top_k_documentation: int = Field(default=3, ge=0)
    retrieval_similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    retrieval_max_context_chars: int = Field(default=6000, gt=0)
    retrieval_max_context_tokens: int = Field(default=1500, gt=0)
    # Weight of raw vector similarity vs. chunk-type priority in
    # retrieval.reranker's final_score formula (see that module's docstring).
    retrieval_rerank_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    # How strongly a repeated (chunk_type, table_name) pair is discounted on
    # subsequent picks during reranking's diversity pass.
    retrieval_diversity_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    retrieval_documentation_chunk_chars: int = Field(default=1500, gt=0)
    retrieval_documentation_chunk_overlap_chars: int = Field(default=200, ge=0)
    retrieval_knowledge_dir: Path = Path("./data/knowledge")

    # Prompt 07 (07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md):
    # db.relationship_inference's candidate-FK inference. True by default --
    # the structural pass (name convention/type compatibility/target
    # uniqueness) never issues a query, so it's the same cost class as
    # relationship_chunks_from_schema itself. A candidate below
    # relationship_inference_min_confidence is dropped outright, never just
    # sorted last (see infer_relationships's own docstring for why). Every
    # resulting chunk is tagged AI_INFERENCE and rendered with an explicit
    # "CANDIDATE relationship... inferred, not confirmed" opening -- never
    # silently indistinguishable from a real, declared foreign key.
    enable_relationship_inference: bool = True
    relationship_inference_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # The second, data-driven refinement pass (null-fraction + bounded
    # value-overlap sampling) -- False by default, since unlike the
    # structural pass above it does issue one small, bounded query per
    # candidate (see db.relationship_inference.verify_candidates_with_data's
    # own docstring for the exact bound). Mirrors enable_media_search/
    # enable_document_rag's "off by default because it has a real,
    # non-trivial cost" posture, not enable_query_planning's "on by default
    # because it's compute-only" one.
    enable_relationship_data_verification: bool = False
    relationship_data_verification_sample_size: int = Field(default=200, gt=0)

    api_auth_token: SecretStr | None = None
    environment: Literal["development", "production"] = "development"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    oidc_clock_skew_seconds: int = Field(default=60, ge=0)
    oidc_role_claim: str = "roles"

    # --- Google sign-in (identity/, security/google_oidc.py -- a specific
    # provider integrated into this app's own local accounts, distinct from
    # the generic third-party-issued-JWT relying party above) ---
    google_oauth_client_id: str | None = None
    google_oauth_allowed_hosted_domains: Annotated[tuple[str, ...], NoDecode] = ()
    google_oauth_clock_skew_seconds: int = Field(default=60, ge=0)
    google_oauth_nonce_ttl_seconds: int = Field(default=300, gt=0)

    # --- Local user accounts / identity database (identity/, see that
    # package's own module docstrings; docs/AUTH_USER_MANAGEMENT.md has the
    # full picture) ---
    local_auth_enabled: bool = False
    auth_database_url: SecretStr | None = None
    auth_database_pool_size: int = Field(default=10, gt=0)
    auth_database_max_overflow: int = Field(default=20, ge=0)
    auth_database_pool_recycle_seconds: int = Field(default=1800, gt=0)
    allow_public_registration: bool = False
    require_email_verification: bool = False
    password_min_length: int = Field(default=12, gt=0)
    password_max_length: int = Field(default=64, gt=0)
    # Server-side default page sizes/caps for the chat-history endpoints
    # (identity/repositories/history.py, api/chat_history.py) -- mirrors
    # this project's existing `*_top_k`-style "one setting per tunable
    # limit" convention rather than hardcoding pagination sizes inline.
    chat_history_page_size: int = Field(default=30, gt=0, le=200)
    chat_history_max_page_size: int = Field(default=100, gt=0, le=500)
    chat_search_page_size: int = Field(default=20, gt=0, le=100)
    chat_search_max_page_size: int = Field(default=50, gt=0, le=200)
    # Universal chat-history persistence (api/chat_persistence.py) -- bounds
    # on what gets written into `ai_outputs.metadata` for a saved turn, kept
    # deliberately separate from `max_result_rows` (the *execution* row cap):
    # this caps what's durably persisted for later replay, not what a live
    # `/execute` call is allowed to return right now. A saved conversation
    # showing "first 50 of 500 rows" on reopen is an accepted, disclosed
    # tradeoff against storing an unbounded result snapshot per turn forever.
    chat_history_max_result_rows: int = Field(default=50, gt=0, le=1000)
    # Caps any single free-text field (an answer, a citation excerpt, a
    # rejection message, ...) persisted into a turn's history snapshot --
    # independent of this same text's own live-response length, which is
    # never truncated. Keeps one pathological answer from bloating a
    # conversation's storage footprint; the persisted field is truncated
    # with a trailing marker, never silently dropped.
    chat_history_max_text_chars: int = Field(default=4000, gt=0, le=50_000)
    # --- Secure conversation sharing (identity/share_policy.py,
    # identity/repositories/shares.py, api/shares.py) ---
    enable_conversation_sharing: bool = True
    # Deny-by-default deployment policy: an "anyone with the link" share is
    # only ever creatable when an operator has explicitly turned this on --
    # per this feature's own spec ("Anonymous link viewer: only if
    # explicitly enabled by deployment policy"). Off by default, unlike
    # `enable_conversation_sharing` itself, since a bearer link is a
    # materially larger exposure surface than an invite-only share.
    share_public_links_enabled: bool = False
    share_default_expiry_days: int = Field(default=30, gt=0, le=365)
    share_invitation_expiry_days: int = Field(default=14, gt=0, le=90)
    share_max_members_per_conversation: int = Field(default=50, gt=0, le=500)
    # Rate limits for the anonymous-reachable surface (`GET /shared/{ref}`,
    # attachment downloads, invitation acceptance) -- deliberately its own
    # settings block, not reused from `question_rate_limit_per_minute`,
    # since these routes have no authenticated caller identity to key a
    # per-user limiter on for an anonymous link visitor (see
    # `api/rate_limit.py`'s own per-IP fallback convention).
    share_link_access_rate_limit_per_minute: int = Field(default=30, gt=0)
    share_invite_rate_limit_per_hour: int = Field(default=20, gt=0)
    max_login_attempts: int = Field(default=5, gt=0)
    login_lockout_minutes: int = Field(default=15, gt=0)
    jwt_algorithm: Literal["HS256", "RS256", "ES256"] = "HS256"
    jwt_secret_key: SecretStr | None = None
    jwt_private_key_path: Path | None = None
    jwt_public_key_path: Path | None = None
    jwt_issuer: str = "text-to-sql-agent"
    jwt_audience: str = "text-to-sql-web"
    access_token_expire_minutes: int = Field(default=15, gt=0)
    refresh_token_expire_days: int = Field(default=14, gt=0)
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None
    login_rate_limit_per_minute: int = Field(default=10, gt=0)
    register_rate_limit_per_hour: int = Field(default=5, gt=0)
    password_reset_rate_limit_per_hour: int = Field(default=5, gt=0)
    password_reset_token_expire_minutes: int = Field(default=60, gt=0)
    email_verification_token_expire_minutes: int = Field(default=1440, gt=0)
    # Used only to build the link inside a password-reset/verification
    # "email" (identity/email.py) -- this app's own externally-reachable
    # origin, e.g. where the React dashboard is actually served from.
    app_base_url: str = "http://localhost:8000"
    bootstrap_admin_enabled: bool = False
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    log_prompt_content: bool = False
    log_voice_transcript_content: bool = False

    rag_store_connection_string: SecretStr | None = None
    rag_store_odbc_driver: str = "ODBC Driver 17 for SQL Server"
    enable_document_rag: bool = False
    enable_policy_rag: bool = False
    rag_top_k: int = Field(default=4, gt=0)
    rag_max_retries: int = Field(default=2, gt=0)
    rag_chunk_size: int = Field(default=1200, gt=0)
    rag_chunk_overlap: int = 150
    rag_embedding_model_name: str = ""
    enable_pdf_download: bool = True
    max_document_upload_mb: int = Field(default=25, gt=0)
    max_document_pages: int = Field(default=2000, gt=0)
    enable_web_search: bool = False
    web_search_provider: str = "tavily"
    web_search_api_key: SecretStr | None = None
    web_search_max_results: int = Field(default=5, gt=0)
    web_search_answer_max_tokens: int = Field(default=1200, gt=0)
    enable_media_generation: bool = False
    ima_api_key: SecretStr | None = None
    ima_api_base_url: str = "https://api.imastudio.com"
    media_gen_rate_limit: int = Field(default=5, gt=0)
    media_gen_rate_window_seconds: float = Field(default=60.0, gt=0)
    media_gen_video_duration_seconds: int | None = Field(default=None, gt=0, le=60)
    require_generation_approval: bool = True
    # AI-guided image editing (2026-09-27) -- a real, generative edit of a
    # user's *own uploaded image* (remove an object, replace a background,
    # region-transform), distinct from media_gen's own "generate a brand
    # new image from text" capability. Deliberately its own flag, not
    # folded into `enable_media_generation`: sending a user's own photo to
    # a third-party provider is a materially bigger privacy decision than
    # generating a fresh image from a text prompt, so it needs its own
    # explicit opt-in even when media generation is already on. Reuses the
    # exact same IMA Studio account/credential (`ima_api_key`) and the
    # `image_to_image` task category -- verified live against this
    # project's real account (a read-only, no-cost `GET /open/v1/product
    # /list?category=image_to_image` call): 5 real models available
    # (gpt-image-2, gemini-3.1-flash-image, gemini-3-pro-image,
    # doubao-seedream-4.5, midjourney), none of which expose a native mask
    # parameter -- see `media_gen/image_edit_provider.py`'s own docstring
    # for how a painted mask is conveyed to this provider instead (a
    # visual overlay baked into the image plus an explicit instruction,
    # not a pixel-level alpha channel -- a real, disclosed provider
    # limitation, not an oversight). Both this flag AND `ima_api_key` must
    # be set for `attachments.capabilities.get_attachment_capabilities` to
    # report `image_ai_editing=true` -- off by default so a fresh clone
    # never sends attachment images to a third party without the operator
    # deliberately opting in.
    enable_image_editing: bool = False
    # Hard ceiling on how long one synchronous `POST /attachments/{id}
    # /ai-edit` call will wait for IMA's own task-poll loop before failing
    # closed -- deliberately much shorter than `media_gen.client.IMAClient
    # .poll_task`'s own 600s default (reasonable for a fire-and-forget
    # generation the orchestrator already handles via its own timeout
    # machinery) because this is a synchronous HTTP request a real browser
    # tab is blocked on; real image_to_image edits with the models above
    # are typically much faster than video generation.
    image_edit_timeout_seconds: int = Field(default=90, gt=0, le=300)
    image_edit_poll_interval_seconds: float = Field(default=3.0, gt=0)
    # Caps the free-text edit instruction a caller may submit -- mirrors
    # `Settings.max_question_length`'s own "a static limit, not a security
    # boundary by itself" role; the real prompt-injection defense is the
    # same "treat all free text as untrusted data" framing this codebase
    # already applies everywhere else, not a length cap.
    image_edit_max_prompt_length: int = Field(default=500, gt=0)
    enable_voice_mode: bool = True
    stt_model_size: str = "base"
    stt_device: Literal["cpu", "cuda"] = "cpu"
    stt_vocabulary_max_chars: int = Field(default=200, gt=0)
    enable_voice_correction: bool = True
    voice_correction_max_tokens: int = Field(default=150, gt=0)
    voice_max_upload_mb: int = Field(default=10, gt=0)
    voice_max_duration_seconds: int = Field(default=30, gt=0)
    tts_voice: str = "en_US-lessac-medium"
    tts_voice_model_path: Path | None = None
    enable_media_search: bool = True
    media_library_path: Path | None = Path("./media_library")
    media_embedding_provider: Literal["local_clip"] = "local_clip"
    media_clip_model_name: str = "clip-ViT-B-32"
    media_vision_model: str = ""
    media_max_file_mb: int = Field(default=200, gt=0)
    media_search_top_k: int = Field(default=5, gt=0)
    media_scene_detect_threshold: float = Field(default=27.0, gt=0)
    enable_chat_attachments: bool = True
    max_attachment_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_attachment_document_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_attachments_per_message: int = Field(default=5, gt=0)
    max_total_attachment_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_attachment_text_chars: int = Field(default=80_000, gt=0)
    max_attachment_document_pages: int = Field(default=200, gt=0)
    max_attachment_spreadsheet_rows: int = Field(default=500, gt=0)
    max_attachment_image_dimension_px: int = Field(default=1568, gt=0)
    max_attachment_resize_dimension_px: int = Field(default=4096, gt=0)
    attachment_ocr_timeout_seconds: float = Field(default=20.0, gt=0)
    max_text_removal_regions: int = Field(default=20, gt=0)
    # Zip-container (DOCX/XLSX/PPTX) decompression-bomb guard -- checked
    # against the archive's own central-directory metadata
    # (`attachments.zip_safety.check_zip_safety`) before python-docx/
    # openpyxl/python-pptx ever decompresses a single entry. Independent of
    # max_attachment_document_bytes (that caps the *compressed* upload
    # size; a small, maliciously crafted archive can still expand to many
    # times that once decompressed).
    max_attachment_zip_uncompressed_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    max_attachment_zip_entries: int = Field(default=2_000, gt=0)
    # Hard wall-clock bound on one attachment's processor.process() call
    # (attachments.pipeline.process_attachment) -- a pathological but
    # otherwise-valid file (e.g. a PDF with a deeply nested object graph)
    # could otherwise tie up a request thread indefinitely; enforced via a
    # background-thread-plus-join timeout, the same mechanism
    # db/execution.py::_execute_with_timeout already uses for SQL queries.
    attachment_processing_timeout_seconds: float = Field(default=30.0, gt=0)
    attachment_storage_dir: Path = Path("./data/attachments")
    attachment_retention_hours: int = Field(default=24, gt=0)
    moderation_provider: Literal["azure_content_safety"] = "azure_content_safety"
    azure_content_safety_endpoint: str = ""
    azure_content_safety_key: SecretStr | None = None
    moderation_severity_threshold: int = Field(default=4, ge=0, le=7)
    moderation_blocklist_path: Path | None = None
    malware_scan_provider: Literal["clamav", "disabled"] = "disabled"
    clamav_host: str = "localhost"
    clamav_port: int = Field(default=3310, gt=0)
    malware_scan_timeout_seconds: float = Field(default=15.0, gt=0)
    moderation_store_connection_string: SecretStr | None = None
    moderation_store_odbc_driver: str = "ODBC Driver 17 for SQL Server"
    moderation_store_pool_size: int = Field(default=10, gt=0)
    moderation_store_max_overflow: int = Field(default=20, ge=0)
    moderation_store_pool_recycle_seconds: int = Field(default=1800, gt=0)
    media_ingest_workers: int = Field(default=4, gt=0)
    media_image_tile_threshold_px: int = Field(default=2048, gt=0)
    session_expensive_source_limit: int = Field(default=10, gt=0)
    session_expensive_source_window_seconds: float = Field(default=3600.0, gt=0)
    # `NoDecode`: see `ollama_allowed_models`'s own comment above -- without
    # it, a real `CORS_ALLOWED_ORIGINS=a,b` env var raised
    # `pydantic_settings.exceptions.SettingsError` before
    # `_split_cors_origins` below ever ran (only direct
    # `Settings(cors_allowed_origins=(...))` construction, as every existing
    # test used, ever worked) -- a real, previously-undiscovered bug found
    # and fixed while building `ollama_allowed_models` against the identical
    # pattern.
    cors_allowed_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        description=(
            "Origins allowed to call this API cross-origin (e.g. a React "
            "dev server at 'http://localhost:5173'). Empty by default -- a "
            "same-origin deployment (the built React app served by this "
            "same FastAPI app) needs no CORS at all. Comma-separated in "
            ".env, e.g. CORS_ALLOWED_ORIGINS=http://localhost:5173."
        ),
    )
    # Enterprise scalability/security assessment (2026-09-27): 0 (the
    # default) preserves this codebase's existing, deliberately-tested
    # posture of never trusting X-Forwarded-For/X-Real-IP
    # (tests/security/test_rate_limit_header_spoofing.py) -- correct for
    # today's single-instance, directly-exposed deployment. Once a real
    # reverse proxy/load balancer sits in front of this app (the target
    # architecture's own Phase 2+), `request.client.host` becomes the
    # proxy's own address for every caller, collapsing every distinct
    # client into one shared rate-limit bucket/audit-log identity -- a real
    # availability problem (innocent users rate-limited together), not
    # merely an inaccuracy. Set to the exact number of trusted reverse
    # proxies between the internet and this process (usually 1) to read the
    # correct hop from X-Forwarded-For instead -- see
    # `security.client_ip.resolve_client_ip`'s own docstring for why this
    # must be an exact hop *count*, never "trust the header if present",
    # and why a misconfigured count fails closed to the direct TCP peer
    # rather than trusting client-supplied data.
    trusted_proxy_count: int = Field(default=0, ge=0)
    # 2026 Phase 3 security review: on by default -- these headers are
    # cheap, have no functional downside for a normal browser session, and
    # (per this codebase's own "no single control is the final barrier"
    # posture) are a real, if secondary, layer against clickjacking, MIME-
    # sniffing, and script-injection XSS even where other controls already
    # exist. `content_security_policy` lets an operator override the
    # built-in default entirely (see `api/main.py`'s `_DEFAULT_CSP`) --
    # an escape hatch for a deployment this default doesn't fit, without
    # needing a code change; set it to the empty string to omit the CSP
    # header altogether while keeping the other headers.
    enable_security_headers: bool = True
    content_security_policy: str | None = None
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)  # 1 year
    project_root: Path = PROJECT_ROOT
    databases: tuple[DatabaseConnectionConfig, ...] = ()

    @field_validator("db_type", "web_search_provider", mode="before")
    @classmethod
    def _lowercase_strip(cls, value: object) -> object:
        """`db/connection.py::SUPPORTED_DB_TYPES` and
        `search/web_search.py::SUPPORTED_SEARCH_PROVIDERS` are both keyed by
        lowercase name -- applied here (rather than trusting every caller to
        lowercase their own `.env` value) so `DB_TYPE=MSSQL` and
        `DB_TYPE=mssql` behave identically. `log_redaction_level` gets the
        same treatment via its own validator below, since it also needs the
        lowercasing to happen *before* the `Literal["standard", "strict"]`
        match, not after.
        """
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_redaction_level", mode="before")
    @classmethod
    def _lowercase_strip_redaction_level(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accepts a comma-separated `.env` string (`DB_CONNECTIONS`'s own
        parsing convention) rather than requiring JSON-array syntax for a
        setting most users will only ever set to zero or one origin."""
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("ollama_allowed_models", mode="before")
    @classmethod
    def _split_ollama_allowed_models(cls, value: object) -> object:
        """Accepts a comma-separated `.env` string (`OLLAMA_ALLOWED_MODELS`),
        the same convention `_split_cors_origins` already establishes for
        `CORS_ALLOWED_ORIGINS` -- a small, human-editable list doesn't need
        JSON-array syntax. `_fill_default_ollama_allowed_models` below (a
        `model_validator(mode="after")`, so it runs once every field is in
        its final form) is what actually fills in the default starter set
        when this is left unset -- this validator only handles *parsing* a
        value that was actually provided.
        """
        if isinstance(value, str):
            return tuple(model.strip() for model in value.split(",") if model.strip())
        return value

    @field_validator("google_oauth_allowed_hosted_domains", mode="before")
    @classmethod
    def _split_google_oauth_allowed_hosted_domains(cls, value: object) -> object:
        """Same comma-separated `.env` convention as `_split_cors_origins`/
        `_split_ollama_allowed_models` above."""
        if isinstance(value, str):
            return tuple(domain.strip() for domain in value.split(",") if domain.strip())
        return value

    @field_validator("cors_allowed_origins", mode="after")
    @classmethod
    def _reject_wildcard_cors_origin(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """2026 Phase 3 security review: `api/main.py`'s `CORSMiddleware` is
        always added with `allow_credentials=True` (so the OIDC/static
        bearer token still reaches the API for the one legitimate
        cross-origin case, a separate dev-mode Vite server) -- combining
        that with a wildcard origin (`CORS_ALLOWED_ORIGINS=*`) is a
        real misconfiguration a browser itself refuses at runtime (the
        Fetch spec forbids `Access-Control-Allow-Origin: *` alongside
        `Access-Control-Allow-Credentials: true`), but this app's own
        "don't rely on a single control, including one browsers happen to
        enforce" posture means it shouldn't be reachable at all -- caught
        here, at config time, the same as `_validate_oidc_algorithms`
        catches `alg: none` before it can ever matter.
        """
        if "*" in value:
            raise ConfigurationError(
                "CORS_ALLOWED_ORIGINS must not include '*' -- this app's CORS "
                "middleware always sets allow_credentials=True, and a wildcard "
                "origin combined with credentials is rejected by browsers "
                "anyway (and should never be relied on to enforce that). List "
                "the specific origin(s) that need cross-origin access instead, "
                "e.g. CORS_ALLOWED_ORIGINS=http://localhost:5173."
            )
        return value

    @field_validator("chroma_persist_dir", mode="before")
    @classmethod
    def _resolve_chroma_dir(cls, value: object) -> Path:
        """Resolves a relative `CHROMA_PERSIST_DIR` against the project
        root rather than the process's current working directory -- applied
        to *any* value (the field's own default included, not just an
        explicit env var), so both stay consistent regardless of where the
        app happens to be launched from."""
        return _resolve_path(str(value))

    @field_validator("retrieval_knowledge_dir", mode="before")
    @classmethod
    def _resolve_retrieval_knowledge_dir(cls, value: object) -> Path:
        """Same project-root-relative resolution as `_resolve_chroma_dir`
        above, applied to `RETRIEVAL_KNOWLEDGE_DIR` for the same reason."""
        return _resolve_path(str(value))

    def __init__(self, **data: object) -> None:
        """Wraps construction so every failure -- Pydantic's own type
        coercion included, not just this file's custom validators below --
        raises `ConfigurationError`, matching this codebase's long-standing
        exception contract (see that class's docstring for every call site
        that catches it specifically).

        Safe to layer like this because a validator that raises
        `ConfigurationError` directly (not a bare `ValueError`) already
        propagates through Pydantic's validation machinery completely
        unwrapped -- verified empirically before relying on it, since
        Pydantic only intercepts `ValueError`/`TypeError`/`AssertionError`
        to build its own `ValidationError` out of them. This `__init__`
        override exists for the *other* case: a field failing Pydantic's
        own built-in type coercion (e.g. `MAX_RETRIES=abc`) raises
        `pydantic.ValidationError` directly, with no validator of this
        file's own in the call stack to intercept it -- so it's caught and
        translated here instead, the one place guaranteed to run for every
        construction path (`get_settings()` and every test that builds a
        `Settings(...)` directly).
        """
        try:
            # `BaseSettings.__init__`'s real signature is a long list of
            # `_env_file`/`_case_sensitive`/etc. special keyword-only
            # params, which mypy can't reconcile with a generic `**data:
            # object` forward -- this override's whole point is to accept
            # arbitrary field kwargs, so the mismatch is expected here.
            super().__init__(**data)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise ConfigurationError(_format_validation_error(exc)) from exc

    @model_validator(mode="after")
    def _validate_cost_threshold_ordering(self) -> Settings:
        """`COST_MODERATE_ROW_THRESHOLD` must be strictly less than
        `COST_HIGH_ROW_THRESHOLD` -- otherwise a query is never classified
        "moderate", only "low" or "high". A cross-field rule Pydantic's
        per-field `Field(gt=0)` constraints can't express on their own."""
        if self.cost_moderate_row_threshold >= self.cost_high_row_threshold:
            raise ConfigurationError(
                f"COST_MODERATE_ROW_THRESHOLD ({self.cost_moderate_row_threshold}) must be "
                f"strictly less than COST_HIGH_ROW_THRESHOLD ({self.cost_high_row_threshold}) "
                f"-- otherwise a query is never classified 'moderate', only 'low' or 'high'. "
                f"Fix both in .env."
            )
        return self

    @model_validator(mode="after")
    def _fill_default_database(self) -> Settings:
        """Ensures `databases` always has >=1 entry.

        `object.__setattr__` is required because `Settings` is frozen
        (immutable after construction is the point -- see the class
        docstring); a `model_validator(mode="after")` is the one place a
        frozen Pydantic model's own fields may still be set, exactly for
        this kind of post-construction normalization (mirrors the
        `object.__setattr__` escape hatch frozen dataclasses' own
        `__post_init__` used before this migration).
        """
        if not self.databases:
            # No DB_CONNECTIONS configured (or a Settings(...) built directly,
            # e.g. by a test, without passing databases=) -- fall back to a
            # single "default" connection mirroring this instance's own flat
            # db_* fields, so `settings.databases` always has >=1 entry and
            # multi-database-aware code never needs a special single-DB case.
            object.__setattr__(
                self,
                "databases",
                (
                    DatabaseConnectionConfig(
                        name="default",
                        db_type=self.db_type,
                        db_host=self.db_host,
                        db_port=self.db_port,
                        db_name=self.db_name,
                        db_user=self.db_user,
                        db_password=self.db_password,
                        db_connection_string=self.db_connection_string,
                        db_schema=self.db_schema,
                        db_odbc_driver=self.db_odbc_driver,
                    ),
                ),
            )
        return self

    @model_validator(mode="after")
    def _fill_default_ollama_allowed_models(self) -> Settings:
        """Normalizes `ollama_allowed_models` into its final, authoritative form.

        Three cases, in order:
          1. `ollama_model_selection_enabled` is False -- selection is
             hard-disabled, so the allowed set collapses to exactly
             `(ollama_model,)` regardless of whatever `OLLAMA_ALLOWED_MODELS`
             says. This is the kill switch: `agent.model_registry` never
             needs its own separate "is selection enabled" branch, since an
             empty-of-alternatives allowed set already produces the right
             behavior (only the default is ever valid).
          2. `ollama_allowed_models` was left unset -- falls back to
             `_DEFAULT_OLLAMA_ALLOWED_MODELS` above, unioned with
             `ollama_model` (order-preserving, default first) so a fresh
             clone gets a useful picker with zero `.env` edits.
          3. `ollama_allowed_models` was explicitly set -- used as-is, except
             `ollama_model` is unioned in if the operator's own list omitted
             it, since the configured default must always remain selectable.

        Same `object.__setattr__` escape hatch as `_fill_default_database`
        above -- `Settings` is frozen, and a `model_validator(mode="after")`
        is the one place that's allowed for post-construction normalization.
        """
        if not self.ollama_model_selection_enabled:
            object.__setattr__(self, "ollama_allowed_models", (self.ollama_model,))
            return self

        base = self.ollama_allowed_models or _DEFAULT_OLLAMA_ALLOWED_MODELS
        if self.ollama_model not in base:
            base = (self.ollama_model, *base)
        object.__setattr__(self, "ollama_allowed_models", base)
        return self

    @property
    def auth_mode(self) -> Literal["none", "static_token", "oidc", "local"]:
        """The single dispatch point `api.auth.verify_api_key` (and
        everything downstream of it, e.g. `agent.authz`) uses to decide how
        a request is authenticated -- computed from the more granular
        fields above rather than stored as its own field, so there is only
        ever one source of truth for "is OIDC configured" instead of a
        second flag that could disagree with `oidc_issuer`/`oidc_audience`.

        Reports the *highest-priority* configured mechanism, not "the only
        one" -- `verify_api_key` actually tries local -> OIDC -> static
        token in sequence, and more than one may be configured
        simultaneously (see `docs/AUTHENTICATION.md`'s "Combining modes").
        "local" (this app's own self-hosted accounts, `identity/`) takes
        priority in this summary because it's the newest, most complete
        option; "none" only when nothing at all is configured, which
        `_require_identity_in_production` below refuses to allow outside
        `environment="development"`.
        """
        if self.local_auth_enabled:
            return "local"
        if self.oidc_issuer is not None:
            return "oidc"
        if self.api_auth_token is not None:
            return "static_token"
        return "none"

    @model_validator(mode="after")
    def _validate_oidc_requires_audience(self) -> Settings:
        """`OIDC_ISSUER` without `OIDC_AUDIENCE` is a real, dangerous
        misconfiguration, not just an incomplete one: without an audience
        check, this app would accept *any* validly-signed token from that
        issuer, including one minted for a completely different
        application that happens to share the same identity provider --
        exactly the kind of cross-application token confusion OIDC's
        audience claim exists to prevent. Caught here, at startup, rather
        than left to `security/oidc.py` to reject per-request.
        """
        if self.oidc_issuer is not None and not self.oidc_audience:
            raise ConfigurationError(
                "OIDC_ISSUER is set but OIDC_AUDIENCE is not -- refusing to start. "
                "Validating a token's signature and issuer without also checking its "
                "audience would accept a token minted for a different application at "
                "the same identity provider. Set OIDC_AUDIENCE in .env."
            )
        return self

    @model_validator(mode="after")
    def _validate_oidc_algorithms(self) -> Settings:
        """`OIDC_ALGORITHMS` must be non-empty and must never contain
        `"none"` -- the classic JWT "alg confusion" bypass (a token whose
        header claims `alg: none`, which some naive verifiers then skip
        signature checking for entirely). `security/oidc.py` also never
        reads the algorithm from the token itself, as defense in depth, but
        this config-level guard exists so a typo'd/misguided `.env` value
        can't silently reintroduce the same class of bug.
        """
        if not self.oidc_algorithms:
            raise ConfigurationError(
                "OIDC_ALGORITHMS must list at least one signing algorithm (e.g. RS256)."
            )
        if any(alg.strip().lower() == "none" for alg in self.oidc_algorithms):
            raise ConfigurationError(
                "OIDC_ALGORITHMS must never include 'none' -- this would accept an "
                "unsigned token from anyone (the classic JWT 'alg confusion' bypass)."
            )
        return self

    @model_validator(mode="after")
    def _require_identity_in_production(self) -> Settings:
        """`ENVIRONMENT=production` with no authentication configured at
        all refuses to start, rather than silently serving every request
        -- including `POST /execute`, document delete, and paid media
        generation -- to any network caller with no credentials, which is
        exactly what `auth_mode == "none"` means (see `api/auth.py`'s own
        docstring). `ENVIRONMENT` defaults to "development", where this
        never fires, so a fresh clone / local dev setup is completely
        unaffected -- this only ever triggers for a deployment that
        explicitly declared itself production-facing.
        """
        if self.environment == "production" and self.auth_mode == "none":
            raise ConfigurationError(
                "ENVIRONMENT=production requires authentication to be configured -- "
                "refusing to start with no identity check of any kind, which would "
                "leave every endpoint (including SQL execution, document deletion, "
                "and paid media generation) open to any network caller. Set either "
                "OIDC_ISSUER (+ OIDC_AUDIENCE) for production-grade OIDC/JWT "
                "authentication, or API_AUTH_TOKEN for a lighter-weight static shared "
                "secret -- see docs/AUTHENTICATION.md."
            )
        return self

    @model_validator(mode="after")
    def _require_malware_scanning_in_production(self) -> Settings:
        """`ENVIRONMENT=production` with at least one untrusted-file-accepting
        feature enabled (chat attachments, document RAG, policy RAG, media
        search) but `MALWARE_SCAN_PROVIDER=disabled` refuses to start --
        the same "fail closed at startup, not silently at request time"
        posture `_require_identity_in_production` above already has for
        authentication. `malware_scan_provider` defaults to `"disabled"`
        deliberately (see that field's own docstring: no scanning
        capability existed before it was added, so defaulting it "on"
        would break every existing deployment without a ClamAV daemon) --
        this validator is what turns "off by default" into "must be
        explicitly turned on before this goes to production," rather than
        leaving that as an easy-to-miss deployment checklist item.
        `ENVIRONMENT` defaults to "development", where this never fires.
        """
        untrusted_upload_features_enabled = (
            self.enable_chat_attachments
            or self.enable_document_rag
            or self.enable_policy_rag
            or self.enable_media_search
        )
        if (
            self.environment == "production"
            and untrusted_upload_features_enabled
            and self.malware_scan_provider == "disabled"
        ):
            raise ConfigurationError(
                "ENVIRONMENT=production has at least one file-upload-accepting feature "
                "enabled (chat attachments, document/policy RAG, or media search) but "
                "MALWARE_SCAN_PROVIDER=disabled -- refusing to start with untrusted "
                "uploads reaching parsers/OCR/vision models with no malware scanning "
                "at all. Set MALWARE_SCAN_PROVIDER=clamav (and CLAMAV_HOST/"
                "CLAMAV_PORT) before deploying to production, or set "
                "ENABLE_CHAT_ATTACHMENTS/ENABLE_DOCUMENT_RAG/ENABLE_POLICY_RAG/"
                "ENABLE_MEDIA_SEARCH=false if this deployment doesn't need file uploads."
            )
        return self

    @model_validator(mode="after")
    def _validate_local_auth_requires_database(self) -> Settings:
        """`LOCAL_AUTH_ENABLED=true` without `AUTH_DATABASE_URL` is a
        real, not just incomplete, misconfiguration -- there would be
        nowhere to actually store a registered user, a session, or a
        single row of activity history. Caught here, at startup, the same
        way `_validate_oidc_requires_audience` catches its own analogous
        gap rather than letting every register/login call fail one at a
        time at first use.
        """
        if self.local_auth_enabled and self.auth_database_url is None:
            raise ConfigurationError(
                "LOCAL_AUTH_ENABLED=true requires AUTH_DATABASE_URL to be set -- "
                "self-hosted user accounts need a dedicated identity database to "
                "store users/sessions/history in. Set AUTH_DATABASE_URL in .env, or "
                "leave LOCAL_AUTH_ENABLED unset/false."
            )
        return self

    @model_validator(mode="after")
    def _validate_google_signin_requires_local_auth(self) -> Settings:
        """`GOOGLE_OAUTH_CLIENT_ID` is meaningless without `LOCAL_AUTH_ENABLED`
        -- a Google-authenticated user is provisioned as a real row in
        `identity.users` (see `identity/repositories/external_identities.py`),
        so there is nowhere to create or look up that user without the
        identity database this app's own local accounts already require.
        Caught here at startup, the same way `_validate_local_auth_requires_database`
        catches its own analogous gap, rather than letting the first real
        Google sign-in attempt fail with a confusing error.
        """
        if self.google_oauth_client_id is not None and not self.local_auth_enabled:
            raise ConfigurationError(
                "GOOGLE_OAUTH_CLIENT_ID is set but LOCAL_AUTH_ENABLED is not -- "
                "refusing to start. Google sign-in creates/links a row in this "
                "app's own identity database, which requires LOCAL_AUTH_ENABLED=true "
                "and AUTH_DATABASE_URL to be set. Enable local auth first, or leave "
                "GOOGLE_OAUTH_CLIENT_ID unset."
            )
        return self

    @model_validator(mode="after")
    def _validate_local_auth_signing_material(self) -> Settings:
        """Whichever `jwt_algorithm` is configured needs its own signing
        material actually present -- an `HS256` deployment with no
        `JWT_SECRET_KEY` (or a trivially short one) would either crash on
        first login or, worse, sign tokens with a guessable/empty secret;
        an `RS256`/`ES256` deployment with no key-file paths configured has
        no way to sign anything at all. Only enforced when
        `local_auth_enabled` -- this app must never demand JWT signing
        material just to boot with local auth off, the same "never breaks
        a deployment that isn't using this feature" posture every other
        optional-subsystem validator in this file already has.

        The `HS256` minimum-length check (32 characters, 256 bits) is a
        floor, not a strength guarantee -- pick a genuinely random secret in
        practice, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
        """
        if not self.local_auth_enabled:
            return self
        if self.jwt_algorithm == "HS256":
            secret = self.jwt_secret_key.get_secret_value() if self.jwt_secret_key else ""
            if len(secret) < 32:
                raise ConfigurationError(
                    "LOCAL_AUTH_ENABLED=true with JWT_ALGORITHM=HS256 requires a "
                    "JWT_SECRET_KEY of at least 32 characters. Generate one with "
                    '`python -c "import secrets; print(secrets.token_urlsafe(48))"` '
                    "and set it in .env."
                )
        else:
            if self.jwt_private_key_path is None or self.jwt_public_key_path is None:
                raise ConfigurationError(
                    f"LOCAL_AUTH_ENABLED=true with JWT_ALGORITHM={self.jwt_algorithm} requires "
                    "both JWT_PRIVATE_KEY_PATH and JWT_PUBLIC_KEY_PATH to be set in .env."
                )
        return self

    @model_validator(mode="after")
    def _validate_bootstrap_admin_requires_credentials(self) -> Settings:
        """`BOOTSTRAP_ADMIN_ENABLED=true` with no email/password configured
        would let `identity.bootstrap.bootstrap_admin_user` be invoked with
        nothing to actually create -- caught here rather than as a
        confusing failure inside the bootstrap script itself."""
        if self.bootstrap_admin_enabled and (
            not self.bootstrap_admin_email or self.bootstrap_admin_password is None
        ):
            raise ConfigurationError(
                "BOOTSTRAP_ADMIN_ENABLED=true requires both BOOTSTRAP_ADMIN_EMAIL and "
                "BOOTSTRAP_ADMIN_PASSWORD to be set in .env."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings instance, built from environment variables.

    Cached with `lru_cache` so repeated calls (e.g. from every agent node) do
    not re-parse the environment -- built once per process, same as
    `api/main.py`'s other process-lifetime singletons (the DB engines, the
    Ollama client, the compiled agent graph).

    Every field is read from the environment automatically by
    `pydantic_settings.BaseSettings` (case-insensitively, so `OLLAMA_HOST`
    in `.env` fills the `ollama_host` field with no extra wiring here) --
    the one field that still needs explicit help is `databases`, since its
    source (`DB_CONNECTIONS` + dynamically-prefixed `DB_<NAME>_*` vars) is
    a genuinely dynamic set of env-var names no static field declaration
    can express.

    Raises:
        ConfigurationError: if a present-but-malformed value is found (e.g.
            DB_PORT is set but isn't a number). Missing values are not an
            error here -- `db/connection.py` validates *combinations* of
            db_* fields (e.g. "DB_TYPE is set but DB_HOST is missing") at
            the point something actually tries to connect, since plenty of
            non-DB functionality (linting, non-DB tests, etc.) shouldn't
            require a fully-configured database connection just to import
            this module.
    """
    return Settings(databases=_parse_named_connections())


class _JsonLogFormatter(logging.Formatter):
    """Renders one JSON object per log line -- `Settings.log_format="json"`.

    Enterprise scalability/security assessment (2026-09-27): the existing
    default text format (`"%(asctime)s | %(levelname)-8s | ..."`) is fine
    for a human watching a terminal, but a real log-aggregation pipeline
    (ELK, CloudWatch, Datadog, Loki) has to regex-parse it back apart --
    exactly the class of fragility structured logging exists to avoid. This
    is purely a rendering choice: every existing `logger.info(...)`/
    `security.audit_log.log_security_event(...)` call site is completely
    unchanged, and the *default* (`log_format="text"`) still uses the
    original format string -- this formatter is only ever attached when an
    operator explicitly opts in.

    Deliberately does not log the raw `%(message)s` args separately, secret
    values, or anything beyond what the existing text formatter already
    rendered -- see this codebase's own "never log connection strings,
    passwords, or full result rows" rule (CLAUDE.md's Coding standards);
    this formatter changes *shape*, not *content*, so every existing
    redaction (`security.redaction`) that already ran before a message
    reached the logger still applies identically.
    """

    def format(self, record: logging.LogRecord) -> str:
        # "-" is `CorrelationIdLogFilter`'s own placeholder for "no request
        # in flight" (chosen there so the *text* format's column stays a
        # stable width) -- rendered here as a real JSON `null` instead, so
        # a log-pipeline query for "no correlation id" is a natural `IS
        # NULL`, not a string-literal match on an internal placeholder.
        correlation_id = getattr(record, "correlation_id", None)
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": None if correlation_id in (None, "-") else correlation_id,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once, in a format useful for terminal debugging.

    Called from entry points (scripts, api/main.py, tests) rather than at
    import time, so importing this module never has the side effect of
    reconfiguring a caller's logging setup.

    Every handler on the root logger gets `security.audit_log
    .CorrelationIdLogFilter` attached, and the format includes
    `correlation_id` -- this is what makes a request's correlation ID show
    up on *every* log line (agent nodes, RAG, DB, external-call modules),
    not just the dedicated `security.audit` event stream. See that filter's
    docstring for the Phase 3 observability gap this closes.

    `Settings.log_format` picks the rendering: "text" (the default) is this
    codebase's original terminal-friendly pipe-delimited format, unchanged;
    "json" (enterprise scalability/security assessment, 2026-09-27) renders
    each line as one JSON object instead (`_JsonLogFormatter`) for a real
    log pipeline to ingest. Same underlying log records either way -- this
    only changes how they're rendered.
    """
    from security.audit_log import CorrelationIdLogFilter

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    handler = logging.StreamHandler()
    if settings.log_format == "json":
        handler.setFormatter(_JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | correlation_id=%(correlation_id)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    logging.basicConfig(
        level=getattr(logging, resolved_level, logging.INFO),
        handlers=[handler],
        force=True,
    )
    correlation_filter = CorrelationIdLogFilter()
    for configured_handler in logging.getLogger().handlers:
        configured_handler.addFilter(correlation_filter)
