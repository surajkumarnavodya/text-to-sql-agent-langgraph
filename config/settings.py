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

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

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
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        llm_max_tokens: Max tokens the LLM may generate per call (sandboxing).
        insight_max_tokens: Max tokens the LLM may generate for the post-query
            plain-English insight sentence (see `agent.llm_client.
            generate_insight_from_llm`) -- deliberately small and separate
            from `llm_max_tokens`, since this is 1-2 sentences, not SQL.
        max_question_length: Maximum accepted raw length (characters) of a
            user's typed question, enforced by `agent.input_guard.
            check_input` before any normalization or LLM call -- see
            CLAUDE.md's adversarial-input-hardening notes.
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
        api_auth_token: Optional bearer token required on every `api/`
            request (`Authorization: Bearer <token>`) when set. None
            (default, unset in `.env`) means the API has no auth check of
            its own -- a deliberate, documented "lightweight hook, not a
            full auth system" posture (see `docs/DEPLOYMENT.md`): anything
            beyond local/trusted-network use should sit behind a real
            authenticating reverse proxy regardless of whether this is set.
            A `SecretStr` for the same reason `db_password` is.
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

    db_type: str = ""
    db_host: str | None = None
    db_port: int | None = None
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_connection_string: SecretStr | None = None
    db_schema: str | None = None
    db_odbc_driver: str = "ODBC Driver 17 for SQL Server"

    chroma_persist_dir: Path = Path("./embeddings/.chroma")
    chroma_collection_name: str = "schema_ddl"
    embedding_model_name: str = "all-MiniLM-L6-v2"
    schema_top_k: int = 4
    max_retries: int = Field(default=3, gt=0)
    complex_query_max_retry_bonus: int = Field(default=2, ge=0)
    max_result_rows: int = Field(default=1000, gt=0)
    query_timeout_seconds: int = Field(default=15, gt=0)
    llm_max_tokens: int = Field(default=1024, gt=0)
    insight_max_tokens: int = Field(default=120, gt=0)
    max_question_length: int = Field(default=500, gt=0)
    question_rate_limit_per_minute: int = Field(default=10, gt=0)
    llm_call_rate_limit_per_minute: int = Field(default=20, gt=0)
    api_action_rate_limit_per_minute: int = Field(default=20, gt=0)
    cost_estimation_enabled: bool = True
    cost_estimation_timeout_seconds: int = Field(default=3, gt=0)
    cost_moderate_row_threshold: int = Field(default=50_000, gt=0)
    cost_high_row_threshold: int = Field(default=1_000_000, gt=0)
    log_level: str = "INFO"
    log_redaction_level: Literal["standard", "strict"] = "standard"
    enable_multi_source_router: bool = False
    enable_query_planning: bool = True
    query_plan_max_tokens: int = Field(default=300, gt=0)
    sql_review_max_tokens: int = Field(default=200, gt=0)
    enable_golden_examples: bool = True
    golden_examples_top_k: int = Field(default=3, gt=0)
    golden_examples_min_similarity: float = Field(default=0.75, ge=0.0, le=1.0)
    api_auth_token: SecretStr | None = None
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
    enable_voice_mode: bool = True
    stt_model_size: str = "base"
    stt_device: Literal["cpu", "cuda"] = "cpu"
    stt_vocabulary_max_chars: int = Field(default=200, gt=0)
    voice_max_upload_mb: int = Field(default=10, gt=0)
    voice_max_duration_seconds: int = Field(default=30, gt=0)
    tts_voice: str = "en_US-lessac-medium"
    tts_voice_model_path: Path | None = None
    session_expensive_source_limit: int = Field(default=10, gt=0)
    session_expensive_source_window_seconds: float = Field(default=3600.0, gt=0)
    cors_allowed_origins: tuple[str, ...] = Field(
        default=(),
        description=(
            "Origins allowed to call this API cross-origin (e.g. a React "
            "dev server at 'http://localhost:5173'). Empty by default -- a "
            "same-origin deployment (the built React app served by this "
            "same FastAPI app) needs no CORS at all. Comma-separated in "
            ".env, e.g. CORS_ALLOWED_ORIGINS=http://localhost:5173."
        ),
    )
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

    @field_validator("chroma_persist_dir", mode="before")
    @classmethod
    def _resolve_chroma_dir(cls, value: object) -> Path:
        """Resolves a relative `CHROMA_PERSIST_DIR` against the project
        root rather than the process's current working directory -- applied
        to *any* value (the field's own default included, not just an
        explicit env var), so both stay consistent regardless of where the
        app happens to be launched from."""
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


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once, in a format useful for terminal debugging.

    Called from entry points (scripts, api/main.py, tests) rather than at
    import time, so importing this module never has the side effect of
    reconfiguring a caller's logging setup.
    """
    resolved_level = (level or get_settings().log_level).upper()
    logging.basicConfig(
        level=getattr(logging, resolved_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
