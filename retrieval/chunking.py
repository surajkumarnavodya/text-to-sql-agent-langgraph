"""Turns live-introspected schema + `data/knowledge/*` files into typed `Chunk`s.

This module never talks to a vector store or an embedding model -- it is
pure data transformation, which is what makes it trivially unit-testable
(see `tests/test_chunking.py`) without mocking either. `ingestion.py` is the
only caller that wires this together with `embeddings.py`/`vector_store.py`.

Table/column/relationship chunks are built from `db.schema_introspection
.TableSchemaInfo` -- the exact same live-introspected objects
`embeddings/schema_indexer.py` already embeds for the existing schema-DDL
collection. This module does **not** replace that collection or its
retrieval path (`embeddings/retriever.py`, still the source of the DDL
actually injected into the generation prompt) -- it adds a second, richer
representation (one chunk per table *and* per column *and* per relationship,
each independently retrievable and rerankable) for the *business-context*
side of retrieval: "what does this column mean," "how do these tables join
and why," not "what are this table's columns" (which `schema_context_text`
already answers authoritatively). See `docs/vector-retrieval-design.md`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from semantic.catalog import CatalogEntrySnapshot

from config.sensitive_columns import SensitivityTier
from config.table_descriptions import TableDescription
from db.relationship_inference import InferredRelationship
from db.schema_introspection import TableSchemaInfo
from retrieval.models import Chunk, ChunkType, Sensitivity, compute_content_hash, make_chunk_id
from security.sanitization import normalize_text

logger = logging.getLogger(__name__)

_TIER_TO_SENSITIVITY: dict[SensitivityTier, Sensitivity] = {
    "public": Sensitivity.NORMAL,
    "internal": Sensitivity.CONFIDENTIAL,
    "restricted": Sensitivity.RESTRICTED,
}


def _make_chunk(
    *,
    chunk_type: ChunkType,
    text: str,
    database_id: str,
    object_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_name: str | None = None,
    table_name: str | None = None,
    column_name: str | None = None,
    source_id: str | None = None,
    version: int = 1,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    tags: tuple[str, ...] = (),
    allowed_roles: tuple[str, ...] = (),
    source_updated_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Chunk:
    """Shared chunk-assembly helper -- every `*_chunks_from_*` builder below
    goes through this so `chunk_id`/`content_hash` are always computed the
    same way (see `retrieval.models`'s docstring for why they're two
    different, deliberately non-random hashes)."""
    extra = extra or {}
    chunk_id = make_chunk_id(
        database_id, schema_name, object_name, chunk_type, column_name, version
    )
    return Chunk(
        chunk_id=chunk_id,
        chunk_type=chunk_type,
        text=text,
        database_id=database_id,
        schema_name=schema_name,
        table_name=table_name,
        column_name=column_name,
        source_id=source_id or object_name,
        content_hash=compute_content_hash(text, extra),
        version=version,
        sensitivity=sensitivity,
        tags=tags,
        allowed_roles=allowed_roles,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        source_updated_at=source_updated_at,
        extra=extra,
    )


def table_chunks_from_schema(
    tables: list[TableSchemaInfo],
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_name: str | None = None,
    descriptions: dict[str, TableDescription] | None = None,
) -> list[Chunk]:
    """One `table`-type chunk per table: purpose, key columns, PK, FKs.

    `descriptions` (`config/table_descriptions.yaml`, loaded fresh by the
    caller -- see that module's own "why uncached" docstring) supplies the
    hand-authored purpose/relationship notes when available; a table with no
    entry still gets a chunk, just without that prose -- a chunk always
    exists for every real table, since "this table exists and has these
    columns" is itself useful retrievable context even with zero curation.
    """
    descriptions = descriptions or {}
    chunks: list[Chunk] = []
    for table in tables:
        description = descriptions.get(table.table_name)
        pk_columns = [c.name for c in table.columns if c.is_primary_key]
        fk_lines = [
            f"{', '.join(fk.constrained_columns)} -> "
            f"{fk.referred_table}({', '.join(fk.referred_columns)})"
            for fk in table.foreign_keys
        ]
        important_columns = [c.name for c in table.columns[:12]]

        lines = [f"Table {table.table_name}."]
        if description and description.purpose:
            lines.append(f"Purpose: {normalize_text(description.purpose)}")
        if pk_columns:
            lines.append(f"Primary key: {', '.join(pk_columns)}.")
        if fk_lines:
            lines.append("Foreign keys: " + "; ".join(fk_lines) + ".")
        if description and description.key_relationships:
            lines.append(f"Key relationships: {normalize_text(description.key_relationships)}")
        lines.append(f"Columns ({len(table.columns)} total): {', '.join(important_columns)}.")
        text = "\n".join(lines)

        chunks.append(
            _make_chunk(
                chunk_type=ChunkType.TABLE,
                text=text,
                database_id=database_id,
                object_name=table.table_name,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                schema_name=schema_name,
                table_name=table.table_name,
                source_id=f"table:{table.table_name}",
                extra={
                    "primary_key": pk_columns,
                    "foreign_keys": fk_lines,
                    "column_count": len(table.columns),
                },
            )
        )
    return chunks


def column_chunks_from_schema(
    tables: list[TableSchemaInfo],
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_name: str | None = None,
    column_notes: dict[str, dict[str, str]] | None = None,
    sensitive_columns: dict[tuple[str, str], SensitivityTier] | None = None,
) -> list[Chunk]:
    """One `column`-type chunk per column: type, nullability, key role, notes.

    `column_notes` is `{table_name: {column_name: note}}`, built by the
    caller from `config/table_descriptions.yaml`'s per-table
    `column_notes`. `sensitive_columns` (`config/sensitive_columns.yaml` via
    `config.sensitive_columns.load_sensitive_columns`) sets `sensitivity` --
    a "restricted" column's chunk is still generated (its *existence* and
    *type* are still useful retrieval context) but tagged `RESTRICTED`,
    which `retriever.py` uses as a retrieval-time hint alongside the real
    enforcement point (`agent.nodes.validate_sql_node`'s restricted-column
    gate, unaffected by this package -- see this module's own docstring).
    """
    column_notes = column_notes or {}
    sensitive_columns = sensitive_columns or {}
    chunks: list[Chunk] = []
    for table in tables:
        table_notes = column_notes.get(table.table_name, {})
        for column in table.columns:
            tier = sensitive_columns.get((table.table_name, column.name), "public")
            sensitivity = _TIER_TO_SENSITIVITY.get(tier, Sensitivity.NORMAL)
            note = table_notes.get(column.name)

            key_role = "primary key" if column.is_primary_key else None
            lines = [
                f"Column {table.table_name}.{column.name}, type {column.type}"
                f"{', nullable' if column.nullable else ', not nullable'}."
            ]
            if key_role:
                lines.append(f"Role: {key_role}.")
            if note:
                lines.append(f"Note: {normalize_text(note)}")
            if sensitivity is not Sensitivity.NORMAL:
                lines.append(f"Sensitivity: {sensitivity.value}.")
            text = " ".join(lines)

            chunks.append(
                _make_chunk(
                    chunk_type=ChunkType.COLUMN,
                    text=text,
                    database_id=database_id,
                    object_name=table.table_name,
                    embedding_model=embedding_model,
                    embedding_dimensions=embedding_dimensions,
                    schema_name=schema_name,
                    table_name=table.table_name,
                    column_name=column.name,
                    source_id=f"column:{table.table_name}.{column.name}",
                    sensitivity=sensitivity,
                    extra={
                        "data_type": column.type,
                        "nullable": column.nullable,
                        "is_primary_key": column.is_primary_key,
                    },
                )
            )
    return chunks


def relationship_chunks_from_schema(
    tables: list[TableSchemaInfo],
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_name: str | None = None,
) -> list[Chunk]:
    """One `relationship`-type chunk per foreign key -- a dedicated,
    independently retrievable join-path fact, distinct from the FK line
    already folded into that table's own `table` chunk above. A question
    like "how do I join products to their category" benefits from a chunk
    whose *entire* content is that one join, rather than needing the whole
    table chunk to rank highly first.
    """
    chunks: list[Chunk] = []
    for table in tables:
        for fk in table.foreign_keys:
            source_cols = ", ".join(fk.constrained_columns)
            target_cols = ", ".join(fk.referred_columns)
            join_condition = " AND ".join(
                f"{table.table_name}.{sc} = {fk.referred_table}.{tc}"
                for sc, tc in zip(fk.constrained_columns, fk.referred_columns, strict=True)
            )
            object_name = f"{table.table_name}->{fk.referred_table}({source_cols})"
            text = (
                f"Relationship: {table.table_name} references {fk.referred_table} "
                f"via foreign key ({source_cols}) -> ({target_cols}). "
                f"Join condition: {join_condition}. "
                f"Recommended direction: join {table.table_name} to {fk.referred_table} "
                f"(many-to-one, {table.table_name} is the referencing side)."
            )
            chunks.append(
                _make_chunk(
                    chunk_type=ChunkType.RELATIONSHIP,
                    text=text,
                    database_id=database_id,
                    object_name=object_name,
                    embedding_model=embedding_model,
                    embedding_dimensions=embedding_dimensions,
                    schema_name=schema_name,
                    table_name=table.table_name,
                    source_id=f"relationship:{object_name}",
                    extra={
                        "source_table": table.table_name,
                        "source_columns": list(fk.constrained_columns),
                        "target_table": fk.referred_table,
                        "target_columns": list(fk.referred_columns),
                        "relationship_type": "many_to_one",
                        "join_condition": join_condition,
                    },
                )
            )
    return chunks


def inferred_relationship_chunks_from_schema(
    candidates: list[InferredRelationship],
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    schema_name: str | None = None,
) -> list[Chunk]:
    """One `relationship`-type chunk per inferred (never declared)
    candidate -- Prompt 07 (`07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`).

    Deliberately the same `ChunkType.RELATIONSHIP` a real, declared FK
    uses (`relationship_chunks_from_schema` above), not a second type --
    both are retrieved the same way, scored the same way, and shown under
    the same "Additional join/relationship hints" label
    (`agent.llm_client._BUSINESS_CONTEXT_TYPE_LABELS`). What keeps rule
    10 ("never silently promote an inference to confirmed truth") true is
    the chunk's own *text*, not a separate type or a field the LLM never
    sees (`extra` isn't rendered into the prompt -- see `retrieval.models
    .Chunk`'s own docstring): every inferred chunk's text opens with
    "CANDIDATE relationship (NOT a declared foreign key -- inferred, not
    confirmed", unmistakably distinct from a real relationship chunk's
    "Relationship: ..." opening, wherever the two are mixed together in
    the same retrieved/rendered group.

    `object_name` is prefixed `candidate:` specifically so a chunk ID
    here can never collide with a real relationship's chunk ID for the
    same source/target/columns -- in practice this never happens anyway
    (`db.relationship_inference.infer_relationships` already excludes any
    column that's part of a declared FK), but the prefix makes that
    non-collision structural rather than incidental.
    """
    chunks: list[Chunk] = []
    for candidate in candidates:
        source_cols = ", ".join(candidate.source_columns)
        target_cols = ", ".join(candidate.target_columns)
        join_condition = " AND ".join(
            f"{candidate.source_table}.{sc} = {candidate.target_table}.{tc}"
            for sc, tc in zip(candidate.source_columns, candidate.target_columns, strict=True)
        )
        evidence_summary = "; ".join(f"{e.signal}: {e.detail}" for e in candidate.evidence)
        object_name = f"candidate:{candidate.source_table}->{candidate.target_table}({source_cols})"
        text = (
            f"CANDIDATE relationship (NOT a declared foreign key -- inferred, not "
            f"confirmed; confidence {candidate.confidence:.2f}): {candidate.source_table} "
            f"may reference {candidate.target_table} via ({source_cols}) -> ({target_cols}). "
            f"Suggested join condition: {join_condition}. "
            f"Suggested direction: {candidate.relationship_type.replace('_', '-')}, "
            f"{candidate.source_table} as the referencing side. "
            f"Evidence: {evidence_summary}. "
            f"Verify this actually makes sense for the question before relying on it -- "
            f"it was inferred from schema/data signals, not declared by the database."
        )
        chunks.append(
            _make_chunk(
                chunk_type=ChunkType.RELATIONSHIP,
                text=text,
                database_id=database_id,
                object_name=object_name,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                schema_name=schema_name,
                table_name=candidate.source_table,
                source_id=object_name,
                extra={
                    "source_table": candidate.source_table,
                    "source_columns": list(candidate.source_columns),
                    "target_table": candidate.target_table,
                    "target_columns": list(candidate.target_columns),
                    "relationship_type": candidate.relationship_type,
                    "join_condition": join_condition,
                    "evidence_level": "inferred",
                    "truth_level": candidate.truth_level.value,
                    "confidence": candidate.confidence,
                    "evidence": [
                        {"signal": e.signal, "score": e.score, "detail": e.detail}
                        for e in candidate.evidence
                    ],
                },
            )
        )
    return chunks


def _load_yaml_list(path: Path, top_level_key: str) -> list[dict[str, Any]]:
    """Generic loader for `data/knowledge/*.yaml` -- mirrors
    `config.table_descriptions.load_table_descriptions`'s "missing file is
    not an error, return empty" contract exactly, since these knowledge
    files are equally optional, hand-authored, best-effort enrichment.
    """
    if not path.exists():
        logger.info("Knowledge file not found, skipping: %s", path)
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = raw.get(top_level_key) or []
    if not isinstance(records, list):
        logger.warning("Expected a list under %r in %s, got %s", top_level_key, path, type(records))
        return []
    return records


def glossary_chunks_from_yaml(
    path: Path,
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[Chunk]:
    """`glossary`-type chunks from `data/knowledge/glossary.yaml`.

    Expected shape (see that file's own header comment for the full
    example): a top-level `terms:` list, each entry a business term with
    `synonyms`, `definition`, `mapped_tables`/`mapped_columns`,
    `common_filters`, and an optional `ambiguity_notes` -- never split
    across chunks (this module's caller-facing contract, per
    `docs/vector-retrieval-design.md`'s chunking-rules section): one
    glossary term is always one chunk.
    """
    chunks: list[Chunk] = []
    for entry in _load_yaml_list(path, "terms"):
        term = entry.get("term")
        if not term:
            continue
        synonyms = list(entry.get("synonyms") or [])
        definition = normalize_text(entry.get("definition") or "")
        mapped_tables = list(entry.get("mapped_tables") or [])
        mapped_columns = list(entry.get("mapped_columns") or [])
        common_filters = list(entry.get("common_filters") or [])
        ambiguity_notes = normalize_text(entry.get("ambiguity_notes") or "")

        lines = [f"Business term: {term}."]
        if synonyms:
            lines.append(f"Also known as: {', '.join(synonyms)}.")
        if definition:
            lines.append(f"Definition: {definition}")
        if mapped_tables or mapped_columns:
            lines.append(
                "Maps to: "
                + (f"tables {', '.join(mapped_tables)}. " if mapped_tables else "")
                + (f"columns {', '.join(mapped_columns)}." if mapped_columns else "")
            )
        if common_filters:
            lines.append(f"Common filters: {', '.join(common_filters)}.")
        if ambiguity_notes:
            lines.append(f"Ambiguity notes: {ambiguity_notes}")
        text = "\n".join(lines)

        chunks.append(
            _make_chunk(
                chunk_type=ChunkType.GLOSSARY,
                text=text,
                database_id=database_id,
                object_name=term,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                table_name=mapped_tables[0] if len(mapped_tables) == 1 else None,
                source_id=f"glossary:{term}",
                tags=tuple(entry.get("tags") or ()),
                extra={
                    "synonyms": synonyms,
                    "mapped_tables": mapped_tables,
                    "mapped_columns": mapped_columns,
                    "common_filters": common_filters,
                },
            )
        )
    return chunks


def metric_chunks_from_yaml(
    path: Path,
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[Chunk]:
    """`metric`-type chunks from `data/knowledge/metrics.yaml`.

    Expected shape: a top-level `metrics:` list, each entry a named metric
    with `definition`, `formula`, `aggregation`, `grain`, `valid_filters`,
    `source_tables`/`source_columns`, and `time_period_interpretation` --
    never split across chunks, same rule as glossary terms above.
    """
    chunks: list[Chunk] = []
    for entry in _load_yaml_list(path, "metrics"):
        name = entry.get("name")
        if not name:
            continue
        definition = normalize_text(entry.get("definition") or "")
        formula = entry.get("formula") or ""
        aggregation = entry.get("aggregation") or ""
        grain = entry.get("grain") or ""
        valid_filters = list(entry.get("valid_filters") or [])
        source_tables = list(entry.get("source_tables") or [])
        source_columns = list(entry.get("source_columns") or [])
        time_period = entry.get("time_period_interpretation") or ""

        lines = [f"Metric: {name}."]
        if definition:
            lines.append(f"Definition: {definition}")
        if formula:
            lines.append(f"Formula: {formula}")
        if aggregation:
            lines.append(f"Aggregation method: {aggregation}.")
        if grain:
            lines.append(f"Grain: {grain}.")
        if source_tables or source_columns:
            lines.append(
                "Source: "
                + (f"tables {', '.join(source_tables)}. " if source_tables else "")
                + (f"columns {', '.join(source_columns)}." if source_columns else "")
            )
        if valid_filters:
            lines.append(f"Valid filters: {', '.join(valid_filters)}.")
        if time_period:
            lines.append(f"Time-period interpretation: {time_period}")
        text = "\n".join(lines)

        chunks.append(
            _make_chunk(
                chunk_type=ChunkType.METRIC,
                text=text,
                database_id=database_id,
                object_name=name,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                table_name=source_tables[0] if len(source_tables) == 1 else None,
                source_id=f"metric:{name}",
                tags=tuple(entry.get("tags") or ()),
                extra={
                    "formula": formula,
                    "aggregation": aggregation,
                    "grain": grain,
                    "valid_filters": valid_filters,
                    "source_tables": source_tables,
                    "source_columns": source_columns,
                },
            )
        )
    return chunks


def business_concept_chunk_from_catalog_entry(
    snapshot: CatalogEntrySnapshot,
    embedding_model: str,
    embedding_dimensions: int,
) -> Chunk:
    """`business_concept`-type chunk from one governed semantic-catalog
    entry -- Prompt 09 (`09_SEMANTIC_CATALOG_CONTRACT.md`).

    **Caller must only ever invoke this with a `status == PUBLISHED`
    snapshot.** This function itself performs no status check -- the
    actual, structural enforcement of "a draft/reviewed entry never
    reaches retrieval" is that `retrieval.ingestion
    .sync_catalog_entry_to_vector_store` (the only real caller, from
    `api/semantic_catalog.py`'s publish route) is itself only ever
    invoked on publish, never on create/review. Mirrors every other
    builder in this module (one concept, one chunk, never split).

    `chunk_id` is made unique per **version** (via `snapshot.version`,
    threaded through to `make_chunk_id`) -- publishing a new version of
    an already-published concept always produces a brand-new chunk id,
    never overwriting the version it supersedes; the caller is
    responsible for deleting the superseded version's own chunk id
    (`retrieval.ingestion.sync_catalog_entry_to_vector_store`'s
    `superseded_chunk_id` parameter).
    """
    object_name = f"{snapshot.concept_type.value}:{snapshot.concept_key}"
    lines = [f"{snapshot.concept_type.value.title()}: {snapshot.business_name}."]
    if snapshot.technical_name:
        lines.append(f"Technical name: {snapshot.technical_name}.")
    if snapshot.approved_expression:
        lines.append(f"Approved expression: {snapshot.approved_expression}")
    if snapshot.aggregation:
        lines.append(f"Aggregation: {snapshot.aggregation}.")
    if snapshot.description:
        lines.append(f"Description: {normalize_text(snapshot.description)}")
    if snapshot.grain:
        lines.append(f"Grain: {snapshot.grain}.")
    if snapshot.keys:
        lines.append(f"Keys: {', '.join(snapshot.keys)}.")
    if snapshot.source_tables:
        lines.append(f"Source tables: {', '.join(snapshot.source_tables)}.")
    if snapshot.filters:
        lines.append(f"Common filters: {', '.join(snapshot.filters)}.")
    if snapshot.dimensions:
        lines.append(f"Allowed breakdown dimensions: {', '.join(snapshot.dimensions)}.")
    if snapshot.relationships:
        rel_lines = [
            f"{r.get('related_concept_key', '?')} ({r.get('relationship_type', 'related to')})"
            for r in snapshot.relationships
        ]
        lines.append(f"Related concepts: {', '.join(rel_lines)}.")
    if snapshot.domain:
        lines.append(f"Domain: {snapshot.domain}.")
    if snapshot.synonyms:
        lines.append(f"Also known as: {', '.join(snapshot.synonyms)}.")
    if snapshot.business_rules:
        lines.append("Business rules: " + "; ".join(snapshot.business_rules) + ".")
    if snapshot.examples:
        lines.append("Examples: " + "; ".join(snapshot.examples))
    text = "\n".join(lines)

    return _make_chunk(
        chunk_type=ChunkType.BUSINESS_CONCEPT,
        text=text,
        database_id=snapshot.database_id,
        object_name=object_name,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        table_name=snapshot.technical_name,
        source_id=f"semantic_catalog:{snapshot.tenant_id}:{object_name}",
        version=snapshot.version,
        tags=snapshot.synonyms,
        extra={
            "concept_type": snapshot.concept_type.value,
            "concept_key": snapshot.concept_key,
            "business_name": snapshot.business_name,
            "status": snapshot.status.value,
            "confidence": snapshot.confidence,
            "evidence": list(snapshot.evidence),
            "owner": snapshot.owner,
            "version": snapshot.version,
            "truth_level": snapshot.truth_level.value,
            "approved_expression": snapshot.approved_expression,
            "aggregation": snapshot.aggregation,
            "source_tables": list(snapshot.source_tables),
            "filters": list(snapshot.filters),
            "dimensions": list(snapshot.dimensions),
        },
    )


def sql_example_chunks_from_yaml(
    path: Path,
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[Chunk]:
    """`sql_example`-type chunks from `data/knowledge/sql_examples.yaml`.

    Expected shape: a top-level `examples:` list, each entry a
    question/SQL pair with `explanation`, `tables_used`, `columns_used`,
    `tags`, and `validation_status` (e.g. `"reviewed"`/`"unreviewed"`).
    These are **curated, file-authored** examples -- distinct from
    `embeddings/golden_examples.py`'s user-feedback-driven store, which
    keeps accumulating from real "Confirm and Run" thumbs-up clicks. Both
    are surfaced to `generate_sql_node`'s prompt, in clearly separate
    labeled blocks (see `agent.llm_client._build_business_context_block`),
    so the model -- and a person reading the prompt -- can tell "a repo
    maintainer wrote and reviewed this" apart from "a user approved this
    result once." One question/SQL pair is always one chunk, never split.
    """
    chunks: list[Chunk] = []
    for entry in _load_yaml_list(path, "examples"):
        question = entry.get("question")
        sql = entry.get("sql")
        if not question or not sql:
            continue
        explanation = normalize_text(entry.get("explanation") or "")
        tables_used = list(entry.get("tables_used") or [])
        columns_used = list(entry.get("columns_used") or [])
        validation_status = entry.get("validation_status") or "unreviewed"

        lines = [f"Question: {question}", f"SQL:\n{sql}"]
        if explanation:
            lines.append(f"Explanation: {explanation}")
        if tables_used:
            lines.append(f"Tables used: {', '.join(tables_used)}.")
        text = "\n".join(lines)

        chunks.append(
            _make_chunk(
                chunk_type=ChunkType.SQL_EXAMPLE,
                text=text,
                database_id=database_id,
                object_name=question,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                table_name=tables_used[0] if len(tables_used) == 1 else None,
                source_id=f"sql_example:{question}",
                tags=tuple(entry.get("tags") or ()),
                extra={
                    "question": question,
                    "sql": sql,
                    "tables_used": tables_used,
                    "columns_used": columns_used,
                    "validation_status": validation_status,
                },
            )
        )
    return chunks


def _split_with_overlap(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    """Character-aware splitter with a small overlap, breaking on the
    nearest paragraph/whitespace boundary rather than mid-word where
    possible.

    Not a tokenizer-based splitter (this project has no tokenizer dependency
    for any other module either -- `agent/insight.py`'s token budgets are
    all character-based approximations too) -- character count is a
    reasonable, dependency-free proxy for prompt-size budgeting, consistent
    with the rest of this codebase's existing conventions.
    """
    if chunk_chars <= overlap_chars:
        raise ValueError("chunk_chars must be greater than overlap_chars")
    if len(text) <= chunk_chars:
        return [text] if text.strip() else []

    pieces: list[str] = []
    start = 0
    text_len = len(text)
    step = chunk_chars - overlap_chars
    while start < text_len:
        end = min(start + chunk_chars, text_len)
        if end < text_len:
            # Prefer breaking at the last paragraph/sentence/word boundary
            # inside the window, so a chunk doesn't end mid-word.
            boundary = text.rfind("\n\n", start, end)
            if boundary == -1 or boundary <= start:
                boundary = text.rfind(". ", start, end)
            if boundary == -1 or boundary <= start:
                boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= text_len:
            break
        start += step
    return pieces


def documentation_chunks_from_text(
    *,
    parent_document_id: str,
    title: str,
    content: str,
    source_path: str,
    database_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunk_chars: int,
    overlap_chars: int,
    version: int = 1,
    tags: tuple[str, ...] = (),
) -> list[Chunk]:
    """`documentation`-type chunks from one long document's text.

    Token/character-aware splitting with a small overlap (`_split_with_overlap`)
    -- long documentation is the one chunk type this package *does* split
    across multiple chunks (glossary/metric/sql_example/relationship chunks
    are never split, per this module's per-type docstrings above), since a
    full document can easily exceed any reasonable single-chunk budget.
    Every resulting chunk keeps `parent_document_id` in `extra` so a caller
    can still reconstruct "these N chunks all came from the same source
    document" even though they're independently retrievable and scored.
    """
    pieces = _split_with_overlap(content, chunk_chars, overlap_chars)
    chunks: list[Chunk] = []
    for index, piece in enumerate(pieces):
        object_name = f"{parent_document_id}#section-{index + 1}"
        text = f"Documentation: {title} (part {index + 1}/{len(pieces)}).\n{piece}"
        chunks.append(
            _make_chunk(
                chunk_type=ChunkType.DOCUMENTATION,
                text=text,
                database_id=database_id,
                object_name=object_name,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                source_id=f"documentation:{parent_document_id}",
                version=version,
                tags=tags,
                extra={
                    "parent_document_id": parent_document_id,
                    "section_title": title,
                    "section_index": index + 1,
                    "section_count": len(pieces),
                    "source_path": source_path,
                },
            )
        )
    return chunks


def load_documentation_files(directory: Path) -> list[tuple[str, str, str, str]]:
    """Reads every `.md`/`.txt` file under `directory` (non-recursive is
    fine for the expected small `data/knowledge/documentation/` scale, but
    this recurses anyway -- a future subfolder-per-topic layout should just
    work).

    Returns:
        `(parent_document_id, title, content, source_path)` tuples.
        `parent_document_id` is the file's relative path with `/` replaced
        by `__` (stable across runs, filesystem-safe). `title` is the first
        `# `-prefixed markdown heading if present, else the filename stem.
    """
    if not directory.exists():
        return []
    results: list[tuple[str, str, str, str]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".md", ".txt"):
            continue
        content = path.read_text(encoding="utf-8")
        relative = path.relative_to(directory).as_posix()
        parent_document_id = relative.replace("/", "__")
        title = path.stem
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        results.append((parent_document_id, title, content, str(path)))
    return results
