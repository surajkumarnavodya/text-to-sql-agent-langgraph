"""Infers candidate foreign-key-shaped relationships that aren't declared.

Prompt 07 (`07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`). A real, declared
foreign key (`db.schema_introspection.ForeignKeyInfo`) is a `DATABASE_FACT`
-- deterministic, no inference involved. This module is the opposite kind
of claim: an *inference* that two columns probably relate, built from
structural signals alone by default (name convention, type compatibility,
target-column uniqueness -- all catalog-metadata, no data query) and
optionally refined with a small, bounded sample of real data (null
fraction, value overlap -- explicitly opt-in, never a full scan).

Every result is tagged `agent.provenance.DataTruthLevel.AI_INFERENCE` and
carries itemized `evidence` -- never silently promoted to
`DATABASE_FACT`/`CONFIRMED_BUSINESS_TRUTH` (rule 10). Nothing in this
module ever executes against, or is fed into, `agent.sql_validator`'s
LLM-output gate; it's schema-analysis tooling that runs at
ingestion/discovery time, consumed downstream only as a clearly-labeled,
reference-only retrieval chunk (`retrieval.chunking
.inferred_relationship_chunks_from_schema`) -- see that module's own
"CANDIDATE relationship" framing.

**Deliberately single-column only** for this increment: a composite
(multi-column) candidate FK is a real, disclosed limitation, not
attempted here -- see this module's own "Known limitations" note in
`07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import Engine, Row, text
from sqlalchemy.exc import SQLAlchemyError

from agent.provenance import DataTruthLevel
from db.schema_introspection import ColumnInfo, TableSchemaInfo

logger = logging.getLogger(__name__)

# Warehouse-style table-name prefixes stripped before name-similarity
# matching -- "DimCustomer"'s meaningful core is "Customer", not "Dim". Not
# exhaustive (like this codebase's other honestly-disclosed heuristic
# lists, e.g. agent.sql_validator._DANGEROUS_FUNCTION_NAMES) -- a table
# named with a convention not listed here just gets no prefix stripped,
# which only *reduces* the match rate, never causes a false positive.
_TABLE_NAME_PREFIXES: tuple[str, ...] = ("dim", "fact", "bridge", "tbl", "stg")

# Suffixes conventionally used for a foreign-key-shaped column name
# (`CustomerKey`, `customer_id`, `CustomerID`, `CustomerCode`) once the
# target table's own core name has been matched as a prefix.
_FK_NAME_SUFFIXES: tuple[str, ...] = ("key", "id", "code", "no", "num", "number")

_TYPE_FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "integer": ("BIGINT", "SMALLINT", "TINYINT", "INT", "SERIAL"),
    "text": ("NVARCHAR", "VARCHAR", "NCHAR", "CHAR", "TEXT", "STRING", "CLOB"),
    "decimal": ("NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "MONEY"),
    "datetime": ("DATETIME", "TIMESTAMP", "DATE", "TIME"),
    "boolean": ("BOOL",),
    "uuid": ("UNIQUEIDENTIFIER", "UUID"),
}


@dataclass(frozen=True)
class RelationshipEvidence:
    """One piece of evidence contributing to a candidate's confidence.

    `score` is that signal's own 0..1 strength, kept alongside `detail`
    (a human-readable explanation) so a candidate's confidence is always
    explainable after the fact -- never a bare, unexplained float.
    """

    signal: str
    score: float
    detail: str


@dataclass(frozen=True)
class InferredRelationship:
    """One candidate relationship this module proposes -- never a
    declared fact. `truth_level` is always `AI_INFERENCE`; it's an
    explicit field (not just a module-level convention) so a consumer
    can assert on it directly rather than trusting a docstring.
    """

    source_table: str
    source_columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]
    relationship_type: str  # "many_to_one" or "one_to_one"
    confidence: float
    evidence: tuple[RelationshipEvidence, ...]
    truth_level: DataTruthLevel = DataTruthLevel.AI_INFERENCE


def type_family(type_str: str) -> str:
    """Classifies a column type string into a coarse family (`"integer"`,
    `"text"`, `"decimal"`, `"datetime"`, `"boolean"`, `"uuid"`, or
    `"other"`) -- public so `agent.plan_validator.validate_plan` (Prompt
    12, `12_ANALYTICAL_PLANNING_CONTRACT.md`) can reuse the identical
    classification this module already uses for structural FK-candidate
    type-compatibility checking, rather than a second, drifting copy."""
    upper = type_str.upper()
    for family, keywords in _TYPE_FAMILY_KEYWORDS.items():
        if any(keyword in upper for keyword in keywords):
            return family
    return "other"


def _normalize_table_core_name(table_name: str) -> str:
    """Strips a warehouse-style prefix and a trailing plural 's', lower-cased."""
    lowered = table_name.lower()
    for prefix in _TABLE_NAME_PREFIXES:
        if lowered.startswith(prefix) and len(lowered) > len(prefix):
            lowered = lowered[len(prefix) :]
            break
    if lowered.endswith("ies") and len(lowered) > 3:
        lowered = lowered[:-3] + "y"
    elif lowered.endswith("s") and not lowered.endswith("ss") and len(lowered) > 1:
        lowered = lowered[:-1]
    return lowered


def _name_similarity_evidence(
    source_column: str, target_table: str, target_column: str
) -> RelationshipEvidence | None:
    """Gates candidate generation on a real name signal -- no name match,
    no candidate, regardless of how well types/uniqueness line up. This
    is what keeps two type-compatible but unrelated columns (a
    "misleading names" case) from ever being proposed."""
    source_lower = source_column.lower()
    target_column_lower = target_column.lower()

    if source_lower == target_column_lower:
        return RelationshipEvidence(
            signal="name_similarity",
            score=1.0,
            detail=(
                f"column name '{source_column}' exactly matches target column " f"'{target_column}'"
            ),
        )

    target_core = _normalize_table_core_name(target_table)
    if target_core and source_lower.startswith(target_core):
        suffix = source_lower[len(target_core) :].lstrip("_")
        if suffix in _FK_NAME_SUFFIXES:
            return RelationshipEvidence(
                signal="name_similarity",
                score=0.85,
                detail=(
                    f"column name '{source_column}' follows the "
                    f"'{target_core}+{suffix}' foreign-key naming convention for "
                    f"target table '{target_table}'"
                ),
            )
    return None


def _single_column_unique_targets(table: TableSchemaInfo) -> dict[str, str]:
    """Column name -> a short description of why it's a legal FK target
    (the table's own PK, or a single-column unique constraint) -- a
    composite (multi-column) key is never offered as a target in this
    increment (disclosed limitation, see module docstring)."""
    targets: dict[str, str] = {}
    pk_columns = [c.name for c in table.columns if c.is_primary_key]
    if len(pk_columns) == 1:
        targets[pk_columns[0]] = "the primary key"
    for constraint in table.unique_constraints:
        if len(constraint) == 1 and constraint[0] not in targets:
            targets[constraint[0]] = "a unique constraint"
    return targets


def infer_relationships(
    tables: list[TableSchemaInfo], *, min_confidence: float = 0.6
) -> list[InferredRelationship]:
    """Infers candidate FK-shaped relationships from structural signals only.

    Never issues a query -- everything here is derived from already-
    introspected `TableSchemaInfo` metadata (column names/types, PK/unique
    constraints). Skips a column that's already part of a *declared* FK
    for that table (nothing to infer -- it's already a `DATABASE_FACT`),
    and skips views entirely (a view's own relationships are whatever its
    underlying base tables already declare or have inferred for them).

    Args:
        tables: Every table/view in one database (typically
            `db.schema_introspection.introspect_schema`'s output).
        min_confidence: Candidates below this are dropped outright, not
            just sorted last -- a low-confidence guess is worse than no
            guess at all in a reference-only retrieval chunk a model might
            over-trust.

    Returns:
        Candidates sorted by descending confidence, every one carrying
        `truth_level=AI_INFERENCE` and itemized `evidence`.
    """
    declared_fk_source_columns = {
        (table.table_name, column)
        for table in tables
        for fk in table.foreign_keys
        for column in fk.constrained_columns
    }

    candidates: list[InferredRelationship] = []
    for source_table in tables:
        if source_table.is_view:
            continue
        for source_column in source_table.columns:
            if (source_table.table_name, source_column.name) in declared_fk_source_columns:
                continue
            for target_table in tables:
                if target_table.table_name == source_table.table_name or target_table.is_view:
                    continue
                for target_column_name, target_reason in _single_column_unique_targets(
                    target_table
                ).items():
                    candidate = _build_candidate(
                        source_table,
                        source_column,
                        target_table,
                        target_column_name,
                        target_reason,
                    )
                    if candidate is not None and candidate.confidence >= min_confidence:
                        candidates.append(candidate)

    return sorted(
        candidates,
        key=lambda c: (-c.confidence, c.source_table, c.source_columns[0]),
    )


def _build_candidate(
    source_table: TableSchemaInfo,
    source_column: ColumnInfo,
    target_table: TableSchemaInfo,
    target_column_name: str,
    target_reason: str,
) -> InferredRelationship | None:
    name_evidence = _name_similarity_evidence(
        source_column.name, target_table.table_name, target_column_name
    )
    if name_evidence is None:
        return None

    target_column = next(c for c in target_table.columns if c.name == target_column_name)
    source_family = type_family(source_column.type)
    target_family = type_family(target_column.type)
    if source_family == "other" or source_family != target_family:
        # Incompatible types -- a hard rejection, not a lower score: an FK
        # between mismatched type families couldn't work in practice.
        return None

    type_evidence = RelationshipEvidence(
        signal="type_compatibility",
        score=1.0,
        detail=(
            f"'{source_column.type}' and '{target_column.type}' are both "
            f"{source_family}-family types"
        ),
    )
    uniqueness_evidence = RelationshipEvidence(
        signal="target_uniqueness",
        score=1.0,
        detail=(
            f"target column '{target_column_name}' on '{target_table.table_name}' is "
            f"{target_reason}"
        ),
    )

    is_one_to_one = source_column.is_primary_key or any(
        constraint == (source_column.name,) for constraint in source_table.unique_constraints
    )

    # Both hard gates (type compatibility, target uniqueness) already
    # passed just to reach here, so they contribute a fixed floor;
    # confidence is otherwise driven by how strong the name match itself
    # was (exact match vs. naming-convention match).
    confidence = 0.5 + name_evidence.score * 0.5

    return InferredRelationship(
        source_table=source_table.table_name,
        source_columns=(source_column.name,),
        target_table=target_table.table_name,
        target_columns=(target_column_name,),
        relationship_type="one_to_one" if is_one_to_one else "many_to_one",
        confidence=round(min(confidence, 1.0), 4),
        evidence=(name_evidence, type_evidence, uniqueness_evidence),
    )


# --- Opt-in, bounded, data-driven refinement (never a full table scan) ---

_MAX_SAMPLE_SIZE = 500


def _quote(engine: Engine, identifier: str) -> str:
    return engine.dialect.identifier_preparer.quote(identifier)


def _fetch_bounded(
    engine: Engine, sql: str, params: dict[str, Any] | None, sample_size: int
) -> Sequence[Row[Any]]:
    """Fetches at most `sample_size` rows -- bounded via `fetchmany()`
    alone, the exact same established pattern `db.value_sampling
    ._sample_column` already uses (no LIMIT/TOP text needed, works
    identically across all four dialects)."""
    with engine.connect() as connection:
        cursor_result = connection.execute(text(sql), params or {})
        return cursor_result.fetchmany(sample_size)


def _sample_null_fraction(
    engine: Engine, table_name: str, column_name: str, schema: str | None, sample_size: int
) -> float | None:
    qualified = (
        f"{_quote(engine, schema)}.{_quote(engine, table_name)}"
        if schema
        else _quote(engine, table_name)
    )
    quoted_column = _quote(engine, column_name)
    rows = _fetch_bounded(
        engine, f"SELECT {quoted_column} FROM {qualified}", None, sample_size  # nosec B608
    )
    if not rows:
        return None
    return sum(1 for row in rows if row[0] is None) / len(rows)


def _sample_value_overlap(
    engine: Engine,
    candidate: InferredRelationship,
    schema: str | None,
    sample_size: int,
) -> float | None:
    source_qualified = (
        f"{_quote(engine, schema)}.{_quote(engine, candidate.source_table)}"
        if schema
        else _quote(engine, candidate.source_table)
    )
    quoted_source_column = _quote(engine, candidate.source_columns[0])
    sample_rows = _fetch_bounded(
        engine,
        f"SELECT {quoted_source_column} FROM {source_qualified} "  # nosec B608
        f"WHERE {quoted_source_column} IS NOT NULL",
        None,
        sample_size,
    )
    sampled_values = [row[0] for row in sample_rows]
    if not sampled_values:
        return None

    target_qualified = (
        f"{_quote(engine, schema)}.{_quote(engine, candidate.target_table)}"
        if schema
        else _quote(engine, candidate.target_table)
    )
    quoted_target_column = _quote(engine, candidate.target_columns[0])
    placeholders = {f"v{i}": value for i, value in enumerate(sampled_values)}
    in_clause = ", ".join(f":{name}" for name in placeholders)
    found_rows = _fetch_bounded(
        engine,
        f"SELECT DISTINCT {quoted_target_column} FROM {target_qualified} "  # nosec B608
        f"WHERE {quoted_target_column} IN ({in_clause})",
        placeholders,
        len(sampled_values),
    )
    return len({row[0] for row in found_rows}) / len(set(sampled_values))


def verify_candidates_with_data(
    candidates: list[InferredRelationship],
    engine: Engine,
    schema: str | None = None,
    sample_size: int = 200,
) -> list[InferredRelationship]:
    """Refines each candidate's confidence with a small, bounded sample of
    real data -- explicitly opt-in (`Settings
    .enable_relationship_data_verification`, default off), never a full
    table scan (bounded via `fetchmany`, the same established pattern
    `db.value_sampling` already uses, not a second implementation of it).

    Two additional signals, each appended to `evidence`:
      - **Null fraction** of the source column (heavy nulls weaken
        confidence in a real key relationship -- a key column is rarely
        mostly-NULL in practice).
      - **Value overlap**: what fraction of sampled non-NULL source
        values are actually found in the target column. This is the
        strongest real-world signal available short of a full join, and
        pulls confidence toward it once measured.

    Args:
        sample_size: Capped at `_MAX_SAMPLE_SIZE` regardless of what's
            passed -- defense in depth against an accidentally huge value,
            mirroring `db.value_sampling`'s own fixed caps.

    Returns:
        A new list (candidates are frozen dataclasses) -- unchanged
        entries where the data probe itself failed (fails open: a probe
        error never removes or downgrades a structurally-sound candidate,
        it just skips the refinement for that one candidate).
    """
    bounded_sample_size = min(sample_size, _MAX_SAMPLE_SIZE)
    refined: list[InferredRelationship] = []
    for candidate in candidates:
        try:
            null_fraction = _sample_null_fraction(
                engine,
                candidate.source_table,
                candidate.source_columns[0],
                schema,
                bounded_sample_size,
            )
            overlap_fraction = _sample_value_overlap(engine, candidate, schema, bounded_sample_size)
        except SQLAlchemyError as exc:
            logger.debug(
                "[relationship_inference] data verification failed for %s.%s -> %s.%s: %s",
                candidate.source_table,
                candidate.source_columns[0],
                candidate.target_table,
                candidate.target_columns[0],
                exc,
            )
            refined.append(candidate)
            continue

        evidence = list(candidate.evidence)
        confidence = candidate.confidence
        if null_fraction is not None:
            evidence.append(
                RelationshipEvidence(
                    signal="null_fraction",
                    score=1 - null_fraction,
                    detail=f"{null_fraction:.0%} of sampled source values are NULL",
                )
            )
            if null_fraction > 0.5:
                confidence *= 0.7
        if overlap_fraction is not None:
            evidence.append(
                RelationshipEvidence(
                    signal="value_overlap",
                    score=overlap_fraction,
                    detail=(
                        f"{overlap_fraction:.0%} of sampled non-NULL source values were "
                        "found in the target column"
                    ),
                )
            )
            confidence = confidence * 0.5 + overlap_fraction * 0.5

        refined.append(
            replace(candidate, confidence=round(min(confidence, 1.0), 4), evidence=tuple(evidence))
        )
    return refined
