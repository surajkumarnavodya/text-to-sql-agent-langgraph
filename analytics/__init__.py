"""Typed analytics contracts (the "Analytics" boundary in
`02_TARGET_ARCHITECTURE.md`), plus one real, tested default adapter over
data this codebase already computes.

Not called by `agent/nodes.py` or any route in this increment -- see
`analytics/provider.py`'s own module docstring and
`02_TARGET_ARCHITECTURE.md`'s "stubbed today, wired later" table for the
named future prompt that retrofits `generate_insight_node` to use this.
"""

from __future__ import annotations
