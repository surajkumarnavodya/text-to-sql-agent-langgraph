"""Top-level multi-source router that sits in front of the SQL agent.

See `agent/orchestrator/graph.py` for the entry point (`run_orchestrated`) and
`docs/ARCHITECTURE.md`'s "Multi-source orchestration" section for the full
design. The existing SQL pipeline (`agent/graph.py`) is never modified by
anything in this package -- the SQL destination here is a plain call to its
already-compiled graph via `agent.graph.run_agent`.
"""
