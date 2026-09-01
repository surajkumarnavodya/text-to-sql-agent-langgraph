"""Minimal REST API over the same LangGraph agent the Streamlit UI drives.

Not a second implementation of anything: `POST /ask` calls
`agent.graph.run_agent` directly, so every safety layer that already governs
the UI (input guard, SQL validator, row cap, timeout, rate limiting,
sensitive-column blocking) applies identically here -- there is no separate
code path that could drift out of sync or be weaker. See `docs/API.md`.
"""
