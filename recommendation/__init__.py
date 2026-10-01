"""Typed recommendation contracts (the "Recommendation" boundary in
`02_TARGET_ARCHITECTURE.md`), plus (Prompt 17,
`17_RECOMMENDATION_ENGINE_CONTRACT.md`) the first real, general-purpose
recommendation engine in this codebase.

`models.py` defines the typed shape (`Recommendation`/`RecommendationKind`/
`RecommendationCategory`); `provider.py` is still the unimplemented
`RecommendationProvider` Protocol (unchanged -- its own `recommend(summary:
ResultSummary)` signature doesn't fit `engine.py`'s own, richer multi-source
input shape, exactly as that Protocol's own docstring already disclosed
before this prompt existed); `engine.py` is the real pipeline: Data ->
Finding -> Evidence -> Rule/Model -> Candidate -> Evidence Validation ->
Confidence -> Recommendation, across nine categories (performance, anomaly,
revenue, customer, product, operations, data quality, security, database
performance). See `engine.py`'s own module docstring for the full design.
"""

from __future__ import annotations
