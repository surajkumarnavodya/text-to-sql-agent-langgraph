"""Loads `config/sensitive_columns.yaml` -- the enforced version of
`docs/GOVERNANCE.md`'s "Data classification policy."

Mirrors `config/table_descriptions.py`'s pattern exactly: a hand-authored,
deliberately-incomplete-until-reviewed YAML file, read fresh on every call
(no caching), because a hand-edit here should take effect on the very next
question -- the same reasoning `config/table_descriptions.py`'s own
docstring gives for its own file.

Every table/column not listed here defaults to "public" (unrestricted) -- an
unlisted column is never treated as restricted by omission, and a missing
file is never an error (an empty classification is a legitimate, if
unreviewed, starting state -- see `docs/GOVERNANCE.md`).

Two independent enforcement points read this, once populated:
  - `db/value_sampling.py` never samples a "restricted" column into the
    schema prompt, regardless of cardinality.
  - `agent/nodes.py::validate_sql_node` rejects (retryable, not a safety
    violation -- the model can self-correct by dropping the column)
    validated SQL that directly `SELECT`s a "restricted" column.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent / "sensitive_columns.yaml"

SensitivityTier = Literal["public", "internal", "restricted"]

_VALID_TIERS: frozenset[str] = frozenset({"public", "internal", "restricted"})


@dataclass(frozen=True)
class ColumnClassification:
    """One column's data-sensitivity tier, as loaded from the YAML file."""

    table_name: str
    column_name: str
    tier: SensitivityTier


def load_sensitive_columns(path: Path | None = None) -> dict[tuple[str, str], SensitivityTier]:
    """Returns (table_name, column_name) -> tier for every classified column.

    Keys are exactly as written in the YAML file (case-sensitive, matching
    live schema introspection's own naming) -- callers that need
    case-insensitive lookup should normalize both sides themselves, the
    same convention `agent.sql_validator.find_unexpected_table_references`
    already uses for its own known-tables comparison.

    Args:
        path: Override for the YAML file location (mainly for tests).
            Defaults to `config/sensitive_columns.yaml`.

    Returns:
        An empty dict if the file is missing or has no entries -- the
        classification is a best-effort, opt-in enrichment, not a hard
        dependency; callers must work (with nothing restricted) when this
        returns {}. Malformed individual entries (missing column name, or
        an unrecognized tier) are skipped rather than raising, consistent
        with `config.table_descriptions.load_table_descriptions`'s own
        best-effort loading -- a typo in one entry must not take down
        every other, already-reviewed classification.
    """
    resolved_path = path or _DEFAULT_PATH
    if not resolved_path.exists():
        return {}

    raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    classifications: dict[tuple[str, str], SensitivityTier] = {}
    for table_entry in raw.get("tables") or []:
        table_name = table_entry.get("table_name")
        if not table_name:
            continue
        for column_entry in table_entry.get("columns") or []:
            column_name = column_entry.get("column")
            tier = column_entry.get("tier")
            if not column_name or tier not in _VALID_TIERS:
                continue
            classifications[(table_name, column_name)] = tier
    return classifications


def is_restricted(
    table_name: str,
    column_name: str,
    classifications: dict[tuple[str, str], SensitivityTier],
) -> bool:
    """True if this exact (table, column) pair is classified "restricted".

    Args:
        table_name: Exact table name (case-sensitive, matching live schema
            introspection).
        column_name: Exact column name.
        classifications: Output of `load_sensitive_columns()`.
    """
    return classifications.get((table_name, column_name)) == "restricted"
