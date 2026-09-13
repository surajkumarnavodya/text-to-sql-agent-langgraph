"""Loads `config/moderation_blocklist.yaml` -- the text-term blocklist
`moderation/gate.py` checks OCR/caption/transcript/extracted text against
for the two restricted categories Azure Content Safety has no dedicated
harm category for (see `moderation/taxonomy.py`'s module docstring).

Mirrors `config/sensitive_columns.py`'s pattern exactly: hand-authored,
read fresh on every call (no caching, so a hand-edit takes effect on the
very next ingestion, no rebuild step). Unlike `sensitive_columns.yaml`
(which ships empty until a human classifies a specific database's columns),
this file ships with a small starter list, since a text blocklist doesn't
need per-deployment schema knowledge to be non-trivially useful -- but it's
explicitly not exhaustive; see the YAML file's own header comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from moderation.taxonomy import Category

_DEFAULT_PATH = Path(__file__).resolve().parent / "moderation_blocklist.yaml"

_VALID_KEYS: frozenset[str] = frozenset({"weapons", "drugs"})


def load_blocklist(path: Path | None = None) -> dict[Category, list[str]]:
    """Returns category -> list of blocklisted terms.

    Args:
        path: Override for the YAML file location (mainly for tests).
            Defaults to `config/moderation_blocklist.yaml`.

    Returns:
        An empty dict if the file is missing or has no entries -- the
        blocklist is a best-effort enrichment on top of the provider's own
        categories, not a hard dependency; callers must work (with nothing
        blocklisted) when this returns {}. A key other than "weapons"/
        "drugs" is ignored rather than raising, consistent with
        `config.sensitive_columns.load_sensitive_columns`'s own
        best-effort loading.
    """
    resolved_path = path or _DEFAULT_PATH
    if not resolved_path.exists():
        return {}

    raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    blocklist: dict[Category, list[str]] = {}
    for key, terms in raw.items():
        if key not in _VALID_KEYS or not isinstance(terms, list):
            continue
        blocklist[key] = [str(term).strip().lower() for term in terms if str(term).strip()]
    return blocklist


def find_matches(text: str, blocklist: dict[Category, list[str]]) -> list[Category]:
    """Returns every category with at least one whole-word, case-insensitive
    match in `text` -- e.g. OCR output, a video transcript, extracted PDF
    text. Empty list if `text` is blank or nothing matches.
    """
    if not text:
        return []
    lowered = text.lower()
    matched: list[Category] = []
    for category, terms in blocklist.items():
        for term in terms:
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                matched.append(category)
                break
    return matched
