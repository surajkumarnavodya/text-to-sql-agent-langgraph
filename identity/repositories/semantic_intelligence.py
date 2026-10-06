"""Persistence for semantic-intelligence findings, and catalog version rollback (Prompt 35).

Findings are upserted by `(tenant_id, database_id, finding_key)`. Every query
filters on `tenant_id`, so a finding id from another tenant resolves to `None`,
exactly like a nonexistent one (anti-enumeration, the same posture as
`identity.repositories.semantic_catalog` and `identity.share_policy`).

Versioning rules:
- An unchanged re-run only touches `last_seen_at`.
- A changed re-run snapshots the previous content into `history`, bumps
  `version`, and reopens the finding. New information needs a fresh decision.
- A rollback restores an earlier `history` snapshot as a new version. It never
  rewrites or deletes history.

Catalog rollback creates a new DRAFT from an older version through
`create_entry`. A published row is never touched, and the change still needs
the normal review and publish workflow.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from semantic.intelligence.detect import Finding
from sqlalchemy import select
from sqlalchemy.orm import Session

from identity.models import SemanticCatalogEntry, SemanticFinding
from identity.repositories.semantic_catalog import create_entry, list_versions_for_concept_key

FINDING_STATUSES = frozenset({"open", "accepted", "dismissed"})


class InvalidFindingTransitionError(Exception):
    """A decision was made on a finding that is not `open`, or an unknown decision was given."""


class FindingVersionNotFoundError(Exception):
    """A rollback target version is not in the finding's history."""


class CatalogVersionNotFoundError(Exception):
    """A catalog rollback target version does not exist for that concept."""


@dataclass(frozen=True)
class UpsertSummary:
    created: int
    updated: int
    unchanged: int


def _content_of(finding: Finding) -> dict[str, Any]:
    """The part of a finding that is versioned. Timestamps are excluded, so
    only a change in what the analysis concluded counts as a new version."""
    return {
        "kind": finding.kind,
        "title": finding.title,
        "detail": finding.detail,
        "subjects": list(finding.subjects),
        "evidence": list(finding.evidence),
        "confidence": finding.confidence,
        "truth_level": finding.truth_level,
        "risk_score": finding.risk_score,
        "risk_tier": finding.risk_tier,
        "reasons": list(finding.reasons),
        "dependents": finding.dependents,
        "payload": finding.payload,
    }


def _hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def upsert_findings(
    session: Session,
    *,
    tenant_id: str,
    database_id: str,
    findings: list[Finding],
    now: datetime,
) -> UpsertSummary:
    """Writes one analysis run's findings for one tenant's database. Creates
    new rows, versions changed ones, and leaves unchanged ones alone."""
    created = updated = unchanged = 0
    for finding in findings:
        content = _content_of(finding)
        content_hash = _hash(content)
        row = session.scalar(
            select(SemanticFinding)
            .where(SemanticFinding.tenant_id == tenant_id)
            .where(SemanticFinding.database_id == database_id)
            .where(SemanticFinding.finding_key == finding.key)
        )
        if row is None:
            session.add(
                SemanticFinding(
                    tenant_id=tenant_id,
                    database_id=database_id,
                    finding_key=finding.key,
                    kind=finding.kind,
                    title=finding.title,
                    content=content,
                    content_hash=content_hash,
                    confidence=finding.confidence,
                    truth_level=finding.truth_level,
                    risk_score=finding.risk_score,
                    risk_tier=finding.risk_tier,
                    status="open",
                    version=1,
                    history=[],
                    last_seen_at=now,
                )
            )
            created += 1
            continue
        row.last_seen_at = now
        if row.content_hash == content_hash:
            unchanged += 1
            continue
        row.history = [
            *(row.history or []),
            {
                "version": row.version,
                "content": row.content,
                "content_hash": row.content_hash,
                "status": row.status,
                "recorded_at": now.isoformat(),
            },
        ]
        row.version = row.version + 1
        row.content = content
        row.content_hash = content_hash
        row.kind = finding.kind
        row.title = finding.title
        row.confidence = finding.confidence
        row.truth_level = finding.truth_level
        row.risk_score = finding.risk_score
        row.risk_tier = finding.risk_tier
        row.status = "open"  # changed content needs a fresh decision
        updated += 1
    session.commit()
    return UpsertSummary(created=created, updated=updated, unchanged=unchanged)


def list_findings(
    session: Session,
    *,
    tenant_id: str,
    database_id: str | None = None,
    status: str | None = None,
) -> list[SemanticFinding]:
    """The tenant's review queue, riskiest first. Ties break on the stable key,
    so the order is the same on every read."""
    stmt = select(SemanticFinding).where(SemanticFinding.tenant_id == tenant_id)
    if database_id is not None:
        stmt = stmt.where(SemanticFinding.database_id == database_id)
    if status is not None:
        stmt = stmt.where(SemanticFinding.status == status)
    rows = list(session.scalars(stmt))
    return sorted(rows, key=lambda r: (-r.risk_score, r.kind, r.finding_key))


def get_finding(
    session: Session, *, tenant_id: str, finding_id: uuid.UUID
) -> SemanticFinding | None:
    """A finding in this tenant, or `None`. Another tenant's id is `None`
    too, never a distinguishable error."""
    row = session.get(SemanticFinding, finding_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


def decide_finding(
    session: Session,
    row: SemanticFinding,
    *,
    decision: str,
    user_id: uuid.UUID | None,
    note: str | None,
    now: datetime,
) -> SemanticFinding:
    """Accepts or dismisses an open finding. Only ever changes review state.
    No catalog entry is touched, and the finding stays AI_INFERENCE."""
    if decision not in {"accept", "dismiss"}:
        raise InvalidFindingTransitionError(f"Unknown decision {decision!r}.")
    if row.status != "open":
        raise InvalidFindingTransitionError(f"A {row.status} finding cannot be decided again.")
    row.status = "accepted" if decision == "accept" else "dismissed"
    row.decided_by_user_id = user_id
    row.decision_note = note
    row.decided_at = now
    session.commit()
    return row


def rollback_finding(
    session: Session,
    row: SemanticFinding,
    *,
    to_version: int,
    user_id: uuid.UUID | None,
    now: datetime,
) -> SemanticFinding:
    """Restores an earlier version's content as a new version, and reopens
    the finding for a fresh decision. The current content is kept in history."""
    if to_version >= row.version:
        raise FindingVersionNotFoundError(f"Version {to_version} is not an earlier version.")
    snapshot = next((h for h in row.history or [] if h["version"] == to_version), None)
    if snapshot is None:
        raise FindingVersionNotFoundError(f"Version {to_version} is not in this finding's history.")
    row.history = [
        *(row.history or []),
        {
            "version": row.version,
            "content": row.content,
            "content_hash": row.content_hash,
            "status": row.status,
            "recorded_at": now.isoformat(),
        },
    ]
    content = dict(snapshot["content"])
    row.version = row.version + 1
    row.content = content
    row.content_hash = snapshot["content_hash"]
    row.kind = content["kind"]
    row.title = content["title"]
    row.confidence = content["confidence"]
    row.truth_level = content["truth_level"]
    row.risk_score = content["risk_score"]
    row.risk_tier = content["risk_tier"]
    row.status = "open"
    row.decided_by_user_id = user_id
    row.decision_note = f"Rolled back to version {to_version}."
    row.decided_at = now
    session.commit()
    return row


def rollback_catalog_entry(
    session: Session,
    entry: SemanticCatalogEntry,
    *,
    target_version: int,
    actor_user_id: uuid.UUID | None,
) -> SemanticCatalogEntry:
    """Creates a new DRAFT carrying an older version's content. The published
    row is never touched, and the new draft goes through the normal review and
    publish workflow like any other edit."""
    versions = list_versions_for_concept_key(
        session,
        tenant_id=entry.tenant_id,
        database_id=entry.database_id,
        concept_type=entry.concept_type,
        concept_key=entry.concept_key,
    )
    target = next((v for v in versions if v.version == target_version), None)
    if target is None:
        raise CatalogVersionNotFoundError(
            f"Version {target_version} does not exist for this concept."
        )
    return create_entry(
        session,
        tenant_id=entry.tenant_id,
        database_id=entry.database_id,
        concept_type=entry.concept_type,
        concept_key=entry.concept_key,
        business_name=target.business_name,
        technical_name=target.technical_name,
        description=target.description,
        grain=target.grain,
        keys=list(target.keys or []),
        relationships=list(target.relationships or []),
        domain=target.domain,
        synonyms=list(target.synonyms or []),
        business_rules=list(target.business_rules or []),
        examples=list(target.examples or []),
        evidence=list(target.evidence or []),
        confidence=target.confidence,
        owner=target.owner,
        created_by_user_id=actor_user_id,
        approved_expression=target.approved_expression,
        source_tables=list(target.source_tables or []),
        filters=list(target.filters or []),
        dimensions=list(target.dimensions or []),
        aggregation=target.aggregation,
    )
