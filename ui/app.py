"""Streamlit UI for the Text-to-SQL agent, connected to a real, configured database.

Design note on "manual confirm before execution": the LangGraph agent
(`agent/graph.py`) executes candidate SQL internally as part of its own
self-correction loop (generate -> validate -> execute -> retry on error) --
that's what lets it catch runtime errors like an unknown column and fix
them. Those internal executions are safe (read-only engine, SELECT-only,
row-capped, timed out) but their *results* are never shown here. Nothing is
rendered to the user until they explicitly click "Confirm and Run", and that
button always re-validates and re-executes whatever SQL text is currently in
the editable box -- including if the user edited it -- rather than trusting
the agent's last internal result. This is the "SQL is untrusted output,
always" rule from CLAUDE.md applied at the UI layer.

Design note on startup: the app refuses to render the chat UI at all only if
*every* configured database (see `Settings.databases` -- a plain
single-database `.env` has exactly one, named "default") is unreachable
(see the connection check right after page setup below) -- better a clear
"fix your .env" screen than a crash or a silently broken agent deeper in
the flow. With multiple databases configured, one being down doesn't block
the others.

Design note on multi-database routing: with more than one database
configured, each question is auto-routed to whichever configured database
looks like the best match (`embeddings.retriever.select_database`, called
from `agent.nodes.retrieve_schema_node`) -- there is no manual
database-picker UI. `state["selected_database"]` records which database a
given agent run targeted, and "Confirm and Run" below deliberately
validates/executes against that same database, not a re-guessed one.
"""

from __future__ import annotations

import html
import logging
import sys
import uuid
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

# `streamlit run ui/app.py` does not guarantee the repo root is on sys.path
# (unlike running as an installed package), so top-level imports like
# `agent.graph` would otherwise fail. Insert it explicitly, matching the
# same pattern used in scripts/build_embeddings.py and scripts/test_db_connection.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.exceptions import SchemaRetrievalError
from agent.graph import run_agent
from agent.rate_limit import QUESTION_LIMIT_MESSAGE, SlidingWindowRateLimiter
from agent.sql_validator import enforce_row_limit, validate_sql
from config.settings import configure_logging, get_settings
from db.connection import (
    ConnectionTestResult,
    check_write_privileges,
    get_connection,
    get_read_only_engine,
    get_sqlglot_dialect,
    test_connection,
)
from db.execution import execute_readonly_sql
from db.schema_introspection import TableSchemaInfo
from embeddings.schema_indexer import refresh_all_schema_indexes
from security.redaction import redact_secrets
from ui.column_formatting import (
    escape_markdown,
    format_column_label,
    get_display_columns,
    get_key_column_names,
)
from ui.session_history import (
    QueryHistoryEntry,
    append_entry,
    build_conversation_history,
    clear_history,
    new_history_entry,
    replace_entry,
    status_label,
    with_confirmed_error,
    with_confirmed_result,
)

configure_logging()
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Text-to-SQL", page_icon="🗄️", layout="wide")


def _inject_custom_css() -> None:
    """Light, modern, responsive visual polish -- CSS only, no behavior change.

    Targets Streamlit's stable `data-testid` hooks (1.41.x) rather than
    generated class names, so it doesn't silently break on a Streamlit
    version bump. Colors intentionally stay light per project preference --
    see `.streamlit/config.toml` for the widget-level (button/input) theme
    this complements.
    """
    st.markdown(
        """
        <style>
        :root {
            --tsql-primary: #4f46e5;
            --tsql-primary-light: #eef2ff;
            --tsql-border: #e2e8f0;
            --tsql-card: #ffffff;
            --tsql-text: #1e293b;
            --tsql-muted: #64748b;
            --tsql-radius: 14px;
            --tsql-shadow: 0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04);
        }

        .stApp {
            background: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%);
        }

        .block-container {
            padding-top: 1.75rem;
            padding-bottom: 3rem;
            max-width: 1200px;
        }

        /* Hero header */
        .tsql-hero {
            display: flex;
            align-items: center;
            gap: 1rem;
            background: var(--tsql-card);
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            padding: 1.25rem 1.5rem;
            box-shadow: var(--tsql-shadow);
            margin-bottom: 1rem;
        }
        .tsql-hero-icon { font-size: 2.25rem; line-height: 1; }
        .tsql-hero h1 {
            margin: 0;
            font-size: 1.6rem;
            font-weight: 700;
            color: var(--tsql-text);
        }
        .tsql-hero p {
            margin: 0.2rem 0 0;
            color: var(--tsql-muted);
            font-size: 0.92rem;
        }

        .tsql-badges {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-bottom: 1.5rem;
        }
        .tsql-badge {
            background: var(--tsql-primary-light);
            color: var(--tsql-primary);
            border: 1px solid #c7d2fe;
            border-radius: 999px;
            padding: 0.3rem 0.75rem;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
        }

        /* Sidebar */
        [data-testid="stSidebar"] {
            background: var(--tsql-card);
            border-right: 1px solid var(--tsql-border);
        }

        /* Buttons */
        .stButton > button {
            border-radius: 10px;
            font-weight: 600;
            box-shadow: var(--tsql-shadow);
            transition: transform 0.05s ease, box-shadow 0.15s ease;
        }
        .stButton > button:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 10px rgba(79, 70, 229, 0.18);
        }

        /* Chat messages */
        [data-testid="stChatMessage"] {
            background: var(--tsql-card);
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            padding: 0.75rem 1rem;
            box-shadow: var(--tsql-shadow);
            margin-bottom: 0.6rem;
        }

        /* SQL editor */
        [data-testid="stTextArea"] textarea {
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.85rem;
            background: #f8fafc;
            border-radius: 10px;
        }

        /* Expanders */
        [data-testid="stExpander"] {
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            box-shadow: var(--tsql-shadow);
            background: var(--tsql-card);
            overflow: hidden;
        }

        /* DataFrame */
        [data-testid="stDataFrame"] {
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            overflow: hidden;
            box-shadow: var(--tsql-shadow);
        }

        /* Alerts */
        [data-testid="stAlert"] {
            border-radius: 10px;
        }

        /* AI insight callout -- deliberately distinct from both the chat
           bubbles and the results table, so it reads as an interpretation
           layered on top of the data rather than part of the data itself. */
        .tsql-insight {
            display: flex;
            align-items: flex-start;
            gap: 0.6rem;
            background: var(--tsql-primary-light);
            border: 1px solid #c7d2fe;
            border-left: 4px solid var(--tsql-primary);
            border-radius: 10px;
            padding: 0.75rem 1rem;
            margin: 0.75rem 0 1.25rem;
            font-size: 0.92rem;
            color: var(--tsql-text);
        }
        .tsql-insight-icon { font-size: 1.1rem; line-height: 1.4; }
        .tsql-insight-label {
            font-weight: 700;
            color: var(--tsql-primary);
            margin-right: 0.35rem;
        }

        /* Responsive tweaks for narrow / mobile viewports */
        @media (max-width: 768px) {
            .block-container { padding-left: 0.75rem; padding-right: 0.75rem; }
            .tsql-hero { flex-direction: column; align-items: flex-start; text-align: left; }
            .tsql-hero h1 { font-size: 1.3rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


_inject_custom_css()


# --------------------------------------------------------------------------
# Cached resources (one-time setup, per Streamlit session/process)
# --------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _warm_settings():
    """Loads settings once; surfaces .env misconfiguration immediately."""
    return get_settings()


settings = _warm_settings()


def _mask_username(user: str) -> str:
    """Shows only the first and last character of a username, e.g. `j***n`."""
    if len(user) <= 2:
        return "*" * len(user)
    return f"{user[0]}{'*' * (len(user) - 2)}{user[-1]}"


@st.cache_resource(show_spinner=False)
def _startup_connection_checks() -> dict[str, ConnectionTestResult]:
    """Runs once per process, for every configured database (see
    `Settings.databases`) -- a plain single-database `.env` has exactly one
    entry, named "default". The uncached `test_connection()` call is used
    by the sidebar's "Test Connection" button so it always re-checks live."""
    return {config.name: test_connection(config) for config in settings.databases}


# --------------------------------------------------------------------------
# Startup gate: refuse to render the app at all only if *every* configured
# database is unreachable -- with multiple databases configured, one being
# down shouldn't block the others (auto-routing simply won't usefully route
# to it; the existing execution-error retry path surfaces that if it does).
# --------------------------------------------------------------------------

_startup_checks = _startup_connection_checks()
if all(not result.success for result in _startup_checks.values()):
    st.title("⚠️ Database Connection Required")
    st.error("Could not reach any configured database:")
    for name, result in _startup_checks.items():
        st.error(f"**{name}**: {result.message}")
    st.markdown(
        "Check your `.env` file — see README's **Connecting to your database** "
        "section. You can also run `python scripts\\test_db_connection.py` from "
        "a terminal for a more detailed diagnostic."
    )
    st.stop()


# --------------------------------------------------------------------------
# Write-privilege check (once per process, per configured database) --
# best-effort, warning-only, see db.connection.check_write_privileges'
# docstring (item A: least-privilege design). Never blocks startup, unlike
# the connection check above.
# --------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _startup_write_privilege_checks():
    return {
        config.name: check_write_privileges(get_read_only_engine(config), config)
        for config in settings.databases
    }


_write_privilege_checks = _startup_write_privilege_checks()


# --------------------------------------------------------------------------
# Schema introspection + embedding index (once per process; "Refresh Schema"
# button below clears and re-runs this) -- one index per configured database.
# --------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _initialize_schema() -> dict[str, list[TableSchemaInfo]]:
    return refresh_all_schema_indexes(settings)


discovered_tables_by_db = _initialize_schema()
discovered_tables = [table for tables in discovered_tables_by_db.values() for table in tables]


@st.cache_data(show_spinner=False)
def _run_readonly_query(
    sql: str,
    query_timeout_seconds: int,
    max_result_rows: int,
    session_token: str,
    db_name: str,
    _engine=None,
) -> tuple[list[str], list[tuple]]:
    """Executes already-validated, row-limited SQL, cached by exact SQL text.

    Caching here means asking to re-run the identical SQL (e.g. re-clicking
    Confirm and Run without edits) doesn't hit the database again.

    `session_token` (see the `session_token` session-state entry below) is
    part of the cache key but never used inside the function body --
    `st.cache_data` is a **process-wide** cache in Streamlit, not
    per-session, despite every other piece of state in this file being
    session-scoped. Without a session-scoped component in the key, a future
    multi-user deployment would let User B, submitting/editing SQL that
    happens to match User A's exact SQL text, receive User A's cached
    *result rows* without their own query ever executing -- a real
    cross-user data leak this app's actual single-user target never
    surfaced, but the fix costs nothing today and closes it structurally
    for whenever that target changes.

    `db_name` is likewise part of the cache key -- identical SQL text
    executed against two different configured databases must never share a
    cached result. `_engine` (leading underscore excludes it from
    Streamlit's cache-key hashing -- a SQLAlchemy `Engine` isn't a stable
    hash key) is the actual connection resolved for `db_name`; `db_name`
    alone carries the cache-identity role `_engine` can't.
    """
    return execute_readonly_sql(sql, query_timeout_seconds, max_result_rows, engine=_engine)


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------

# Types (documented here since Streamlit's session_state attributes can't
# carry inline type annotations -- mypy rejects `obj.attr: T = ...` for any
# object other than `self`):
#   chat_history: list[dict]                          [{"role": ..., "content": ...}]
#   nl_question_cache: dict[tuple, dict]               (question text, prior-question tuple) -> agent final state
#   current_agent_state: dict | None
#   editable_sql: str
#   display_result: tuple[list[str], list[tuple]] | None
#   display_error: str | None
#   query_history: list[QueryHistoryEntry]             every question asked this session, oldest first
#   active_entry_id: str | None                        which query_history entry the SQL box / Confirm-and-Run applies to
#   display_sql: str | None                            the exact SQL text that produced display_result, if any
#   enable_insight: bool                                whether to generate a plain-English insight (sidebar toggle)
#   question_rate_limiter: SlidingWindowRateLimiter     per-session cap on question submissions (see agent/rate_limit.py)
#   session_token: str                                  random per-session id, scopes _run_readonly_query's cache (see its docstring)
if "session_token" not in st.session_state:
    st.session_state.session_token = uuid.uuid4().hex
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "nl_question_cache" not in st.session_state:
    # Skips redundant LLM calls for repeated identical questions within a
    # session (see CLAUDE.md "Caching"). Keyed on (question, prior-questions)
    # rather than question text alone -- the same follow-up phrasing ("now
    # break that down by month") can resolve to a different query depending
    # on what it's following up on, so the cache must not conflate those.
    st.session_state.nl_question_cache = {}
if "current_agent_state" not in st.session_state:
    st.session_state.current_agent_state = None
if "editable_sql" not in st.session_state:
    st.session_state.editable_sql = ""
if "display_result" not in st.session_state:
    st.session_state.display_result = None
if "display_error" not in st.session_state:
    st.session_state.display_error = None
if "query_history" not in st.session_state:
    st.session_state.query_history = []
if "active_entry_id" not in st.session_state:
    st.session_state.active_entry_id = None
if "display_sql" not in st.session_state:
    st.session_state.display_sql = None
if "enable_insight" not in st.session_state:
    st.session_state.enable_insight = True  # default ON -- see sidebar toggle
if "question_rate_limiter" not in st.session_state:
    # Genuinely per-session (unlike the process-wide LLM-call limiter
    # generate_sql_node enforces) -- lives in st.session_state itself, one
    # instance per browser session, exactly matching "max N questions per
    # minute per session." See agent/rate_limit.py's module docstring for
    # why the two limiters have different scopes.
    st.session_state.question_rate_limiter = SlidingWindowRateLimiter(
        max_events=settings.question_rate_limit_per_minute,
        window_seconds=60.0,
        name="question_submissions",
    )


# --------------------------------------------------------------------------
# Sidebar: Database Connection
# --------------------------------------------------------------------------

_multi_db = len(settings.databases) > 1

with st.sidebar:
    st.subheader("🔌 Database Connections" if _multi_db else "🔌 Database Connection")
    for config in settings.databases:
        check = _startup_checks.get(config.name)
        status_icon = "✅" if check and check.success else "❌"
        write_check = _write_privilege_checks.get(config.name)
        with st.container(border=_multi_db):
            if _multi_db:
                st.markdown(f"{status_icon} **{config.name}**")
            st.markdown(f"**Type:** `{config.db_type or 'not set'}`")
            st.markdown(f"**Database:** `{config.db_name or 'not set'}`")
            if config.db_user:
                st.markdown(f"**User:** `{_mask_username(config.db_user)}`")
            if config.db_schema:
                st.markdown(f"**Schema:** `{config.db_schema}`")
            if check and check.db_version:
                st.caption(check.db_version)
            if check and not check.success:
                st.caption(f"⚠️ {check.message}")
            if write_check and write_check.checked and write_check.has_write_privileges:
                st.warning(f"⚠️ {write_check.message}")

    if _multi_db:
        last_state = st.session_state.current_agent_state
        last_routed_db = last_state.get("selected_database") if last_state else None
        if last_routed_db:
            st.caption(f"🧭 Last question routed to: **{last_routed_db}**")

    if st.button("🔌 Test Connection", use_container_width=True):
        with st.spinner("Testing connection(s)..."):
            # deliberately uncached -- always a fresh check
            results = {config.name: test_connection(config) for config in settings.databases}
        for name, result in results.items():
            prefix = f"{name}: " if _multi_db else ""
            if result.success:
                st.success(
                    f"{prefix}Connected.{f' {result.db_version}' if result.db_version else ''}"
                )
            else:
                st.error(f"{prefix}{result.message}")

    if st.button("🔄 Refresh Schema", use_container_width=True):
        with st.spinner("Re-introspecting schema and refreshing embeddings..."):
            # st.cache_resource's real runtime object exposes .clear() (see
            # Streamlit's caching docs), but its type stub types a decorated
            # function as a plain Callable preserving the wrapped signature,
            # which doesn't declare .clear() -- a stub gap, not an app bug.
            _initialize_schema.clear()  # type: ignore[attr-defined]
            refreshed_by_db = _initialize_schema()
        total_tables = sum(len(tables) for tables in refreshed_by_db.values())
        st.success(
            f"Schema refreshed: {total_tables} table(s) across "
            f"{len(refreshed_by_db)} database(s)."
        )
        discovered_tables_by_db = refreshed_by_db
        discovered_tables = [table for tables in refreshed_by_db.values() for table in tables]

    with st.expander(f"📋 Discovered tables ({len(discovered_tables)})", expanded=False):
        for db_name, db_tables in discovered_tables_by_db.items():
            if _multi_db:
                st.markdown(f"**Database: {db_name}**")
            for discovered_table in db_tables:
                st.markdown(f"**{escape_markdown(discovered_table.table_name)}**")
                column_summary = ", ".join(
                    f"{escape_markdown(c.name)} ({escape_markdown(c.type)})"
                    for c in discovered_table.columns
                )
                st.caption(column_summary)

    st.divider()
    st.subheader("⚙️ Options")
    st.session_state.enable_insight = st.checkbox(
        "💡 Generate AI insight",
        value=st.session_state.enable_insight,
        help=(
            "After a successful query, generate a short plain-English sentence "
            "summarizing the result. Strictly grounded in the returned data -- "
            "turn off for the cleanest possible demo view, or if it's ever inaccurate."
        ),
    )

    st.divider()
    st.subheader("📜 History")
    st.caption("This session only -- cleared on refresh or app restart.")

    history: list[QueryHistoryEntry] = st.session_state.query_history
    if not history:
        st.caption("No questions asked yet.")
    else:
        for entry in reversed(history):
            icon, label = status_label(entry)
            with st.container(border=True):
                st.markdown(f"{icon} **{label}** -- {entry.timestamp.strftime('%H:%M:%S')}")
                st.caption(entry.question)
                view_col, rerun_col = st.columns(2)
                with view_col:
                    if st.button("👁 View", key=f"view_{entry.entry_id}", use_container_width=True):
                        st.session_state.active_entry_id = entry.entry_id
                        st.session_state.current_agent_state = entry.final_state
                        st.session_state.editable_sql = entry.sql or ""
                        if entry.confirmed_columns is not None:
                            st.session_state.display_result = (
                                entry.confirmed_columns,
                                entry.confirmed_rows,
                            )
                            st.session_state.display_error = None
                            st.session_state.display_sql = entry.sql
                        elif entry.confirmed_error is not None:
                            st.session_state.display_result = None
                            st.session_state.display_error = entry.confirmed_error
                            st.session_state.display_sql = None
                        else:
                            st.session_state.display_result = None
                            st.session_state.display_error = None
                            st.session_state.display_sql = None
                with rerun_col:
                    if st.button(
                        "🔄 Re-run", key=f"rerun_{entry.entry_id}", use_container_width=True
                    ):
                        # A re-run is a fresh submission for rate-limiting
                        # purposes too -- it costs exactly as much LLM/DB
                        # work as typing the question again would.
                        rerun_limit = st.session_state.question_rate_limiter.check()
                        if not rerun_limit.allowed:
                            st.warning(QUESTION_LIMIT_MESSAGE)
                        else:
                            with st.spinner("Re-running against the live database..."):
                                prior_context = build_conversation_history(history)
                                rerun_state = run_agent(
                                    entry.question,
                                    prior_context,
                                    enable_insight=st.session_state.enable_insight,
                                )
                            new_entry = new_history_entry(entry.question, rerun_state)
                            st.session_state.query_history = append_entry(history, new_entry)
                            st.session_state.active_entry_id = new_entry.entry_id
                            st.session_state.current_agent_state = rerun_state
                            st.session_state.editable_sql = rerun_state.get("sql", "") or ""
                            st.session_state.display_result = None
                            st.session_state.display_error = None
                            st.session_state.display_sql = None

        if st.button("🗑️ Clear history", use_container_width=True):
            st.session_state.query_history = clear_history()
            st.session_state.active_entry_id = None
            st.session_state.current_agent_state = None
            st.session_state.editable_sql = ""
            st.session_state.display_result = None
            st.session_state.display_error = None
            st.session_state.display_sql = None


# --------------------------------------------------------------------------
# Chart auto-selection
# --------------------------------------------------------------------------


def _build_chart(df: pd.DataFrame):
    """Best-effort bar/line chart from a query result, or None if not chartable.

    Heuristic: needs at least one numeric column and one non-numeric (label)
    column. If the label column looks like a date/time, use a line chart
    (trend over time); otherwise a bar chart (comparison across categories).
    Anything else (all-numeric, all-text, too many columns) is left as a
    table only -- a wrong guess at a chart is worse than no chart.
    """
    if df.empty or len(df.columns) < 2:
        return None

    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    label_cols = [c for c in df.columns if c not in numeric_cols]
    if not numeric_cols or not label_cols:
        return None

    label_col = label_cols[0]
    value_col = numeric_cols[0]

    is_date_like = "date" in label_col.lower() or pd.api.types.is_datetime64_any_dtype(
        df[label_col]
    )
    if is_date_like:
        try:
            plot_df = df.copy()
            plot_df[label_col] = pd.to_datetime(plot_df[label_col])
            plot_df = plot_df.sort_values(label_col)
        except (ValueError, TypeError):
            plot_df = df
        return px.line(plot_df, x=label_col, y=value_col, markers=True)

    # Cap category count so a bar chart doesn't render hundreds of bars.
    plot_df = df.nlargest(30, value_col) if len(df) > 30 else df
    return px.bar(plot_df, x=label_col, y=value_col)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

st.markdown(
    """
    <div class="tsql-hero">
        <div class="tsql-hero-icon">🗄️</div>
        <div>
            <h1>Text-to-SQL Dashboard</h1>
            <p>Ask a question in plain English — get validated, read-only SQL, a live result table, and an auto-picked chart.</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)
_db_badge = (
    f"🗃️ {len(settings.databases)} databases configured"
    if _multi_db
    else f"🗃️ {settings.db_type} · {settings.db_name}"
)
st.markdown(
    f"""
    <div class="tsql-badges">
        <span class="tsql-badge">🧠 {settings.ollama_model}</span>
        <span class="tsql-badge">{_db_badge}</span>
        <span class="tsql-badge">📋 {len(discovered_tables)} table(s) discovered</span>
    </div>
    """,
    unsafe_allow_html=True,
)

for message in st.session_state.chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

question = st.chat_input("Ask a question about your data...")

if question:
    st.session_state.chat_history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        # Checked before anything else -- including the dup-question cache
        # lookup below, which is a separate, independent mechanism (see
        # agent/rate_limit.py's module docstring). A denial here means no
        # run_agent call, no query_history entry: nothing was actually
        # attempted, so there's nothing to record beyond the chat message.
        rate_limit_result = st.session_state.question_rate_limiter.check()
        if not rate_limit_result.allowed:
            st.warning(QUESTION_LIMIT_MESSAGE)
            st.session_state.chat_history.append(
                {"role": "assistant", "content": QUESTION_LIMIT_MESSAGE}
            )
        else:
            # Built from the *current* history, before this turn is appended
            # to it -- a question is never its own follow-up context (see
            # Part 3's "one source of truth" note in ui/session_history.py).
            prior_context = build_conversation_history(st.session_state.query_history)
            # enable_insight is part of the cache key too -- toggling it and
            # re-asking the identical question must not silently serve a
            # cached answer generated under the opposite setting.
            cache_key = (
                question.strip().lower(),
                tuple(e["question"] for e in prior_context),
                st.session_state.enable_insight,
            )
            if cache_key in st.session_state.nl_question_cache:
                logger.info("Serving question from in-session cache: %r", question)
                st.caption("(served from this session's cache -- no LLM call made)")
                final_state = st.session_state.nl_question_cache[cache_key]
            else:
                try:
                    with st.spinner(
                        "Retrieving schema, generating SQL, self-correcting if needed..."
                    ):
                        final_state = run_agent(
                            question, prior_context, enable_insight=st.session_state.enable_insight
                        )
                    st.session_state.nl_question_cache[cache_key] = final_state
                except SchemaRetrievalError as exc:
                    final_state = {"status": "failed", "error_history": [str(exc)]}

            new_entry = new_history_entry(question, final_state)
            st.session_state.query_history = append_entry(st.session_state.query_history, new_entry)
            st.session_state.active_entry_id = new_entry.entry_id
            st.session_state.current_agent_state = final_state
            st.session_state.display_result = None
            st.session_state.display_error = None
            st.session_state.display_sql = None

            if final_state.get("status") == "rate_limited":
                # Distinct from "rejected": a temporary, systemic load
                # condition, not a judgment about the question. Reached via
                # the LLM-call limiter tripping mid-retry-loop (see
                # generate_sql_node) -- rarer than the submission-level
                # check above, but must be just as visibly communicated.
                message = final_state.get("rate_limit_message") or QUESTION_LIMIT_MESSAGE
                st.warning(message)
                st.session_state.chat_history.append({"role": "assistant", "content": message})
            elif final_state.get("status") == "rejected":
                # Standardized, non-technical message only -- never the raw
                # rejection_reason or which pattern matched (CLAUDE.md's Part 3:
                # a rejection must not confirm to an attacker exactly what was
                # detected, and must not alarm a legitimate user over an
                # unusual phrasing). st.warning rather than st.error: this is a
                # normal "I can't help with that" outcome, not a system fault.
                message = final_state.get("rejection_message") or (
                    "I couldn't process that question. Try rephrasing it as a "
                    "direct question about your data."
                )
                st.warning(message)
                st.session_state.chat_history.append({"role": "assistant", "content": message})
            elif final_state.get("status") == "needs_clarification":
                message = (
                    final_state.get("clarification_message") or "Could not tell what was asked."
                )
                st.warning(f"Needs clarification: {message}")
                st.session_state.chat_history.append(
                    {"role": "assistant", "content": f"Needs clarification: {message}"}
                )
            elif final_state.get("status") == "failed":
                explanation = (
                    final_state.get("failure_explanation")
                    or (final_state.get("error_history") or ["Unknown error."])[-1]
                )
                last_sql = final_state.get("sql")
                st.error(f"Agent could not produce a working query: {explanation}")
                if last_sql:
                    st.caption("Last SQL attempted:")
                    st.code(last_sql, language="sql")
                st.session_state.chat_history.append(
                    {"role": "assistant", "content": f"Failed: {explanation}"}
                )
            else:
                if final_state.get("followup_classification") == "followup":
                    resolved_against = final_state.get("followup_resolved_against") or {}
                    st.caption(f"↪ Following up on: “{resolved_against.get('question', '')}”")
                st.session_state.editable_sql = final_state.get("sql", "")
                retries = final_state.get("retry_count", 0)
                summary = (
                    f"Generated SQL after {retries} retr{'y' if retries == 1 else 'ies'}."
                    if retries
                    else "Generated SQL."
                )
                st.markdown(summary)
                st.session_state.chat_history.append({"role": "assistant", "content": summary})


# --------------------------------------------------------------------------
# Retry timeline: one line per attempt, so the self-correction loop (and the
# specific reason each attempt failed) is visible/demoable, not just logged
# to the terminal. Shown for both the succeeded and failed cases, which is
# why it's rendered outside the "status != failed" gate below.
# --------------------------------------------------------------------------

_OUTCOME_ICONS = {
    "succeeded": "✅",
    "safety_violation": "\U0001f6d1",
    "timeout": "⏱️",
    "high_cost": "\U0001f4c8",
    "rate_limited": "\U0001f40c",
    "off_topic": "\U0001f6ab",
}


def _render_attempt_timeline(attempt_history: list[dict]) -> None:
    if not attempt_history:
        return
    with st.expander(f"🔁 Retry timeline ({len(attempt_history)} attempt(s))", expanded=False):
        for record in attempt_history:
            icon = _OUTCOME_ICONS.get(record["outcome"], "\U0001f501")
            label = record["outcome"].replace("_", " ")
            suffix = " -- will retry" if record.get("will_retry") else ""
            st.markdown(f"{icon} **Attempt {record['attempt']}**: {label}{suffix}")
            if record.get("sql"):
                st.code(record["sql"], language="sql")
            if record.get("error"):
                st.caption(record["error"])


# --------------------------------------------------------------------------
# Current turn: schema context, editable SQL, confirm-and-run, results, chart
# --------------------------------------------------------------------------

state = st.session_state.current_agent_state
if state:
    _render_attempt_timeline(state.get("attempt_history", []))

if state and state.get("status") not in (
    "failed",
    "needs_clarification",
    "rejected",
    "rate_limited",
):
    if _multi_db and state.get("selected_database"):
        st.caption(f"🧭 Routed to database: **{state['selected_database']}**")

    with st.expander("🔍 Retrieved schema context", expanded=False):
        tables = state.get("schema_tables", [])
        if not tables:
            st.write("No schema context retrieved.")
        for table in tables:
            st.markdown(
                f"**{escape_markdown(table['table_name'])}** "
                f"(similarity: {table['similarity_score']:.3f})"
            )
            st.code(table["ddl"], language="sql")

    st.subheader("🛠️ Generated SQL")
    st.caption(
        "Edit if needed -- it will be re-validated and re-run when you click Confirm and Run."
    )
    st.session_state.editable_sql = st.text_area(
        "SQL",
        value=st.session_state.editable_sql,
        height=140,
        label_visibility="collapsed",
    )

    # Proactive "this may take a moment" notice from the moderate-cost path
    # in estimate_query_cost_node (see db/query_cost.py) -- shown before the
    # user clicks Confirm and Run, not after, so they aren't left wondering
    # if the app has frozen. Only shown while the SQL box still matches what
    # the notice was computed for -- same "don't show something stale after
    # an edit" rule as the AI insight callout below.
    cost_notice = state.get("cost_notice")
    if cost_notice and st.session_state.editable_sql == state.get("sql"):
        st.caption(f"⏳ {cost_notice}")

    confirm_clicked = st.button("▶ Confirm and Run", type="primary")

    if confirm_clicked:
        # Validate/execute against the *same* database the agent generated
        # this SQL for -- not a re-guessed one. `state["selected_database"]`
        # is set once by retrieve_schema_node and carried through the whole
        # run; falling back to the first configured connection only covers
        # a pre-migration history entry with no selected_database recorded.
        selected_db_name = state.get("selected_database") or settings.databases[0].name
        db_config = get_connection(settings, selected_db_name)
        dialect = get_sqlglot_dialect(db_config.db_type)
        sql_to_run = st.session_state.editable_sql
        validation = validate_sql(sql_to_run, dialect=dialect)
        if not validation.is_valid:
            st.session_state.display_result = None
            st.session_state.display_error = f"Rejected: {validation.error}"
        else:
            assert validation.normalized_sql is not None  # guaranteed when is_valid is True
            safe_sql = enforce_row_limit(
                validation.normalized_sql, settings.max_result_rows, dialect=dialect
            )
            st.session_state.editable_sql = safe_sql
            active_id = st.session_state.active_entry_id
            active_entry = next(
                (e for e in st.session_state.query_history if e.entry_id == active_id), None
            )
            try:
                with st.spinner("Running query..."):
                    columns, rows = _run_readonly_query(
                        safe_sql,
                        settings.query_timeout_seconds,
                        settings.max_result_rows,
                        st.session_state.session_token,
                        selected_db_name,
                        _engine=get_read_only_engine(db_config),
                    )
                st.session_state.display_result = (columns, rows)
                st.session_state.display_error = None
                st.session_state.display_sql = safe_sql
                if active_entry is not None:
                    updated_entry = with_confirmed_result(active_entry, columns, rows)
                    st.session_state.query_history = replace_entry(
                        st.session_state.query_history, active_id, updated_entry
                    )
            except (SQLAlchemyError, TimeoutError) as exc:
                # Redacted before display/storage: on some drivers/failure
                # modes a mid-query connection failure can surface the full
                # connection string (including the password) verbatim in
                # the exception text -- see security.redaction's docstring.
                # Passed db_config (not the global settings) so the exact
                # password redacted is the one actually in play for this
                # connection, which can differ once multiple databases are
                # configured -- see redact_secrets' docstring.
                safe_detail = redact_secrets(str(exc), db_config)
                st.session_state.display_result = None
                st.session_state.display_error = f"Execution failed: {safe_detail}"
                st.session_state.display_sql = None
                if active_entry is not None:
                    updated_entry = with_confirmed_error(active_entry, safe_detail)
                    st.session_state.query_history = replace_entry(
                        st.session_state.query_history, active_id, updated_entry
                    )

    if st.session_state.display_error:
        st.error(st.session_state.display_error)

    if st.session_state.display_result:
        columns, rows = st.session_state.display_result
        df = pd.DataFrame(rows, columns=columns)

        st.subheader(f"📊 Results ({len(df)} row{'s' if len(df) != 1 else ''})")

        # Detection-only signal from execute_sql_node (see AgentState.
        # low_confidence_notice's docstring) -- only shown when the
        # currently displayed (confirmed) result came from running exactly
        # the SQL the notice was computed for, same staleness guard as the
        # AI insight below.
        low_confidence_notice = state.get("low_confidence_notice")
        if low_confidence_notice and st.session_state.display_sql == state.get("sql"):
            st.warning(f"⚠️ {low_confidence_notice}")

        # Only shown when the currently displayed (confirmed) result was
        # produced by running exactly the SQL the insight was generated
        # for -- if the user edited the SQL box before clicking Confirm and
        # Run, the insight would describe a different query than what's on
        # screen, so it's correctly withheld rather than shown stale. This
        # is what keeps the insight a narrative layer on top of already-
        # correct results, never something that could contradict them.
        insight_text = state.get("insight")
        insight_matches_displayed_sql = (
            st.session_state.display_sql is not None
            and st.session_state.display_sql == state.get("sql")
        )
        if insight_text and insight_matches_displayed_sql:
            st.markdown(
                f"""
                <div class="tsql-insight">
                    <span class="tsql-insight-icon">💡</span>
                    <span><span class="tsql-insight-label">AI insight</span>{html.escape(insight_text)}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

        key_columns = get_key_column_names(discovered_tables)
        display_columns, used_fallback = get_display_columns(list(df.columns), key_columns)
        chart = _build_chart(df)  # unchanged logic, always against the full raw df

        col_a, col_b = st.columns(2)
        with col_a:
            show_technical = st.checkbox(
                "Show technical columns",
                value=False,
                help="Show raw column names and surrogate key (ID/Key) columns.",
            )
        with col_b:
            show_chart = st.checkbox(
                "Show chart",
                value=False,
                disabled=chart is None,
                help=(
                    "Plot this result."
                    if chart is not None
                    else "No suitable chart for this result shape."
                ),
            )

        if show_technical:
            display_df = df
        else:
            display_df = df[display_columns].rename(columns=format_column_label)
            if used_fallback:
                st.caption("Only identifier columns were returned.")

        # st.dataframe virtualizes rendering internally, so this stays
        # responsive even at the MAX_RESULT_ROWS cap without manual paging.
        st.dataframe(
            display_df, use_container_width=True, height=min(400, 40 + 28 * len(display_df))
        )

        if show_chart and chart is not None:
            st.subheader("📈 Chart")
            st.plotly_chart(chart, use_container_width=True)
