"""Curated display-label rules for `ui/column_formatting.py`.

Hand-maintained, not env-sourced: unlike `config/settings.py`'s `Settings`
dataclass (runtime tunables from `.env`), this is a static lookup table.
Extend `ABBREVIATION_EXPANSIONS` as you spot more AdventureWorksDW2025-
specific abbreviations that don't already split into human-readable words
on their own (e.g. "Prod" -> "Product", "Org" -> "Organization").
"""

from __future__ import annotations

# Token (case-insensitive) -> human-readable expansion, applied per word
# after PascalCase/camelCase/snake_case splitting.
ABBREVIATION_EXPANSIONS: dict[str, str] = {
    "Cust": "Customer",
    "Qty": "Quantity",
    "Amt": "Amount",
    "Desc": "Description",
    "Num": "Number",
}

# A column whose last word-token (see ui/column_formatting.py's tokenizer)
# exactly matches one of these, case-insensitively, is treated as a
# surrogate key and hidden from the default results view.
SURROGATE_KEY_SUFFIXES: tuple[str, ...] = ("key", "id")
