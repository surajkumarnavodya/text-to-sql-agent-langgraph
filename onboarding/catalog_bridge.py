"""Bridges a published onboarding job into the governed semantic catalog --
Prompt 32 (`32_ROLE_BASED_NAVIGATION_CONTRACT.md`).

Before this module, onboarding and the SME catalog were two review systems that
never met: `onboarding.jobs.publish_job` built a semantic-contract artifact, but
no catalog entry ever existed for the SME Semantic Review dashboard to approve.
This closes that gap with the smallest honest step.

**Creates DRAFT entries only.** One `entity` draft per table that has at least
one SME-confirmed semantic label or relationship. A draft is `AI_INFERENCE` in
`agent.provenance` terms: it still needs a catalog reviewer's approval and a
separate publish before it can reach retrieval or the SQL prompt. Master rule
10 holds: confirming an onboarding item does not, by itself, make a catalog
concept confirmed business truth.

**Idempotent.** A table that already has a live (non-superseded) entity for this
database is skipped, so a retried publish never duplicates a concept.

**Only confirmed items are used.** Pending and rejected items are ignored, the
same rule `onboarding.semantic_contract.build_semantic_contract` applies. A
rejected classification leaves no trace here either.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from identity.models import OnboardingJob, OnboardingReviewItem
from identity.repositories.semantic_catalog import create_entry, list_entries
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: The catalog concept type every onboarded table becomes. A table is an entity
#: (see `semantic.catalog.CatalogConceptType`); metrics and dimensions still
#: come from the governed catalog itself, never inferred from column names here.
_CONCEPT_TYPE = "entity"
_BRIDGED_ITEM_TYPES = frozenset({"semantic_label", "relationship"})


def _table_for_item(item: OnboardingReviewItem) -> str | None:
    if item.item_type == "relationship":
        source = item.payload.get("source_table") if item.payload else None
        return source or item.table_name
    return item.table_name


_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def catalog_database_id_for_job(job: OnboardingJob) -> str:
    """The `database_id` every catalog entry from this job is filed under.

    An onboarding job's `database_label` is free text ("My Warehouse"), but the
    catalog's `database_id` is also used to name a vector-store collection
    (`knowledge_base__<id>`), which only accepts `[A-Za-z0-9_-]`. Storing the raw
    label made publish fail for any label with a space. This is a deterministic,
    collision-resistant slug: the label with unsafe runs replaced by `_`, plus the
    job id's first 8 characters so two labels that differ only in punctuation
    never share a catalog namespace.
    """
    slug = _UNSAFE_ID_CHARS.sub("_", job.database_label).strip("_-")
    return f"{slug or 'onboarded'}_{str(job.id)[:8]}"


def _humanize(name: str) -> str:
    return " ".join(part for part in name.replace("-", "_").split("_") if part).title() or name


def _describe(table: str, items: list[OnboardingReviewItem], job: OnboardingJob) -> str:
    labels: list[str] = []
    relationships: list[str] = []
    for item in items:
        if item.item_type == "semantic_label" and item.column_name:
            label = (item.payload or {}).get("label", "unknown")
            labels.append(f"{item.column_name} ({label})")
        elif item.item_type == "relationship":
            target = (item.payload or {}).get("target_table")
            if target:
                relationships.append(target)
    parts = [f"Draft from onboarding job {job.id}, SME-confirmed items only."]
    if labels:
        parts.append("Confirmed column roles: " + ", ".join(sorted(labels)) + ".")
    if relationships:
        parts.append("Confirmed relationships to: " + ", ".join(sorted(set(relationships))) + ".")
    parts.append(f"Table: {table}.")
    return " ".join(parts)


def draft_catalog_entries_for_job(
    session: Session,
    job: OnboardingJob,
    items: list[OnboardingReviewItem],
) -> int:
    """Creates one DRAFT entity per confirmed table that has no live entity yet.

    Returns:
        How many new draft entries were created (`0` on a retry, or when no
        item was confirmed).
    """
    confirmed_by_table: dict[str, list[OnboardingReviewItem]] = defaultdict(list)
    for item in items:
        if item.decision != "confirmed" or item.item_type not in _BRIDGED_ITEM_TYPES:
            continue
        table = _table_for_item(item)
        if table:
            confirmed_by_table[table].append(item)
    if not confirmed_by_table:
        return 0

    database_id = catalog_database_id_for_job(job)
    existing_keys = {
        entry.concept_key
        for entry in list_entries(
            session,
            job.tenant_id,
            database_id=database_id,
            concept_type=_CONCEPT_TYPE,
        )
        if entry.status != "superseded"
    }

    created = 0
    for table in sorted(confirmed_by_table):
        concept_key = f"table:{table}"
        if concept_key in existing_keys:
            continue
        table_items = confirmed_by_table[table]
        confidences = [item.confidence for item in table_items if item.confidence is not None]
        confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
        evidence: list[dict[str, Any]] = [
            {
                "source": "onboarding_job",
                "job_id": str(job.id),
                "item_ids": [str(item.id) for item in table_items],
                "truth_level": "ai_inference",
            }
        ]
        create_entry(
            session,
            tenant_id=job.tenant_id,
            database_id=database_id,
            concept_type=_CONCEPT_TYPE,
            concept_key=concept_key,
            business_name=_humanize(table),
            technical_name=table,
            description=_describe(table, table_items, job),
            grain=None,
            keys=[],
            relationships=[],
            domain=None,
            synonyms=[],
            business_rules=[],
            examples=[],
            evidence=evidence,
            confidence=min(max(confidence, 0.0), 1.0),
            owner=None,
            created_by_user_id=job.created_by_user_id,
        )
        created += 1

    logger.info(
        "[onboarding] bridged job %s into the semantic catalog: %d new draft entr%s",
        job.id,
        created,
        "y" if created == 1 else "ies",
    )
    return created
