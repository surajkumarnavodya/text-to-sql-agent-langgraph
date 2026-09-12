"""Dark/light theme system shared by every Streamlit page in `ui/`.

A single source of truth so `ui/app.py` and `ui/pages/1_Knowledge_Sources.py`
(Streamlit multipage apps don't share a layout wrapper -- each script runs
independently) both get the same visual treatment and the same toggle,
rather than two copies of the same CSS drifting apart.

Toggle mechanism: `st.session_state.theme_mode` plus server-side
conditional CSS, not a client-side JS switch. Streamlit reruns the whole
script on every widget interaction, so flipping the toggle just changes
which variable set `inject_theme_css()` renders into the `<style>` block on
the next run -- no JavaScript needed. `session_state` already persists
across this app's multipage navigation within one browser session, so a
choice made on one page carries over to the other automatically; each page
still renders its own `render_theme_toggle()` for direct-landing
reachability.

`.streamlit/config.toml`'s `[theme]` block is loaded once at server
startup and can't be flipped per-session at runtime, so this deliberately
doesn't try to fight it -- instead, the CSS below overrides Streamlit's
actual rendered surfaces directly (the same technique the project's
original light-only styling already used), which reaches everything that
matters except Streamlit's own built-in chrome (the top header bar, its
native hamburger "Settings" menu) -- a real, narrow, disclosed gap, not
silently pretended away.
"""

from __future__ import annotations

from typing import Literal

import streamlit as st

ThemeMode = Literal["light", "dark"]

DEFAULT_THEME_MODE: ThemeMode = "light"

# Every value the CSS template below substitutes in. "light" is exactly
# today's palette, unchanged -- flipping the toggle must never alter the
# default experience for anyone who doesn't touch it. "dark" is a deep
# violet/near-black glass palette (inspired by the reference mockup this
# was built from), keeping the same #4f46e5 indigo accent as "light" for
# brand continuity across both themes rather than picking a different hue
# per theme.
THEME_VARIABLES: dict[ThemeMode, dict[str, str]] = {
    "light": {
        "primary": "#4f46e5",
        "primary-light": "#eef2ff",
        "primary-border": "#c7d2fe",
        "border": "#e2e8f0",
        "card": "#ffffff",
        "text": "#1e293b",
        "muted": "#64748b",
        "radius": "14px",
        "shadow": "0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04)",
        "bg-start": "#f8fafc",
        "bg-end": "#eef2ff",
        "sidebar-bg": "#ffffff",
        "input-bg": "#f8fafc",
        "header-bg": "#f8fafc",
    },
    "dark": {
        "primary": "#818cf8",
        "primary-light": "rgba(99, 102, 241, 0.18)",
        "primary-border": "rgba(129, 140, 248, 0.4)",
        "border": "rgba(255, 255, 255, 0.09)",
        "card": "rgba(30, 23, 57, 0.65)",
        "text": "#f1f5f9",
        "muted": "#a1a1c2",
        "radius": "14px",
        "shadow": "0 4px 20px rgba(0, 0, 0, 0.45), 0 1px 3px rgba(0, 0, 0, 0.35)",
        "bg-start": "#0f0a1f",
        "bg-end": "#241a3d",
        "sidebar-bg": "#150f28",
        "input-bg": "rgba(255, 255, 255, 0.05)",
        "header-bg": "#150f28",
    },
}


def get_theme_mode() -> ThemeMode:
    """Returns the current theme, defaulting to `DEFAULT_THEME_MODE`.

    Reads (never writes) `st.session_state.theme_mode` -- the toggle
    widget itself (`render_theme_toggle`) owns writing it.
    """
    mode = st.session_state.get("theme_mode", DEFAULT_THEME_MODE)
    return "dark" if mode == "dark" else "light"


def render_theme_toggle() -> None:
    """Renders the dark-mode switch and updates `st.session_state.theme_mode`.

    Call once near the top of each page's own `with st.sidebar:` block, so
    it's reachable regardless of which page a session starts on.
    """
    is_dark = st.toggle("🌙 Dark mode", value=get_theme_mode() == "dark", key="theme_toggle")
    st.session_state.theme_mode = "dark" if is_dark else "light"


def inject_theme_css() -> None:
    """Emits the `<style>` block for the current theme (see `get_theme_mode`).

    Call once per page, right after `st.set_page_config`. CSS only, no
    behavior change -- targets Streamlit's stable `data-testid` hooks
    (1.41.x) rather than generated class names, so it doesn't silently
    break on a Streamlit version bump.
    """
    v = THEME_VARIABLES[get_theme_mode()]
    st.markdown(
        f"""
        <style>
        :root {{
            --tsql-primary: {v["primary"]};
            --tsql-primary-light: {v["primary-light"]};
            --tsql-primary-border: {v["primary-border"]};
            --tsql-border: {v["border"]};
            --tsql-card: {v["card"]};
            --tsql-text: {v["text"]};
            --tsql-muted: {v["muted"]};
            --tsql-radius: {v["radius"]};
            --tsql-shadow: {v["shadow"]};
        }}

        .stApp {{
            background: linear-gradient(180deg, {v["bg-start"]} 0%, {v["bg-end"]} 100%);
        }}

        [data-testid="stHeader"] {{
            background: {v["header-bg"]};
        }}

        [data-testid="stAppViewContainer"], [data-testid="stMain"],
        [data-testid="stMarkdownContainer"] {{
            color: var(--tsql-text);
        }}

        .block-container {{
            padding-top: 1.75rem;
            padding-bottom: 3rem;
            max-width: 1200px;
        }}

        /* Hero header */
        .tsql-hero {{
            display: flex;
            align-items: center;
            gap: 1rem;
            background: var(--tsql-card);
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            padding: 1.25rem 1.5rem;
            box-shadow: var(--tsql-shadow);
            margin-bottom: 1rem;
            backdrop-filter: blur(12px);
        }}
        .tsql-hero-icon {{ font-size: 2.25rem; line-height: 1; }}
        .tsql-hero h1 {{
            margin: 0;
            font-size: 1.6rem;
            font-weight: 700;
            color: var(--tsql-text);
        }}
        .tsql-hero p {{
            margin: 0.2rem 0 0;
            color: var(--tsql-muted);
            font-size: 0.92rem;
        }}

        .tsql-badges {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-bottom: 1.5rem;
        }}
        .tsql-badge {{
            background: var(--tsql-primary-light);
            color: var(--tsql-primary);
            border: 1px solid var(--tsql-primary-border);
            border-radius: 999px;
            padding: 0.3rem 0.75rem;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
        }}

        /* Sidebar */
        [data-testid="stSidebar"] {{
            background: {v["sidebar-bg"]};
            border-right: 1px solid var(--tsql-border);
        }}
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {{
            color: var(--tsql-text);
        }}
        /* Bordered st.container(border=True) blocks -- connection-status
           cards and history-entry cards in the sidebar both get the same
           glass-card treatment this way, without either needing its own
           wrapper markup/CSS class. */
        [data-testid="stVerticalBlockBorderWrapper"] {{
            background: var(--tsql-card);
            border-color: var(--tsql-border) !important;
            border-radius: var(--tsql-radius);
            backdrop-filter: blur(12px);
        }}

        /* Buttons */
        .stButton > button {{
            border-radius: 10px;
            font-weight: 600;
            box-shadow: var(--tsql-shadow);
            transition: transform 0.05s ease, box-shadow 0.15s ease;
        }}
        .stButton > button:hover {{
            transform: translateY(-1px);
            box-shadow: 0 4px 10px rgba(79, 70, 229, 0.18);
        }}

        /* Chat messages */
        [data-testid="stChatMessage"] {{
            background: var(--tsql-card);
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            padding: 0.75rem 1rem;
            box-shadow: var(--tsql-shadow);
            margin-bottom: 0.6rem;
            backdrop-filter: blur(12px);
        }}

        /* SQL editor */
        [data-testid="stTextArea"] textarea {{
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.85rem;
            background: {v["input-bg"]};
            color: var(--tsql-text);
            border-radius: 10px;
        }}

        /* Expanders */
        [data-testid="stExpander"] {{
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            box-shadow: var(--tsql-shadow);
            background: var(--tsql-card);
            overflow: hidden;
        }}

        /* DataFrame */
        [data-testid="stDataFrame"] {{
            border: 1px solid var(--tsql-border);
            border-radius: var(--tsql-radius);
            overflow: hidden;
            box-shadow: var(--tsql-shadow);
        }}

        /* Alerts */
        [data-testid="stAlert"] {{
            border-radius: 10px;
        }}

        /* Dark-mode toggle switch */
        [data-testid="stToggle"] label p {{
            color: var(--tsql-text);
        }}

        /* AI insight callout -- deliberately distinct from both the chat
           bubbles and the results table, so it reads as an interpretation
           layered on top of the data rather than part of the data itself. */
        .tsql-insight {{
            display: flex;
            align-items: flex-start;
            gap: 0.6rem;
            background: var(--tsql-primary-light);
            border: 1px solid var(--tsql-primary-border);
            border-left: 4px solid var(--tsql-primary);
            border-radius: 10px;
            padding: 0.75rem 1rem;
            margin: 0.75rem 0 1.25rem;
            font-size: 0.92rem;
            color: var(--tsql-text);
        }}
        .tsql-insight-icon {{ font-size: 1.1rem; line-height: 1.4; }}
        .tsql-insight-label {{
            font-weight: 700;
            color: var(--tsql-primary);
            margin-right: 0.35rem;
        }}

        /* Responsive tweaks for narrow / mobile viewports */
        @media (max-width: 768px) {{
            .block-container {{ padding-left: 0.75rem; padding-right: 0.75rem; }}
            .tsql-hero {{ flex-direction: column; align-items: flex-start; text-align: left; }}
            .tsql-hero h1 {{ font-size: 1.3rem; }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
