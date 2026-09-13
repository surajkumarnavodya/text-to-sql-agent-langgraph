"""REST API over the same LangGraph agent the React dashboard (frontend/)
talks to -- the API this app runs on. Also serves the dashboard's own
built static files from this same process/port (see `api/main.py`'s
StaticFiles mount) once `frontend/dist` exists.

Not a second implementation of anything: `POST /ask` calls
`agent.orchestrator.graph.run_orchestrated` directly, which itself is a
pure pass-through to `agent.graph.run_agent` unless
`ENABLE_MULTI_SOURCE_ROUTER` is set (see `agent/orchestrator/graph.py`'s
docstring) -- so every safety layer that already governs the agent (input
guard, SQL validator, row cap, timeout, rate limiting, sensitive-column
blocking) applies identically here -- there is no separate code path that
could drift out of sync or be weaker. See `docs/API.md`.
"""
