"""Metric relationships and semantic impact analysis (Prompt 35).

Two read-only analyses over the current catalog. Neither changes any entry.

- `metric_relationships`: which metrics depend on which. Two relations are
  detected. `shares_source_tables`: two metrics read a common table. And
  `references_metric`: one metric's approved expression names another
  metric's business term, with every word of that term present in the
  expression. Both are heuristics and are labelled AI_INFERENCE with their
  confidence.
- `impact_of_change`: if this concept changed, or stopped reading some of its
  tables, which other concepts would be affected. This is a what-if list for an
  SME, not a prediction of what will break.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from semantic.catalog import CatalogEntrySnapshot
from semantic.intelligence.detect import current_entries
from semantic.intelligence.normalize import content_tokens, same_concept

# Expression words that carry no metric identity. A term made only of these
# is never treated as a reference.
_EXPRESSION_NOISE = frozenset(
    {
        "sum",
        "count",
        "avg",
        "min",
        "max",
        "distinct",
        "select",
        "from",
        "where",
        "group",
        "by",
        "and",
        "or",
        "not",
        "null",
        "as",
    }
)


@dataclass(frozen=True)
class MetricEdge:
    source_key: str
    target_key: str
    relation: str  # "shares_source_tables" | "references_metric"
    confidence: float
    reason: str


@dataclass(frozen=True)
class ImpactReport:
    concept_key: str
    dependents: tuple[dict[str, Any], ...]
    risk_tier: str
    removed_tables: tuple[str, ...]


def _tables(entry: CatalogEntrySnapshot) -> set[str]:
    return {t.lower() for t in entry.source_tables}


def _name_tokens(entry: CatalogEntrySnapshot) -> frozenset[str]:
    return content_tokens(entry.business_name) - _EXPRESSION_NOISE


def metric_relationships(entries: list[CatalogEntrySnapshot]) -> list[MetricEdge]:
    """Every dependency edge between current metric concepts. Each unordered
    pair is reported at most once per relation, in input order."""
    metrics = [
        e
        for e in current_entries(entries)
        if e.concept_type.value == "metric" and e.approved_expression
    ]
    edges: list[MetricEdge] = []
    for i, a in enumerate(metrics):
        for b in metrics[i + 1 :]:
            if a.concept_key == b.concept_key:
                continue
            shared = _tables(a) & _tables(b)
            if shared:
                edges.append(
                    MetricEdge(
                        a.concept_key,
                        b.concept_key,
                        "shares_source_tables",
                        0.7,
                        f"both read {', '.join(sorted(shared))}",
                    )
                )
    for a in metrics:
        expression_tokens = content_tokens(a.approved_expression or "")
        for b in metrics:
            if a.concept_key == b.concept_key:
                continue
            name = _name_tokens(b)
            if name and name <= expression_tokens:
                edges.append(
                    MetricEdge(
                        a.concept_key,
                        b.concept_key,
                        "references_metric",
                        0.6,
                        f"its expression names '{b.business_name}'",
                    )
                )
    return edges


def _risk_tier(dependents: list[dict[str, Any]], subject_published: bool) -> str:
    if any(d["status"] == "published" for d in dependents) or (dependents and subject_published):
        return "high"
    return "medium" if dependents else "low"


def impact_of_change(
    entries: list[CatalogEntrySnapshot],
    concept_key: str,
    proposed_source_tables: list[str] | None = None,
) -> ImpactReport:
    """Lists what depends on `concept_key`, and what would be affected if its
    source tables changed to `proposed_source_tables` (tables it stops reading
    count as removed)."""
    current = current_entries(entries)
    subject = next((e for e in current if e.concept_key == concept_key), None)
    if subject is None:
        raise KeyError(concept_key)

    # One entry per dependent concept, carrying every reason it qualifies for,
    # so a metric that both shares a table and reads a removed one says both.
    reasons_by_key: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}

    def _add(other: CatalogEntrySnapshot, reason: str) -> None:
        if other.concept_key == concept_key:
            return
        reasons_by_key.setdefault(other.concept_key, [])
        statuses[other.concept_key] = other.status.value
        if reason not in reasons_by_key[other.concept_key]:
            reasons_by_key[other.concept_key].append(reason)

    by_key = {e.concept_key: e for e in current}
    for edge in metric_relationships(current):
        if edge.relation == "references_metric" and edge.target_key == concept_key:
            _add(by_key[edge.source_key], "references this metric by name")
        if edge.relation == "references_metric" and edge.source_key == concept_key:
            _add(by_key[edge.target_key], "is referenced by this metric")
        if edge.relation == "shares_source_tables" and concept_key in (
            edge.source_key,
            edge.target_key,
        ):
            other_key = edge.target_key if edge.source_key == concept_key else edge.source_key
            _add(by_key[other_key], "shares source tables")

    for other in current:
        if other.concept_key == concept_key or other.concept_type != subject.concept_type:
            continue
        if any(
            same_concept(term, t)[0]
            for term in [subject.business_name, *subject.synonyms]
            for t in [other.business_name, *other.synonyms]
        ):
            _add(other, "shares a business term")

    removed: set[str] = set()
    if proposed_source_tables is not None:
        removed = _tables(subject) - {t.lower() for t in proposed_source_tables}
        for other in current:
            if (
                other.concept_key != concept_key
                and other.concept_type.value == "metric"
                and _tables(other) & removed
            ):
                _add(other, "reads a table this change removes")

    dependents = [
        {"concept_key": key, "status": statuses[key], "reason": "; ".join(reasons)}
        for key, reasons in reasons_by_key.items()
    ]
    dependents_list: list[dict[str, Any]] = dependents

    return ImpactReport(
        concept_key=concept_key,
        dependents=tuple(dependents_list),
        risk_tier=_risk_tier(dependents_list, subject.status.value == "published"),
        removed_tables=tuple(sorted(removed)),
    )
