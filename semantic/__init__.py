"""The "Semantic" boundary in `02_TARGET_ARCHITECTURE.md`: governed metric
definitions.

Deliberately separate from `retrieval/`'s existing `ChunkType.METRIC`
fuzzy-retrieval chunks, not a reuse of them or an 8th `ChunkType` --
`retrieval/`'s job is similarity-threshold-gated *discovery* ("which
metrics might be relevant to this question"), with type-specific fields
living in a chunk's deliberately untyped `extra: dict[str, Any]`.
Governance fields this module cares about (an owner, an approval status, a
supersession chain) need authoritative typed resolution ("what does this
metric officially mean, right now"), which retrieval's untyped,
maybe-doesn't-surface-at-all model can't honestly provide. See
`02_TARGET_ARCHITECTURE.md`'s Semantic section for the full reasoning and
the intended later integration (retrieval renders a governed
`MetricDefinition` into a `METRIC` chunk's `extra` for discovery purposes
-- never the reverse).

`semantic.metrics.YamlMetricRegistry` is a real, tested default
implementation reading the same `data/knowledge/metrics.yaml`
`retrieval/chunking.py::metric_chunks_from_yaml` already parses -- zero new
persistence, zero approval-workflow API. Not called by any node or route
in this increment.
"""

from __future__ import annotations
