"""Detectors for the semantic-intelligence engine (Prompt 35).

Input is the tenant's current catalog snapshots (superseded versions are
excluded). Output is a list of `Finding`s. Every finding is a review
candidate: `truth_level` is always AI_INFERENCE, because a detector's
suggestion is inference about the catalog, not confirmed business truth. A
finding never changes a catalog entry. Only an SME publishing an entry makes
that entry CONFIRMED_BUSINESS_TRUTH.

Each finding has a stable `key`, built from its kind and its subjects'
concept keys. Re-running the analysis on unchanged data therefore produces the
same keys, which is what lets persistence update a finding rather than
duplicate it.

Detectors:
- synonym candidates: two different concepts of one type whose names or
  synonyms name the same concept (`normalize.same_concept`).
- term clusters: three or more terms that group through the same test.
- ambiguity: one canonical term that maps to several concepts, or to several
  concept types.
- conflicts: (a) a draft or reviewed definition that differs from the
  published one for the same concept; (b) two metrics with the same business
  term and different definitions.
- candidate business rules: a metric's filters, offered for review. Never
  promoted to a rule.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from semantic.catalog import CatalogEntrySnapshot, CatalogStatus
from semantic.intelligence.normalize import canonical, same_concept

_CURRENT = frozenset({CatalogStatus.DRAFT, CatalogStatus.REVIEWED, CatalogStatus.PUBLISHED})

KIND_SYNONYM = "synonym"
KIND_CLUSTER = "term_cluster"
KIND_AMBIGUITY = "ambiguity"
KIND_CONFLICT = "conflict"
KIND_RULE = "rule_candidate"
KIND_RELATIONSHIP = "metric_relationship"


@dataclass(frozen=True)
class Finding:
    """One review candidate. Risk fields are filled in by `risk.score`."""

    kind: str
    key: str
    title: str
    detail: str
    subjects: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    confidence: float
    published_count: int = 0
    dependents: int = 0
    risk_score: int = 0
    risk_tier: str = "low"
    reasons: tuple[str, ...] = ()
    truth_level: str = "ai_inference"
    payload: dict[str, Any] = field(default_factory=dict)


def _finding_key(kind: str, subject_ids: list[str]) -> str:
    """Stable identity for a finding: kind plus its sorted subject ids, hashed.
    Subject ids are concept keys, not row ids, so the key survives new versions."""
    raw = kind + "|" + "|".join(sorted(subject_ids))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _subject(entry: CatalogEntrySnapshot) -> dict[str, Any]:
    """One concept a finding is about. `owner` is the catalog's business owner
    for it, so a review item names whom to ask, not just what changed."""
    return {
        "entry_id": entry.id,
        "concept_key": entry.concept_key,
        "concept_type": entry.concept_type.value,
        "status": entry.status.value,
        "version": entry.version,
        "owner": entry.owner,
    }


def _terms(entry: CatalogEntrySnapshot) -> list[str]:
    return [entry.business_name, *entry.synonyms]


def current_entries(entries: list[CatalogEntrySnapshot]) -> list[CatalogEntrySnapshot]:
    """The entries the engine reasons over: every non-superseded version."""
    return [e for e in entries if e.status in _CURRENT]


def _definition_signature(entry: CatalogEntrySnapshot) -> dict[str, Any]:
    """What a metric's definition is, normalized so cosmetic differences
    (whitespace, case, table or filter order) are not conflicts."""
    expression = entry.approved_expression or ""
    return {
        "expression": "".join(expression.split()).lower(),
        "source_tables": sorted(t.lower() for t in entry.source_tables),
        "aggregation": (entry.aggregation or "").lower(),
        "filters": sorted(f.lower() for f in entry.filters),
    }


def _differing_fields(a: CatalogEntrySnapshot, b: CatalogEntrySnapshot) -> list[str]:
    sa, sb = _definition_signature(a), _definition_signature(b)
    return [name for name in sa if sa[name] != sb[name]]


def detect_synonyms(entries: list[CatalogEntrySnapshot]) -> list[Finding]:
    """Synonym candidates between different concepts of one type."""
    findings: list[Finding] = []
    seen: set[str] = set()
    for i, a in enumerate(entries):
        for b in entries[i + 1 :]:
            if a.concept_type != b.concept_type or a.concept_key == b.concept_key:
                continue
            known_to_a = {canonical(t) for t in _terms(a)}
            for term_a in _terms(a):
                for term_b in _terms(b):
                    if canonical(term_b) in known_to_a:
                        continue  # already declared on this concept: not a new discovery
                    matched, method, confidence = same_concept(term_a, term_b)
                    if not matched:
                        continue
                    pair = sorted([canonical(term_a), canonical(term_b)])
                    key = _finding_key(KIND_SYNONYM, [f"term:{pair[0]}", f"term:{pair[1]}"])
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(
                        Finding(
                            kind=KIND_SYNONYM,
                            key=key,
                            title=f"Possible synonym: '{term_a}' / '{term_b}'",
                            detail=(
                                f"'{term_a}' ({a.concept_key}) and '{term_b}' ({b.concept_key}) may name "
                                f"the same concept ({method}). Review before treating them as one."
                            ),
                            subjects=(_subject(a), _subject(b)),
                            evidence=(
                                {"entry_id": a.id, "term": term_a},
                                {"entry_id": b.id, "term": term_b},
                            ),
                            confidence=confidence,
                            payload={"method": method, "terms": [term_a, term_b]},
                        )
                    )
    return findings


@dataclass(frozen=True)
class TermCluster:
    """A group of terms that name one concept, across entries."""

    representative: str
    terms: tuple[str, ...]
    entries: tuple[CatalogEntrySnapshot, ...]


def cluster_terms(entries: list[CatalogEntrySnapshot]) -> list[TermCluster]:
    """Groups every (entry, term) occurrence by `same_concept`, union-find
    style. Deterministic: occurrences are visited in input order."""
    occurrences: list[tuple[CatalogEntrySnapshot, str]] = [
        (entry, term) for entry in entries for term in _terms(entry) if canonical(term)
    ]
    parent = list(range(len(occurrences)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(occurrences)):
        for j in range(i + 1, len(occurrences)):
            if same_concept(occurrences[i][1], occurrences[j][1])[0]:
                parent[find(j)] = find(i)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(occurrences)):
        groups[find(index)].append(index)

    clusters: list[TermCluster] = []
    for members in groups.values():
        terms = sorted({occurrences[m][1] for m in members})
        cluster_entries = tuple({occurrences[m][0].id: occurrences[m][0] for m in members}.values())
        counts: dict[str, int] = defaultdict(int)
        for m in members:
            counts[canonical(occurrences[m][1])] += 1
        representative = sorted(counts, key=lambda c: (-counts[c], c))[0]
        clusters.append(TermCluster(representative, tuple(terms), cluster_entries))
    return sorted(clusters, key=lambda c: c.representative)


def detect_clusters(clusters: list[TermCluster]) -> list[Finding]:
    """A persisted cluster finding only for groups of three or more terms, the
    size at which a cluster adds information beyond its pairwise synonyms."""
    findings: list[Finding] = []
    for cluster in clusters:
        if len(cluster.terms) < 3:
            continue
        key = _finding_key(KIND_CLUSTER, [f"term:{cluster.representative}"])
        findings.append(
            Finding(
                kind=KIND_CLUSTER,
                key=key,
                title=f"Term cluster: {cluster.representative}",
                detail=f"{len(cluster.terms)} terms appear to name one concept: {', '.join(cluster.terms)}.",
                subjects=tuple(_subject(e) for e in cluster.entries),
                evidence=tuple(
                    {"entry_id": e.id, "concept_key": e.concept_key} for e in cluster.entries
                ),
                confidence=0.7,
                payload={"terms": list(cluster.terms)},
            )
        )
    return findings


def detect_ambiguity(clusters: list[TermCluster]) -> list[Finding]:
    """A term that maps to several concepts, or to several concept types, is
    ambiguous: a question using it could mean either, and the catalog should
    say which one it means."""
    findings: list[Finding] = []
    for cluster in clusters:
        concept_keys = {e.concept_key for e in cluster.entries}
        concept_types = {e.concept_type.value for e in cluster.entries}
        if len(concept_keys) < 2:
            continue
        reason = (
            "spans several concept types" if len(concept_types) > 1 else "maps to several concepts"
        )
        key = _finding_key(KIND_AMBIGUITY, [f"term:{cluster.representative}"])
        findings.append(
            Finding(
                kind=KIND_AMBIGUITY,
                key=key,
                title=f"Ambiguous term: {cluster.representative}",
                detail=f"'{cluster.representative}' {reason}: {', '.join(sorted(concept_keys))}.",
                subjects=tuple(_subject(e) for e in cluster.entries),
                evidence=tuple(
                    {"entry_id": e.id, "concept_key": e.concept_key} for e in cluster.entries
                ),
                confidence=0.8,
                payload={
                    "concept_keys": sorted(concept_keys),
                    "concept_types": sorted(concept_types),
                },
            )
        )
    return findings


def detect_conflicts(entries: list[CatalogEntrySnapshot]) -> list[Finding]:
    """Definition conflicts between current metric definitions.

    (a) `proposed_definition_differs`: the same concept has a published version
        and a draft or reviewed version whose definition differs. This is the
        exact change an SME must decide on.
    (b) `conflicting_metric_definitions`: two different metric concepts share a
        business term and disagree on definition.
    """
    findings: list[Finding] = []
    by_key: dict[str, list[CatalogEntrySnapshot]] = defaultdict(list)
    for entry in entries:
        if entry.concept_type.value == "metric" and entry.approved_expression:
            by_key[entry.concept_key].append(entry)

    for concept_key, versions in by_key.items():
        published = [v for v in versions if v.status == CatalogStatus.PUBLISHED]
        pending = [v for v in versions if v.status != CatalogStatus.PUBLISHED]
        for pub in published:
            for draft in pending:
                fields = _differing_fields(pub, draft)
                if not fields:
                    continue
                key = _finding_key(KIND_CONFLICT, [f"metric:{concept_key}", "proposed"])
                findings.append(
                    Finding(
                        kind=KIND_CONFLICT,
                        key=key,
                        title=f"Proposed definition differs from published: {concept_key}",
                        detail=(
                            f"The {draft.status.value} v{draft.version} of '{concept_key}' differs from "
                            f"published v{pub.version} in: {', '.join(fields)}."
                        ),
                        subjects=(_subject(pub), _subject(draft)),
                        evidence=(
                            {
                                "entry_id": pub.id,
                                "concept_key": concept_key,
                                "version": pub.version,
                            },
                            {
                                "entry_id": draft.id,
                                "concept_key": concept_key,
                                "version": draft.version,
                            },
                        ),
                        confidence=0.9,
                        published_count=1,
                        payload={
                            "differing_fields": fields,
                            "conflict": "proposed_definition_differs",
                        },
                    )
                )

    metrics = [e for e in entries if e.concept_type.value == "metric" and e.approved_expression]
    seen: set[str] = set()
    for i, a in enumerate(metrics):
        for b in metrics[i + 1 :]:
            if a.concept_key == b.concept_key:
                continue
            if not same_concept(a.business_name, b.business_name)[0]:
                continue
            fields = _differing_fields(a, b)
            if not fields:
                continue
            key = _finding_key(
                KIND_CONFLICT, [f"metric:{a.concept_key}", f"metric:{b.concept_key}"]
            )
            if key in seen:
                continue
            seen.add(key)
            published_count = sum(1 for e in (a, b) if e.status == CatalogStatus.PUBLISHED)
            findings.append(
                Finding(
                    kind=KIND_CONFLICT,
                    key=key,
                    title=f"Conflicting metric definitions: '{a.business_name}'",
                    detail=(
                        f"Metrics '{a.concept_key}' and '{b.concept_key}' share the term "
                        f"'{a.business_name}' but differ in: {', '.join(fields)}."
                    ),
                    subjects=(_subject(a), _subject(b)),
                    evidence=(
                        {"entry_id": a.id, "concept_key": a.concept_key},
                        {"entry_id": b.id, "concept_key": b.concept_key},
                    ),
                    confidence=0.85,
                    published_count=published_count,
                    payload={
                        "differing_fields": fields,
                        "conflict": "conflicting_metric_definitions",
                    },
                )
            )
    return findings


def detect_rule_candidates(entries: list[CatalogEntrySnapshot]) -> list[Finding]:
    """A metric's filters, offered as candidate business rules. The wording
    says plainly that the rule is unconfirmed. It is never promoted automatically."""
    findings: list[Finding] = []
    for entry in entries:
        if entry.concept_type.value != "metric":
            continue
        for filter_text in entry.filters:
            normalized = " ".join(filter_text.lower().split())
            key = _finding_key(KIND_RULE, [f"metric:{entry.concept_key}", f"filter:{normalized}"])
            findings.append(
                Finding(
                    kind=KIND_RULE,
                    key=key,
                    title=f"Candidate rule for {entry.concept_key}",
                    detail=(
                        f"Candidate (unconfirmed) rule: metric '{entry.business_name}' applies the "
                        f"filter {filter_text}. Confirm before treating this as a business rule."
                    ),
                    subjects=(_subject(entry),),
                    evidence=({"entry_id": entry.id, "concept_key": entry.concept_key},),
                    confidence=0.5,
                    payload={"filter": filter_text},
                )
            )
    return findings


def analyze(entries: list[CatalogEntrySnapshot]) -> tuple[list[Finding], list[TermCluster]]:
    """Runs every detector over one tenant's catalog. Returns the findings and
    the term clusters (clusters are also returned for the impact analysis and
    the API's grouping view)."""
    current = current_entries(entries)
    clusters = cluster_terms(current)
    findings = (
        detect_synonyms(current)
        + detect_clusters(clusters)
        + detect_ambiguity(clusters)
        + detect_conflicts(current)
        + detect_rule_candidates(current)
    )
    return findings, clusters
