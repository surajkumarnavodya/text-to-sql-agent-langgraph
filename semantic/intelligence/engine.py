"""The semantic-intelligence engine: one deterministic pass over a tenant's catalog (Prompt 35).

Runs every detector in `detect.py`, adds one finding per metric relationship
from `impact.py`, counts how many relationships touch each finding's subjects,
and returns the queue ordered by risk (`risk.prioritize`).

Pure: no database, no LLM call, no I/O. Persistence and HTTP are layered on top
in `identity/repositories/semantic_intelligence.py` and `api/semantic_intelligence.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from semantic.catalog import CatalogEntrySnapshot
from semantic.intelligence import detect, impact, risk
from semantic.intelligence.detect import Finding, TermCluster


@dataclass(frozen=True)
class AnalysisResult:
    findings: tuple[Finding, ...]
    clusters: tuple[TermCluster, ...]
    relationships: tuple[impact.MetricEdge, ...]


def run_analysis(entries: list[CatalogEntrySnapshot]) -> AnalysisResult:
    findings, clusters = detect.analyze(entries)
    edges = impact.metric_relationships(entries)

    for edge in edges:
        findings.append(
            Finding(
                kind=detect.KIND_RELATIONSHIP,
                key=detect._finding_key(
                    detect.KIND_RELATIONSHIP,
                    [f"metric:{edge.source_key}", f"metric:{edge.target_key}", edge.relation],
                ),
                title=f"Metric relationship ({edge.relation}): {edge.source_key} -> {edge.target_key}",
                detail=f"'{edge.source_key}' and '{edge.target_key}' are related: {edge.reason}.",
                subjects=(
                    {"concept_key": edge.source_key, "concept_type": "metric"},
                    {"concept_key": edge.target_key, "concept_type": "metric"},
                ),
                evidence=({"relation": edge.relation, "reason": edge.reason},),
                confidence=edge.confidence,
                payload={
                    "relation": edge.relation,
                    "source_key": edge.source_key,
                    "target_key": edge.target_key,
                },
            )
        )

    def _touches(finding: Finding) -> int:
        keys = {s.get("concept_key") for s in finding.subjects}
        return sum(
            1
            for edge in edges
            if (edge.source_key in keys or edge.target_key in keys)
            and not (edge.source_key in keys and edge.target_key in keys)
        )

    scored = [
        (
            risk.score(f, dependents=_touches(f))
            if f.kind != detect.KIND_RELATIONSHIP
            else risk.score(f)
        )
        for f in findings
    ]
    queue = tuple(sorted(scored, key=lambda f: (-f.risk_score, f.kind, f.key)))
    return AnalysisResult(findings=queue, clusters=tuple(clusters), relationships=tuple(edges))
