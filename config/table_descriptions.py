"""Loads `config/table_descriptions.yaml` -- hand-authored table/column notes.

Per that file's own header: it is a hand-reviewed source of truth for table
*meaning*, not something code generates or overwrites. This module only
reads it. A table or column with no entry simply gets no extra context --
never an error -- since the file is deliberately incomplete until reviewed
entry by entry.

Deliberately uncached: `load_table_descriptions()` re-reads and re-parses
the file from disk on every call. `agent.nodes.retrieve_schema_node` calls
it fresh on every single question (not once at embedding-build time, and
not memoized), specifically so a hand-edit to this file -- fixing a wrong
note, adding a new disambiguation -- takes effect on the very next question
asked, with no embeddings rebuild required. The file is small and local, so
re-parsing it per question is cheap next to the LLM call it feeds into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent / "table_descriptions.yaml"


@dataclass(frozen=True)
class TableDescription:
    """One table's hand-authored notes, as loaded from the YAML file."""

    purpose: str
    key_relationships: str
    column_notes: dict[str, str] = field(default_factory=dict)


def load_table_descriptions(path: Path | None = None) -> dict[str, TableDescription]:
    """Returns table_name -> `TableDescription` for every entry in the YAML file.

    Args:
        path: Override for the YAML file location (mainly for tests).
            Defaults to `config/table_descriptions.yaml`.

    Returns:
        An empty dict if the file is missing -- the descriptions are a
        best-effort enrichment, not a hard dependency; callers must work
        (just with less context) when this returns {}.
    """
    resolved_path = path or _DEFAULT_PATH
    if not resolved_path.exists():
        return {}

    raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    descriptions: dict[str, TableDescription] = {}
    for entry in raw.get("tables", []):
        table_name = entry.get("table_name")
        if not table_name:
            continue
        column_notes = {
            note["column"]: note["note"]
            for note in entry.get("column_notes", [])
            if note.get("column") and note.get("note")
        }
        descriptions[table_name] = TableDescription(
            purpose=(entry.get("purpose") or "").strip(),
            key_relationships=(entry.get("key_relationships") or "").strip(),
            column_notes=column_notes,
        )
    return descriptions


def apply_table_description(ddl: str, description: TableDescription | None) -> str:
    """Appends hand-authored purpose/relationship/column notes as DDL comments.

    Called from `agent.nodes.retrieve_schema_node` on every question, against
    the freshest `load_table_descriptions()` result -- never baked into the
    Chroma-embedded document at build time -- so an edit to
    `table_descriptions.yaml` is reflected in the very next generation
    prompt. Tables with no entry get `ddl` back unchanged; expected for most
    tables until the file is fully reviewed (see its header).
    """
    if description is None:
        return ddl

    lines = [ddl]
    if description.purpose:
        lines.append(f"-- Purpose: {description.purpose}")
    if description.key_relationships:
        lines.append(f"-- Relationships: {description.key_relationships}")
    for column, note in description.column_notes.items():
        lines.append(f"-- {column}: {note}")
    return "\n".join(lines)
