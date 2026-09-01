"""Cross-cutting observability: request tracing, structured logging, and
redaction, shared by `api/` and `eval/` (and available to `ui/app.py`).

Nothing here talks to the live agent/database directly -- these are pure
data-shaping and log-capture utilities that `services/` and `eval/runner.py`
feed real `AgentState`/log data into.
"""
