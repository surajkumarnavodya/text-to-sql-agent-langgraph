"""Infers a plausible business-semantic label per column, flagging
ambiguous cases for SME review -- Prompt 08
(`08_ONBOARDING_ENGINE_CONTRACT.md`)'s "semantic inference" +
"ambiguity detection" stages.

Synthesizes signals already produced by earlier stages (this module
computes nothing from a live connection itself -- it takes already-
introspected schema, already-inferred relationships, and already-
computed column profiles as plain input): a column's declared/inferred
key-ness, its type family, and its profiled cardinality/distribution.
Every label is a candidate among possibly several plausible ones for the
same column -- `is_ambiguous` is set whenever the best candidate isn't
clearly ahead of the next-best one, which is the literal mechanism behind
this prompt's acceptance criterion ("ambiguous business meaning is
routed to SME review").
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.provenance import DataTruthLevel
from db.relationship_inference import InferredRelationship
from db.schema_introspection import ColumnInfo, TableSchemaInfo
from onboarding.profiling import ColumnProfile

_DATETIME_TYPE_MARKERS = ("DATE", "TIME", "TIMESTAMP")
_NUMERIC_TYPE_MARKERS = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "MONEY")
_BOOLEAN_TYPE_MARKERS = ("BOOL", "BIT")

# A candidate is only declared ambiguous when the best-scoring label isn't
# clearly ahead of the runner-up -- this margin is what "clearly ahead"
# means. Also ambiguous outright below this absolute confidence floor,
# even with no close runner-up (a single weak guess is still a guess).
_AMBIGUITY_MARGIN = 0.15
_MIN_CONFIDENT_SCORE = 0.65


def _looks_datetime(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _DATETIME_TYPE_MARKERS)


def _looks_numeric(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _NUMERIC_TYPE_MARKERS)


def _looks_boolean(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _BOOLEAN_TYPE_MARKERS)


@dataclass(frozen=True)
class SemanticLabel:
    """The best-scoring candidate label for one column, plus enough of
    the runner-up's own score to explain why this is (or isn't)
    ambiguous. `truth_level` is always `AI_INFERENCE`."""

    table_name: str
    column_name: str
    label: str
    confidence: float
    is_ambiguous: bool
    evidence: tuple[str, ...]
    truth_level: DataTruthLevel = DataTruthLevel.AI_INFERENCE


def _candidate_labels(
    table: TableSchemaInfo,
    column: ColumnInfo,
    fk_source_columns: set[str],
    profile: ColumnProfile | None,
) -> list[tuple[str, float, str]]:
    """Every plausible (label, score, evidence) triple for one column --
    deliberately returns several when genuinely plausible, so the caller
    can detect a close tie rather than this function silently picking one."""
    candidates: list[tuple[str, float, str]] = []

    if column.is_primary_key:
        candidates.append(("identifier", 1.0, "is the table's own primary key"))
    if column.name in fk_source_columns:
        candidates.append(("foreign_key", 0.9, "is part of a declared or accepted foreign key"))

    if _looks_boolean(column.type):
        candidates.append(("flag", 0.9, "column type is boolean-shaped"))
    elif _looks_datetime(column.type):
        candidates.append(("timestamp", 0.9, "column type is date/time-shaped"))
    elif _looks_numeric(column.type):
        if profile is not None and profile.distinct_count == 2:
            candidates.append(
                ("flag", 0.8, "numeric column with exactly 2 distinct sampled values")
            )
        if profile is not None and profile.distinct_count > 0 and profile.distinct_count <= 20:
            candidates.append(
                (
                    "category",
                    0.65,
                    f"numeric column with only {profile.distinct_count} distinct sampled values",
                )
            )
        is_non_key = not column.is_primary_key and column.name not in fk_source_columns
        looks_continuous = (
            profile is None or profile.uniqueness_ratio > 0.5 or profile.distinct_count > 20
        )
        if is_non_key and looks_continuous:
            candidates.append(("measure", 0.7, "numeric, non-key column"))
    else:
        # Text/other: categorical vs. free text, driven by the profiled
        # cardinality (top_values is only ever populated for a column
        # profile.py itself already judged low-cardinality).
        if profile is not None and profile.top_values:
            candidates.append(
                ("category", 0.75, f"only {profile.distinct_count} distinct sampled values")
            )
        elif profile is not None:
            candidates.append(("free_text", 0.6, "high-cardinality text column"))
        else:
            candidates.append(("free_text", 0.4, "text column, not yet profiled"))

    return candidates


def infer_semantic_labels(
    tables: list[TableSchemaInfo],
    relationships: list[InferredRelationship] | None = None,
    column_profiles: dict[tuple[str, str], ColumnProfile] | None = None,
) -> list[SemanticLabel]:
    """One `SemanticLabel` per non-view column.

    Args:
        tables: Already-introspected tables (views are skipped entirely
            -- a view's own semantics are whatever its underlying base
            tables already have).
        relationships: Already-inferred candidates (`db
            .relationship_inference.infer_relationships`), used only to
            recognize a column as `"foreign_key"`-shaped alongside any
            *declared* FK already on the table itself.
        column_profiles: `{(table_name, column_name): ColumnProfile}` --
            optional; a column with no profile available still gets a
            label (from type/key-ness alone), just a less confident one.
    """
    relationships = relationships or []
    column_profiles = column_profiles or {}

    inferred_fk_by_table: dict[str, set[str]] = {}
    for candidate in relationships:
        inferred_fk_by_table.setdefault(candidate.source_table, set()).update(
            candidate.source_columns
        )

    labels: list[SemanticLabel] = []
    for table in tables:
        if table.is_view:
            continue
        declared_fk_columns = {
            column for fk in table.foreign_keys for column in fk.constrained_columns
        }
        fk_source_columns = declared_fk_columns | inferred_fk_by_table.get(table.table_name, set())

        for column in table.columns:
            profile = column_profiles.get((table.table_name, column.name))
            candidates = _candidate_labels(table, column, fk_source_columns, profile)
            candidates.sort(key=lambda c: -c[1])
            best_label, best_score, best_evidence = candidates[0]
            runner_up_score = candidates[1][1] if len(candidates) > 1 else 0.0
            is_ambiguous = (
                best_score < _MIN_CONFIDENT_SCORE
                or (best_score - runner_up_score) < _AMBIGUITY_MARGIN
            )
            evidence = tuple(
                f"{label}: {detail} (score {score:.2f})" for label, score, detail in candidates
            )
            labels.append(
                SemanticLabel(
                    table_name=table.table_name,
                    column_name=column.name,
                    label=best_label,
                    confidence=round(best_score, 4),
                    is_ambiguous=is_ambiguous,
                    evidence=evidence,
                )
            )
    return labels
