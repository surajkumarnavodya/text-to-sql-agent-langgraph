"""Evidence-based root-cause/contribution analysis -- Prompt 14
(`14_ANOMALY_ROOT_CAUSE_CONTRACT.md`).

`investigate_root_cause` implements the prompt's own named flow --
anomaly (magnitude) -> baseline -> dimension breakdown -> contribution ->
ranking -> validation -> evidence -- as one function, each step a
clearly separated piece of it. Pure, deterministic, no LLM, no I/O, the
same posture every other module under `analytics/` already establishes.

**Acceptance criterion, enforced structurally, not by convention**: "no
root cause is stated without supporting evidence." This function has no
code path that populates `RootCauseResult.contributors` (or sets
`.confidence` to anything but `None`) unless at least one contributor
clears `Settings.root_cause_min_contribution_percent` in the validation
step -- `has_sufficient_evidence` is derived from `contributors` being
non-empty, never set independently of it.

**Deliberately standalone, not wired into any live LangGraph node in
this pass**: unlike `analytics.anomaly.detect_anomalies` (which only
ever needs the one time series `compute_analytics_result` already
computes), this function inherently needs *two* datasets -- the
anomalous period's dimensional breakdown and a baseline period's. This
application's pipeline generates exactly one SQL query per question
today; there is no existing mechanism that produces a second, comparison
dataset for one question. See `14_ANOMALY_ROOT_CAUSE_CONTRACT.md` for
the full, disclosed reasoning and the recommended follow-up prompt that
would close this gap.
"""

from __future__ import annotations

from analytics.engine import group_and_sum_by_label
from analytics.models import ANALYTICS_ENGINE_VERSION, ContributorStat, RootCauseResult
from config.settings import Settings, get_settings

_FORMULA = (
    "contribution_percent = 100 * (current - baseline) / magnitude; "
    "confidence = min(1.0, sum(|contribution_percent| of validated contributors) / 100)"
)


def investigate_root_cause(
    current_rows: list[tuple],
    baseline_rows: list[tuple],
    columns: list[str],
    dimension_columns: list[str],
    value_column: str,
    settings: Settings | None = None,
) -> RootCauseResult:
    """Investigates which dimension value(s) explain the change between
    `baseline_rows` and `current_rows`.

    Args:
        current_rows: Rows for the period under investigation (the one
            that moved) -- same `(columns, rows)` shape every other
            `analytics/` function takes.
        baseline_rows: Rows for the comparison/"normal" period, using the
            identical `columns` layout.
        columns: Column names shared by both `current_rows` and
            `baseline_rows`, in the same order.
        dimension_columns: Which column(s) to break the change down by
            (e.g. `["Region"]`, or `["Category", "Region"]` for a
            composite, multidimensional breakdown -- see
            `analytics.engine.group_and_sum_by_label`'s own docstring for
            how a multi-column breakdown becomes one flattened label).
        value_column: Which column holds the metric being investigated.
        settings: Defaults to `config.settings.get_settings()`.

    Returns:
        A `RootCauseResult` -- `magnitude`/`baseline_total`/
        `current_total` are always populated when computable; `contributors`/
        `confidence` are populated **only** when at least one contributor
        clears `Settings.root_cause_min_contribution_percent`
        (`has_sufficient_evidence=True`); otherwise both stay empty/`None`
        and `limitations` explains why -- see this module's own docstring
        for why that's never optional.
    """
    settings = settings or get_settings()
    limitations: list[str] = []

    if not current_rows or not baseline_rows:
        limitations.append(
            "no data for the current period or the baseline period -- nothing to compare"
        )
        return RootCauseResult(
            has_sufficient_evidence=False, limitations=tuple(limitations), formula=_FORMULA
        )

    try:
        value_idx = columns.index(value_column)
    except ValueError:
        limitations.append(f"value column {value_column!r} not found in columns")
        return RootCauseResult(
            has_sufficient_evidence=False, limitations=tuple(limitations), formula=_FORMULA
        )

    # Step 1: anomaly (magnitude) + step 2: baseline.
    current_total = sum(row[value_idx] for row in current_rows if row[value_idx] is not None)
    baseline_total = sum(row[value_idx] for row in baseline_rows if row[value_idx] is not None)
    magnitude = current_total - baseline_total
    magnitude_percent = (
        round(100 * magnitude / abs(baseline_total), 1) if baseline_total != 0 else None
    )

    if magnitude == 0:
        limitations.append("no change between the baseline and current period totals")
        return RootCauseResult(
            has_sufficient_evidence=False,
            magnitude=magnitude,
            magnitude_percent=magnitude_percent,
            baseline_total=baseline_total,
            current_total=current_total,
            limitations=tuple(limitations),
            formula=_FORMULA,
        )

    if not dimension_columns:
        limitations.append("no dimension_columns given -- nothing to break the change down by")
        return RootCauseResult(
            has_sufficient_evidence=False,
            magnitude=magnitude,
            magnitude_percent=magnitude_percent,
            baseline_total=baseline_total,
            current_total=current_total,
            limitations=tuple(limitations),
            formula=_FORMULA,
        )

    try:
        dimension_indices = [columns.index(c) for c in dimension_columns]
    except ValueError as exc:
        limitations.append(f"dimension column not found in columns: {exc}")
        return RootCauseResult(
            has_sufficient_evidence=False,
            magnitude=magnitude,
            magnitude_percent=magnitude_percent,
            baseline_total=baseline_total,
            current_total=current_total,
            limitations=tuple(limitations),
            formula=_FORMULA,
        )

    # Step 3: dimension breakdown.
    current_by_label, _, _ = group_and_sum_by_label(
        columns, current_rows, value_idx, dimension_indices
    )
    baseline_by_label, _, _ = group_and_sum_by_label(
        columns, baseline_rows, value_idx, dimension_indices
    )

    # Step 4: contribution, over the union of labels seen in either period
    # (a label present in only one period is a genuine new/dropped
    # contributor, not an error -- the missing side defaults to 0.0).
    all_labels = set(current_by_label) | set(baseline_by_label)
    contributions: list[tuple[str, float, float, float, float]] = []
    for label in all_labels:
        current_value = current_by_label.get(label, 0.0)
        baseline_value = baseline_by_label.get(label, 0.0)
        contribution = current_value - baseline_value
        contribution_percent = round(100 * contribution / magnitude, 1)
        contributions.append(
            (label, baseline_value, current_value, contribution, contribution_percent)
        )

    # Step 5: ranking, by |contribution| descending.
    contributions.sort(key=lambda item: abs(item[3]), reverse=True)

    # Step 6: validation -- only a contributor clearing the configured
    # bar is real evidence; everything else is counted, not detailed.
    min_percent = settings.root_cause_min_contribution_percent
    validated: list[ContributorStat] = []
    excluded_count = 0
    for rank, (
        label,
        baseline_value,
        current_value,
        contribution,
        contribution_percent,
    ) in enumerate(contributions, start=1):
        if abs(contribution_percent) >= min_percent:
            validated.append(
                ContributorStat(
                    label=label,
                    baseline_value=baseline_value,
                    current_value=current_value,
                    contribution=contribution,
                    contribution_percent=contribution_percent,
                    rank=rank,
                )
            )
        else:
            excluded_count += 1

    if excluded_count:
        limitations.append(
            f"{excluded_count} dimension value(s) did not individually explain at least "
            f"{min_percent:g}% of the change and were excluded as noise, not evidence"
        )

    # Step 7: evidence.
    if not validated:
        limitations.append(
            "no single dimension value explains at least "
            f"{min_percent:g}% of the change -- the change appears broadly distributed, "
            "not attributable to a specific contributor"
        )
        return RootCauseResult(
            has_sufficient_evidence=False,
            magnitude=magnitude,
            magnitude_percent=magnitude_percent,
            baseline_total=baseline_total,
            current_total=current_total,
            limitations=tuple(limitations),
            formula=_FORMULA,
        )

    confidence = min(1.0, round(sum(abs(c.contribution_percent) for c in validated) / 100, 2))
    return RootCauseResult(
        has_sufficient_evidence=True,
        magnitude=magnitude,
        magnitude_percent=magnitude_percent,
        baseline_total=baseline_total,
        current_total=current_total,
        contributors=tuple(validated),
        confidence=confidence,
        limitations=tuple(limitations),
        formula=_FORMULA,
        engine_version=ANALYTICS_ENGINE_VERSION,
    )
