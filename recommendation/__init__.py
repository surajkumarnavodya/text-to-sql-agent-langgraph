"""Typed recommendation contracts (the "Recommendation" boundary in
`02_TARGET_ARCHITECTURE.md`).

Deliberately the thinnest of the four new modules this prompt adds:
`01_BASELINE_ARCHITECTURE_AND_REUSE_INVENTORY.md`'s repo-wide inspection
found no existing recommendation logic anywhere to adapt (unlike
`analytics/`, which adapts `agent.insight.ResultSummary`), so this module
defines only the shape a future recommendation engine's output must take
-- no default provider, no computation, nothing wired into any node or
route. See `02_TARGET_ARCHITECTURE.md`'s "stubbed today, wired later"
table for the future prompt expected to add a real implementation.
"""

from __future__ import annotations
