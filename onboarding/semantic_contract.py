"""Assembles SME-confirmed review decisions into a draft semantic
contract -- Prompt 08 (`08_ONBOARDING_ENGINE_CONTRACT.md`)'s "semantic
contract" stage.

Reuses the *shape* `config/table_descriptions.yaml`/
`config/sensitive_columns.yaml` already define (this codebase's own,
pre-existing semantic-contract format) rather than inventing a third
representation -- `config.table_descriptions.TableDescription`/
`config.sensitive_columns.ColumnClassification` are the schemas this
module's output is meant to be pasted into, once a human operator
reviews it.

**Only `"confirmed"` decisions are ever included.** A `"pending"` or
`"rejected"` item contributes nothing here -- this module has no
`agent.provenance.DataTruthLevel.AI_INFERENCE` output path at all, by
construction: everything it emits already cleared SME review before
reaching this function, which is what makes the *emitted* content
`CONFIRMED_BUSINESS_TRUTH`-eligible (an explicit human action, per that
vocabulary's own contract) rather than another inference this module
would need to re-disclose as unconfirmed.

**Does not write to `config/table_descriptions.yaml`/
`config/sensitive_columns.yaml` directly** -- the output is an
`OnboardingArtifact` row an operator reviews and applies manually
(mirrors `06_DATABASE_DISCOVERY_CONTRACT.md`'s own disclosed "no runtime
API to register a new database" boundary: this engine produces what's
needed to onboard a database, applying it to live config remains an
explicit, separate operator action).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfirmedReviewItem:
    """The minimal shape this module needs from a decided
    `identity.models.OnboardingReviewItem` -- deliberately not that ORM
    model itself, so this module (like every other file in `onboarding/`)
    has zero dependency on `identity/`/SQLAlchemy and is testable with
    plain objects."""

    item_type: str
    table_name: str | None
    column_name: str | None
    payload: dict[str, Any]


def build_semantic_contract(confirmed_items: list[ConfirmedReviewItem]) -> dict[str, Any]:
    """Builds `{"table_descriptions": {...}, "sensitive_columns": {...}}`,
    each inner dict already shaped exactly like the corresponding YAML
    file's own top-level structure (`{"tables": [...]}`)  -- an operator
    can paste either section directly into the matching file.

    Non-`"confirmed"` items (still pending, or explicitly rejected by an
    SME) are silently skipped -- this function has no visibility into
    *why* an item wasn't confirmed, only that it wasn't, which is
    intentional: a rejected PII classification must never leave any
    trace in the emitted contract, not even a commented-out note.
    """
    table_relationships: dict[str, list[str]] = {}
    sensitive_columns: dict[str, list[dict[str, str]]] = {}
    tables_with_notes: set[str] = set()

    for item in confirmed_items:
        if item.item_type == "pii_classification" and item.table_name and item.column_name:
            sensitive_columns.setdefault(item.table_name, []).append(
                {"column": item.column_name, "tier": "restricted"}
            )
        elif item.item_type == "semantic_label" and item.table_name and item.column_name:
            tables_with_notes.add(item.table_name)
        elif item.item_type == "relationship":
            source_table = item.payload.get("source_table") or item.table_name
            target_table = item.payload.get("target_table")
            source_columns = item.payload.get("source_columns") or []
            target_columns = item.payload.get("target_columns") or []
            if source_table and target_table:
                table_relationships.setdefault(source_table, []).append(
                    f"references {target_table} via "
                    f"({', '.join(source_columns)}) -> ({', '.join(target_columns)}), "
                    "confirmed by SME review"
                )

    all_table_names = sorted(tables_with_notes | set(table_relationships))
    table_descriptions = {
        "tables": [
            {
                "table_name": table_name,
                "purpose": "",
                "key_relationships": "; ".join(table_relationships.get(table_name, [])),
                "column_notes": _column_notes_by_name(confirmed_items, table_name),
            }
            for table_name in all_table_names
        ]
    }

    sensitive_columns_output = {
        "tables": [
            {"table_name": table_name, "columns": columns}
            for table_name, columns in sorted(sensitive_columns.items())
        ]
    }

    return {
        "table_descriptions": table_descriptions,
        "sensitive_columns": sensitive_columns_output,
    }


def _column_notes_by_name(
    confirmed_items: list[ConfirmedReviewItem], table_name: str
) -> dict[str, str]:
    """`{column_name: note}` for every confirmed `semantic_label` item on
    `table_name` -- the real `column_notes` shape
    `config.table_descriptions.TableDescription` expects."""
    notes: dict[str, str] = {}
    for item in confirmed_items:
        if (
            item.item_type == "semantic_label"
            and item.table_name == table_name
            and item.column_name
        ):
            label = item.payload.get("label", "unknown")
            notes[item.column_name] = f"Inferred semantic role: {label} (confirmed by SME review)"
    return notes
