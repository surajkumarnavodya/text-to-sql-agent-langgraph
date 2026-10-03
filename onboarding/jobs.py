"""Orchestrates the onboarding pipeline stages into one job state
machine -- Prompt 08 (`08_ONBOARDING_ENGINE_CONTRACT.md`). The only
module in `onboarding/` that imports both the pure-logic pipeline stages
*and* `identity/`'s persistence layer -- every other module in this
package stays independently testable with plain Python objects; this one
is what glues them into `identity.models.OnboardingJob`'s actual state
transitions.

**No background worker, by design.** `run_discovery_stage`/
`publish_job` both need a live `Engine` for the target database, and
this engine is always supplied fresh by the caller
(`api/onboarding.py`), built from a connection secret that is validated,
used immediately, and never persisted (see `identity.models
.OnboardingJob`'s own docstring for the full rationale). There is no
"resume this job later with no further input" code path here -- a job
stuck in `"discovering"`/`"publishing"` because its own API call crashed
mid-flight is only ever recoverable via `retry_job`, which requires a
fresh call (and fresh credentials) to actually resume, exactly like the
original call did. This is the deliberate, disclosed tradeoff for never
storing a database password.

State machine: `pending -> discovering -> awaiting_review -> publishing
-> published`, with `failed` reachable from `discovering`/`publishing`
(a `retry_job` call resumes from the stage that actually failed, not
always from the very beginning), and `cancelled` reachable from any
non-terminal status.
"""

from __future__ import annotations

import logging
from typing import Any

from identity.models import OnboardingJob
from identity.repositories.onboarding import (
    add_artifact,
    add_review_items,
    increment_retry_count,
    list_review_items,
    update_job_status,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from db.connection import get_sqlglot_dialect
from db.relationship_inference import (
    InferredRelationship,
    infer_relationships,
    verify_candidates_with_data,
)
from db.schema_introspection import TableSchemaInfo, introspect_schema
from onboarding.catalog_bridge import draft_catalog_entries_for_job
from onboarding.evaluation import evaluate_candidates
from onboarding.golden_questions import GoldenQuestionCandidate, generate_candidate_questions
from onboarding.pii_detection import PiiFinding, detect_pii_columns, verify_pii_with_data
from onboarding.profiling import ColumnProfile, find_duplicate_keys, profile_column
from onboarding.semantic_contract import ConfirmedReviewItem, build_semantic_contract
from onboarding.semantic_inference import SemanticLabel, infer_semantic_labels
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"published", "cancelled"})
_MAX_PROFILED_COLUMNS = 300


class OnboardingJobError(Exception):
    """A pipeline-level error -- always caught by this module's own
    orchestration functions and turned into a `"failed"` job status, so
    this is only ever raised for a genuine caller-misuse case (e.g.
    publishing a job that still has undecided review items), not a data/
    connection failure (which fails the job, it doesn't raise past this
    module)."""


def run_discovery_stage(
    session: Session,
    job: OnboardingJob,
    engine: Engine,
    *,
    verify_relationships_with_data: bool = False,
    verify_pii_with_data_flag: bool = False,
) -> OnboardingJob:
    """Runs discovery -> relationships -> profiling -> PII -> semantic
    inference -> golden-question generation, and persists every
    candidate as an `OnboardingReviewItem`. Reuses
    `db.schema_introspection` (Prompt 06) and `db.relationship_inference`
    (Prompt 07) unchanged -- this function adds no schema/relationship
    logic of its own, only the new profiling/PII/semantic/golden-question
    stages plus the job-state bookkeeping.

    `verify_relationships_with_data`/`verify_pii_with_data_flag` are two
    *separate* opt-in flags (`Settings.enable_relationship_data_
    verification`/`Settings.enable_pii_data_verification`), not one --
    per this prompt's own "minimize sensitive sampling" requirement, an
    operator may want relationship value-overlap sampling (never
    touches anything PII-shaped) without also opting into sampling
    columns already name-flagged as likely PII.

    Any failure (a dropped connection mid-profiling, an unexpected
    exception in one stage) marks the job `"failed"` with a redacted
    error message and re-raises nothing -- the caller (`api/onboarding.py`)
    reads the returned job's own `status`/`error_message`.
    """
    job = update_job_status(session, job, status="discovering", current_stage="discovery")
    try:
        tables = introspect_schema(engine, schema=job.db_schema, include_row_counts=True)
        relationships = infer_relationships(tables)
        if verify_relationships_with_data:
            relationships = verify_candidates_with_data(relationships, engine, schema=job.db_schema)

        column_profiles = _profile_all_columns(engine, tables, job.db_schema)
        duplicate_keys = find_duplicate_keys(engine, tables, schema=job.db_schema)

        pii_findings = detect_pii_columns(tables)
        if verify_pii_with_data_flag:
            pii_findings = verify_pii_with_data(pii_findings, engine, schema=job.db_schema)

        semantic_labels = infer_semantic_labels(tables, relationships, column_profiles)
        golden_candidates = generate_candidate_questions(semantic_labels, relationships)

        review_items = (
            [_pii_finding_to_review_item(f) for f in pii_findings]
            + [_relationship_to_review_item(r) for r in relationships]
            + [_semantic_label_to_review_item(label) for label in semantic_labels]
            + [_golden_question_to_review_item(g) for g in golden_candidates]
        )
        add_review_items(session, job, review_items)

        summary = {
            "table_count": sum(1 for t in tables if not t.is_view),
            "view_count": sum(1 for t in tables if t.is_view),
            "relationship_candidate_count": len(relationships),
            "pii_finding_count": len(pii_findings),
            "duplicate_key_count": len(duplicate_keys),
            "golden_question_count": len(golden_candidates),
        }
        return update_job_status(
            session, job, status="awaiting_review", current_stage=None, discovery_summary=summary
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any stage failure marks the job failed, never crashes the caller
        safe_error = redact_secrets(str(exc))
        logger.warning("[onboarding] discovery failed for job %s: %s", job.id, safe_error)
        return update_job_status(
            session, job, status="failed", current_stage="discovery", error_message=safe_error
        )


def _profile_all_columns(
    engine: Engine, tables: list[TableSchemaInfo], schema: str | None
) -> dict[tuple[str, str], ColumnProfile]:
    profiles: dict[tuple[str, str], ColumnProfile] = {}
    profiled = 0
    for table in tables:
        if table.is_view:
            continue
        for column in table.columns:
            if profiled >= _MAX_PROFILED_COLUMNS:
                return profiles
            profile = profile_column(engine, table.table_name, column.name, column.type, schema)
            if profile is not None:
                profiles[(table.table_name, column.name)] = profile
            profiled += 1
    return profiles


def _pii_finding_to_review_item(finding: PiiFinding) -> dict[str, Any]:
    return {
        "item_type": "pii_classification",
        "table_name": finding.table_name,
        "column_name": finding.column_name,
        "subject": f"{finding.table_name}.{finding.column_name} -> {finding.pii_category}",
        "payload": {
            "pii_category": finding.pii_category,
            "truth_level": finding.truth_level.value,
            "evidence": [
                {"signal": e.signal, "score": e.score, "detail": e.detail} for e in finding.evidence
            ],
        },
        "confidence": finding.confidence,
        "is_ambiguous": finding.confidence < 0.6,
    }


def _relationship_to_review_item(candidate: InferredRelationship) -> dict[str, Any]:
    return {
        "item_type": "relationship",
        "table_name": candidate.source_table,
        "column_name": candidate.source_columns[0],
        "subject": (
            f"{candidate.source_table}.{','.join(candidate.source_columns)} -> "
            f"{candidate.target_table}.{','.join(candidate.target_columns)}"
        ),
        "payload": {
            "source_table": candidate.source_table,
            "source_columns": list(candidate.source_columns),
            "target_table": candidate.target_table,
            "target_columns": list(candidate.target_columns),
            "relationship_type": candidate.relationship_type,
            "truth_level": candidate.truth_level.value,
            "evidence": [
                {"signal": e.signal, "score": e.score, "detail": e.detail}
                for e in candidate.evidence
            ],
        },
        "confidence": candidate.confidence,
        "is_ambiguous": False,
    }


def _semantic_label_to_review_item(label: SemanticLabel) -> dict[str, Any]:
    return {
        "item_type": "semantic_label",
        "table_name": label.table_name,
        "column_name": label.column_name,
        "subject": f"{label.table_name}.{label.column_name} -> {label.label}",
        "payload": {
            "label": label.label,
            "truth_level": label.truth_level.value,
            "evidence": list(label.evidence),
        },
        "confidence": label.confidence,
        "is_ambiguous": label.is_ambiguous,
    }


def _golden_question_to_review_item(candidate: GoldenQuestionCandidate) -> dict[str, Any]:
    return {
        "item_type": "golden_question",
        "table_name": candidate.tables_involved[0] if candidate.tables_involved else None,
        "column_name": None,
        "subject": candidate.question,
        "payload": {
            "question": candidate.question,
            "candidate_sql": candidate.candidate_sql,
            "tables_involved": list(candidate.tables_involved),
            "question_type": candidate.question_type,
            "truth_level": candidate.truth_level.value,
        },
        "confidence": candidate.confidence,
        "is_ambiguous": candidate.confidence < 0.6,
    }


def publish_job(
    session: Session,
    job: OnboardingJob,
    engine: Engine,
) -> OnboardingJob:
    """Builds the semantic-contract/golden-questions/evaluation-report
    artifacts from every `"confirmed"` review item and marks the job
    `"published"`. Requires every review item to already be decided
    (`"confirmed"` or `"rejected"`) -- a job with any `"pending"` item
    cannot be published, since an undecided PII/relationship/semantic
    claim reaching a published artifact would be exactly the "silently
    promoted inference" rule 10 forbids.
    """
    job = update_job_status(session, job, status="publishing", current_stage="publish")
    try:
        all_items = list_review_items(session, job.id)
        if any(item.decision == "pending" for item in all_items):
            raise OnboardingJobError(
                "Cannot publish: one or more review items are still pending a decision."
            )

        confirmed_items = [item for item in all_items if item.decision == "confirmed"]
        contract_items = [
            ConfirmedReviewItem(
                item_type=item.item_type,
                table_name=item.table_name,
                column_name=item.column_name,
                payload=item.payload,
            )
            for item in confirmed_items
            if item.item_type != "golden_question"
        ]
        contract = build_semantic_contract(contract_items)
        add_artifact(session, job, artifact_type="semantic_contract", content=contract)

        confirmed_questions = [
            item for item in confirmed_items if item.item_type == "golden_question"
        ]
        candidates = [
            GoldenQuestionCandidate(
                question=item.payload["question"],
                tables_involved=tuple(item.payload.get("tables_involved", [])),
                question_type=item.payload["question_type"],
                confidence=item.confidence,
                candidate_sql=item.payload["candidate_sql"],
            )
            for item in confirmed_questions
        ]
        add_artifact(
            session,
            job,
            artifact_type="golden_questions",
            content={
                "questions": [
                    {"question": c.question, "sql": c.candidate_sql, "type": c.question_type}
                    for c in candidates
                ]
            },
        )

        dialect = get_sqlglot_dialect(job.db_type)
        evaluation_results = evaluate_candidates(candidates, engine, dialect=dialect)
        add_artifact(
            session,
            job,
            artifact_type="evaluation_report",
            content={
                "results": [
                    {
                        "question": r.question,
                        "passed": r.passed,
                        "error": r.error,
                        "row_count": r.row_count,
                    }
                    for r in evaluation_results
                ],
                "pass_count": sum(1 for r in evaluation_results if r.passed),
                "total_count": len(evaluation_results),
            },
        )

        # Prompt 32: confirmed tables become DRAFT catalog entries so the SME
        # Semantic Review dashboard can approve them. Drafts only, and idempotent
        # on a retried publish (see `onboarding.catalog_bridge`).
        draft_catalog_entries_for_job(session, job, all_items)

        return update_job_status(session, job, status="published", current_stage=None)
    except OnboardingJobError:
        update_job_status(
            session,
            job,
            status="failed",
            current_stage="publish",
            error_message="Cannot publish: one or more review items are still pending a decision.",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - any stage failure marks the job failed
        safe_error = redact_secrets(str(exc))
        logger.warning("[onboarding] publish failed for job %s: %s", job.id, safe_error)
        return update_job_status(
            session, job, status="failed", current_stage="publish", error_message=safe_error
        )


def cancel_job(session: Session, job: OnboardingJob) -> OnboardingJob:
    """Cancels a job from any non-terminal status. A `"published"` or
    already-`"cancelled"` job cannot be cancelled -- there is nothing
    left to stop."""
    if job.status in _TERMINAL_STATUSES:
        raise OnboardingJobError(f"Cannot cancel a job that is already {job.status!r}.")
    return update_job_status(session, job, status="cancelled", current_stage=None)


def retry_job(session: Session, job: OnboardingJob) -> OnboardingJob:
    """Resets a `"failed"` job so the caller can retry the stage that
    actually failed -- a discovery failure resets to `"pending"` (the
    caller re-calls `run_discovery_stage`); a publish failure resets to
    `"awaiting_review"` (the caller re-calls `publish_job` directly,
    skipping discovery since it already succeeded)."""
    if job.status != "failed":
        raise OnboardingJobError("Only a failed job can be retried.")
    resume_status = "awaiting_review" if job.current_stage == "publish" else "pending"
    increment_retry_count(session, job)
    return update_job_status(session, job, status=resume_status, current_stage=None)
