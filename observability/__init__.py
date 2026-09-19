"""Live, in-process observability for this application.

Deliberately small in scope: `metrics.py` turns per-stage LangGraph node
timings that `agent.nodes._timed_node` already emits into every request's
log lines (and has since before this package existed) into a queryable,
in-memory rollup -- no new instrumentation, only aggregation of data this
codebase already produces on every `/ask` call. See `metrics.py`'s module
docstring and `CLAUDE.md`'s "Observability rollup" section for the full
design and its deliberate limits (single-process only, not a replacement
for real metrics infrastructure in a multi-worker deployment).
"""
