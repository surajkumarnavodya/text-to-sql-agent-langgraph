"""Turns one governed sub-question run into typed evidence (Prompt 33).

The input is a finished `agent.graph.run_agent` state. The output is only
what the analyst may cite: rows and statistics the database actually produced,
each tagged with the truth level its source claim already carries. Nothing
here computes a new number or upgrades a level. Analytics and recommendation
values are read, never recalculated.

Failure outcomes are mapped to plain-language open items. A restricted-column
block becomes a generic "your role cannot view this data" note with no column
name in it, because the caller may not know that column exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.analyst.state import EvidenceItem, SubquestionStatus

# Enough findings to answer a question well without flooding the report.
# Anything beyond this per sub-question is dropped in analytics' own order.
MAX_FINDINGS_PER_SUBQUESTION = 8

_BLOCKED_CATEGORIES = frozenset({"restricted_column"})


def _enum_value(value: Any) -> str | None:
    """Normalizes an enum-or-string field from a `model_dump()` output."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _finding_claim(finding: dict) -> tuple[str, str]:
    """Returns `(claim_text, truth_level)` for one analytics finding."""
    claim = finding.get("claim") or {}
    return str(claim.get("value", "")), _enum_value(claim.get("level")) or "DATABASE_FACT"


def build_outcome(
    final_state: Mapping[str, Any],
    subquestion_id: str,
    next_evidence_index: int,
    subquestion_text: str,
) -> dict:
    """Maps one finished sub-run to its status, evidence, open items and
    recommendations.

    Args:
        final_state: The `run_agent` return value.
        subquestion_id: The id of the sub-question this run answered.
        next_evidence_index: The number to start evidence ids from, so ids
            stay unique across every sub-question in one analysis.
        subquestion_text: The sub-question's own text, used only to name it
            in an open item.

    Returns:
        A dict with `status` (a `SubquestionStatus`), `evidence`
        (`EvidenceItem` list), `open_items` (str list), `recommendations`
        (dict list), and `detail` (a one-line trace summary).
    """
    status = final_state.get("status")
    evidence: list[EvidenceItem] = []
    open_items: list[str] = []
    recommendations: list[dict] = []

    if status == "succeeded":
        row_count = int(final_state.get("row_count") or 0)
        sql = final_state.get("sql")
        index = next_evidence_index
        evidence.append(
            EvidenceItem(
                id=f"E{index}",
                subquestion_id=subquestion_id,
                claim=f"The query returned {row_count} row(s).",
                truth_level="DATABASE_FACT",
                kind="row_count",
                finding_kind=None,
                period=None,
                sql=sql,
                row_count=row_count,
            )
        )
        index += 1
        if row_count == 0:
            open_items.append(f'No rows matched "{subquestion_text}"; the data may not exist.')
            sub_status: SubquestionStatus = "empty"
        else:
            sub_status = "succeeded"
            analytical = final_state.get("analytical_result") or {}
            for finding in (analytical.get("findings") or [])[:MAX_FINDINGS_PER_SUBQUESTION]:
                claim_text, level = _finding_claim(finding)
                if not claim_text:
                    continue
                anomaly = finding.get("anomaly") or {}
                evidence.append(
                    EvidenceItem(
                        id=f"E{index}",
                        subquestion_id=subquestion_id,
                        claim=claim_text,
                        truth_level=level,
                        kind="analytics_finding",
                        finding_kind=_enum_value(finding.get("kind")),
                        period=(
                            str(anomaly["period"]) if anomaly.get("period") is not None else None
                        ),
                        sql=sql,
                        row_count=row_count,
                    )
                )
                index += 1
            recommendations = list(final_state.get("recommendations") or [])
        return {
            "status": sub_status,
            "evidence": evidence,
            "open_items": open_items,
            "recommendations": recommendations,
            "detail": f"{sub_status}: {row_count} row(s), {len(evidence)} evidence item(s)",
        }

    if status == "needs_clarification":
        message = final_state.get("clarification_message") or (
            "The question is ambiguous. Please make it more specific."
        )
        open_items.append(f'Needs clarification for "{subquestion_text}": {message}')
        return _outcome(
            "needs_clarification", open_items, evidence, recommendations, "needs clarification"
        )

    if status == "rejected":
        open_items.append(
            f'Not analysed, input was rejected: {final_state.get("rejection_message") or "rejected"}'
        )
        return _outcome("rejected", open_items, evidence, recommendations, "input rejected")

    if status == "rate_limited":
        open_items.append(
            f'Not analysed, rate limited: {final_state.get("rate_limit_message") or "try again shortly"}'
        )
        return _outcome("rate_limited", open_items, evidence, recommendations, "rate limited")

    if final_state.get("last_error_category") in _BLOCKED_CATEGORIES:
        # Generic on purpose: no column or table name, and no echo of the
        # question text, since the caller may not be allowed to know the
        # restricted data exists.
        open_items.append(
            f"Not analysed (step {subquestion_id}): your role cannot view part of the data it needs."
        )
        return _outcome(
            "blocked", open_items, evidence, recommendations, "blocked by access policy"
        )

    explanation = final_state.get("failure_explanation") or "The query could not be completed."
    open_items.append(f'Could not answer "{subquestion_text}": {explanation}')
    return _outcome("failed", open_items, evidence, recommendations, "failed")


def _outcome(
    status: SubquestionStatus,
    open_items: list[str],
    evidence: list[EvidenceItem],
    recommendations: list[dict],
    detail: str,
) -> dict:
    return {
        "status": status,
        "evidence": evidence,
        "open_items": open_items,
        "recommendations": recommendations,
        "detail": detail,
    }


def failed_outcome(subquestion_text: str) -> dict:
    """The outcome recorded when `run_agent` itself raised. The exception text
    is deliberately not included, so an internal error never reaches the user.
    The graph logs the full exception server-side."""
    return _outcome(
        "failed",
        [f'Could not answer "{subquestion_text}": an internal error stopped this step.'],
        [],
        [],
        "internal error",
    )
