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

Design note on startup: the app refuses to render the chat UI at all if the
configured database can't be reached (see the connection check right after
page setup below) -- better a clear "fix your .env" screen than a crash or
a silently broken agent deeper in the flow.
"""

from __future__ import annotations

import logging
import sys
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
from agent.nodes import execute_readonly_sql
from agent.sql_validator import enforce_row_limit, validate_sql
from config.settings import configure_logging, get_settings
from db.connection import get_read_only_engine, get_sqlglot_dialect, test_connection
from db.schema_introspection import TableSchemaInfo, introspect_schema
from db.value_sampling import attach_sample_values
from embeddings.schema_indexer import build_index
from ui.column_formatting import format_column_label, get_display_columns, get_key_column_names

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
def _startup_connection_check():
    """Runs once per process. The uncached `test_connection()` call is used
    by the sidebar's "Test Connection" button so it always re-checks live."""
    return test_connection(settings)


# --------------------------------------------------------------------------
# Startup gate: refuse to render the app at all if the DB is unreachable
# --------------------------------------------------------------------------

_startup_check = _startup_connection_check()
if not _startup_check.success:
    st.title("⚠️ Database Connection Required")
    st.error(f"Could not reach database: {_startup_check.message}")
    st.markdown(
        "Check your `.env` file — see README's **Connecting to your database** "
        "section. You can also run `python scripts\\test_db_connection.py` from "
        "a terminal for a more detailed diagnostic."
    )
    st.stop()


# --------------------------------------------------------------------------
# Schema introspection + embedding index (once per process; "Refresh Schema"
# button below clears and re-runs this)
# --------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _initialize_schema() -> list[TableSchemaInfo]:
    engine = get_read_only_engine(settings)
    tables = introspect_schema(engine, schema=settings.db_schema)
    sampled_tables = attach_sample_values(engine, tables)
    build_index(sampled_tables, settings=settings, fingerprint_tables=tables)
    return sampled_tables


discovered_tables = _initialize_schema()


@st.cache_data(show_spinner=False)
def _run_readonly_query(
    sql: str, query_timeout_seconds: int, max_result_rows: int
) -> tuple[list[str], list[tuple]]:
    """Executes already-validated, row-limited SQL, cached by exact SQL text.

    Caching here means asking to re-run the identical SQL (e.g. re-clicking
    Confirm and Run without edits) doesn't hit the database again.
    """
    return execute_readonly_sql(sql, query_timeout_seconds, max_result_rows)


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------

# Types (documented here since Streamlit's session_state attributes can't
# carry inline type annotations -- mypy rejects `obj.attr: T = ...` for any
# object other than `self`):
#   chat_history: list[dict]                          [{"role": ..., "content": ...}]
#   nl_question_cache: dict[str, dict]                 question text -> agent final state
#   current_agent_state: dict | None
#   editable_sql: str
#   display_result: tuple[list[str], list[tuple]] | None
#   display_error: str | None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "nl_question_cache" not in st.session_state:
    # Skips redundant LLM calls for repeated identical questions within a
    # session (see CLAUDE.md "Caching").
    st.session_state.nl_question_cache = {}
if "current_agent_state" not in st.session_state:
    st.session_state.current_agent_state = None
if "editable_sql" not in st.session_state:
    st.session_state.editable_sql = ""
if "display_result" not in st.session_state:
    st.session_state.display_result = None
if "display_error" not in st.session_state:
    st.session_state.display_error = None


# --------------------------------------------------------------------------
# Sidebar: Database Connection
# --------------------------------------------------------------------------

with st.sidebar:
    st.subheader("🔌 Database Connection")
    st.markdown(f"**Type:** `{settings.db_type or 'not set'}`")
    st.markdown(f"**Database:** `{settings.db_name or 'not set'}`")
    if settings.db_user:
        st.markdown(f"**User:** `{_mask_username(settings.db_user)}`")
    if settings.db_schema:
        st.markdown(f"**Schema:** `{settings.db_schema}`")
    if _startup_check.db_version:
        st.caption(_startup_check.db_version)

    if st.button("🔌 Test Connection", use_container_width=True):
        with st.spinner("Testing connection..."):
            result = test_connection(settings)  # deliberately uncached -- always a fresh check
        if result.success:
            st.success(f"Connected.{f' {result.db_version}' if result.db_version else ''}")
        else:
            st.error(result.message)

    if st.button("🔄 Refresh Schema", use_container_width=True):
        with st.spinner("Re-introspecting schema and refreshing embeddings..."):
            _initialize_schema.clear()
            refreshed_tables = _initialize_schema()
        st.success(f"Schema refreshed: {len(refreshed_tables)} table(s).")
        discovered_tables = refreshed_tables

    with st.expander(f"📋 Discovered tables ({len(discovered_tables)})", expanded=False):
        for table in discovered_tables:
            st.markdown(f"**{table.table_name}**")
            column_summary = ", ".join(f"{c.name} ({c.type})" for c in table.columns)
            st.caption(column_summary)


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
st.markdown(
    f"""
    <div class="tsql-badges">
        <span class="tsql-badge">🧠 {settings.ollama_model}</span>
        <span class="tsql-badge">🗃️ {settings.db_type} · {settings.db_name}</span>
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
        cache_key = question.strip().lower()
        if cache_key in st.session_state.nl_question_cache:
            logger.info("Serving question from in-session cache: %r", question)
            st.caption("(served from this session's cache -- no LLM call made)")
            final_state = st.session_state.nl_question_cache[cache_key]
        else:
            try:
                with st.spinner("Retrieving schema, generating SQL, self-correcting if needed..."):
                    final_state = run_agent(question)
                st.session_state.nl_question_cache[cache_key] = final_state
            except SchemaRetrievalError as exc:
                final_state = {"status": "failed", "error_history": [str(exc)]}

        st.session_state.current_agent_state = final_state
        st.session_state.display_result = None
        st.session_state.display_error = None

        if final_state.get("status") == "failed":
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

if state and state.get("status") != "failed":
    with st.expander("🔍 Retrieved schema context", expanded=False):
        tables = state.get("schema_tables", [])
        if not tables:
            st.write("No schema context retrieved.")
        for table in tables:
            st.markdown(f"**{table['table_name']}** (similarity: {table['similarity_score']:.3f})")
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

    confirm_clicked = st.button("▶ Confirm and Run", type="primary")

    if confirm_clicked:
        dialect = get_sqlglot_dialect(settings.db_type)
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
            try:
                with st.spinner("Running query..."):
                    columns, rows = _run_readonly_query(
                        safe_sql, settings.query_timeout_seconds, settings.max_result_rows
                    )
                st.session_state.display_result = (columns, rows)
                st.session_state.display_error = None
            except (SQLAlchemyError, TimeoutError) as exc:
                st.session_state.display_result = None
                st.session_state.display_error = f"Execution failed: {exc}"

    if st.session_state.display_error:
        st.error(st.session_state.display_error)

    if st.session_state.display_result:
        columns, rows = st.session_state.display_result
        df = pd.DataFrame(rows, columns=columns)

        st.subheader(f"📊 Results ({len(df)} row{'s' if len(df) != 1 else ''})")

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
