"""Result-level governance: protects SQL result values before anyone else sees them.

Runs on the rows execution returned, before they are stored in agent state.
That means the insight step, the recommendation engine, and the response
all receive the protected values, not the raw ones. The LLM never sees a
restricted value merely because the UI would later hide it.

Two protections, both no-ops for ordinary data:

1. Restricted columns. A column whose name is classified "restricted" in
   `config/sensitive_columns.yaml` has its values replaced with
   `RESTRICTED_MASK` for a caller without `VIEW_RESTRICTED_COLUMNS`. Matching
   is by result column name, case-insensitively, because a result set names
   columns by alias. This is a second layer behind the SQL-level gate in
   `agent.sql_validator`, which runs first and catches direct references.
2. Secret-shaped text. Any string cell containing a connection string or
   password pattern is redacted (`security.redaction.redact_secrets`). This
   covers data that happens to contain a credential.

Row values are data, not instructions. Nothing in a cell can change how this
module decides, and the module never logs cell values, only counts.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from agent.authz import Permission, has_role_permission
from config.sensitive_columns import load_sensitive_columns
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)

RESTRICTED_MASK = "[restricted]"


@dataclass(frozen=True)
class GovernedResult:
    """A protected copy of a result set. `actions` describes what changed, with no values."""

    columns: list[str]
    rows: list[Any]
    actions: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.actions)


def govern_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    restricted_column_names: Collection[str],
    can_view_restricted: bool,
) -> GovernedResult:
    """Applies result-level protection. Returns new rows and leaves the input untouched.

    Args:
        columns: The result's column names, as returned by execution.
        rows: The result's rows. Tuples stay tuples and lists stay lists.
        restricted_column_names: Column names classified "restricted", in any case.
        can_view_restricted: True if the caller holds `VIEW_RESTRICTED_COLUMNS`.
    """
    restricted_lower = {name.lower() for name in restricted_column_names}
    masked_indexes: dict[int, str] = {}
    if not can_view_restricted:
        for index, name in enumerate(columns):
            if name.lower() in restricted_lower:
                masked_indexes[index] = name

    redacted_cells = 0
    governed_rows: list[Any] = []
    for row in rows:
        values: list[Any] = []
        for index, value in enumerate(row):
            if index in masked_indexes:
                values.append(RESTRICTED_MASK)
                continue
            if isinstance(value, str):
                redacted = redact_secrets(value)
                if redacted != value:
                    redacted_cells += 1
                    value = redacted
            values.append(value)
        governed_rows.append(tuple(values) if isinstance(row, tuple) else values)

    actions: list[str] = []
    if masked_indexes:
        actions.append("masked_restricted_columns:" + ",".join(sorted(masked_indexes.values())))
    if redacted_cells:
        actions.append(f"redacted_secret_cells:{redacted_cells}")

    return GovernedResult(columns=list(columns), rows=governed_rows, actions=tuple(actions))


def govern_rows_for_caller(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    caller_roles: Sequence[str],
) -> list[Any]:
    """The live entry point: governs executed rows for one caller.

    Reads the restricted-column classification fresh (`config/sensitive_columns
    .yaml`, same as the SQL-level gate) and derives the caller's permission from
    their roles. Logs what changed, never the values. Returns the rows unchanged
    when there are none.
    """
    if not rows:
        return list(rows)
    restricted = {
        column
        for (_table, column), tier in load_sensitive_columns().items()
        if tier == "restricted"
    }
    can_view = has_role_permission(caller_roles, Permission.VIEW_RESTRICTED_COLUMNS)
    governed = govern_result(columns, rows, restricted, can_view)
    if governed.changed:
        logger.info("[result_governance] applied: %s", ", ".join(governed.actions))
    return governed.rows
