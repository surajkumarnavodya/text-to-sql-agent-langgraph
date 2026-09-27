"""Loads `config/ollama_models.yaml` -- hand-authored Ollama model metadata.

Same convention as `config/table_descriptions.py` (see that module's own
docstring): a hand-reviewed, deliberately incomplete source of truth for
*display* information, not something code generates or that gates behavior.
A model id with no entry here still works fine -- `describe_model` returns a
generic fallback description rather than an error, since this file's whole
purpose is cosmetic enrichment of `Settings.ollama_allowed_models`/
`agent.model_registry`, never a second allowlist.

Deliberately uncached, like `load_table_descriptions` -- re-read fresh on
every `GET /models` call, so a hand-edit to this file (a better description,
a newly-added model) takes effect on the very next request with no restart.
The file is tiny and local, so this is not a meaningful cost.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_PATH = Path(__file__).resolve().parent / "ollama_models.yaml"

#: Capabilities a model must be tagged with to appear in a Text-to-SQL model
#: picker -- see agent/model_registry.py's module docstring for why not
#: every Ollama model is treated as SQL-capable just because it's listed.
SQL_CAPABILITY = "sql"


class OllamaModelInfo(BaseModel):
    """One model's hand-authored display metadata, as loaded from the YAML file."""

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str
    parameter_size: str = ""
    context_length: int | None = None
    resource_level: str = "unknown"
    capabilities: tuple[str, ...] = Field(default_factory=tuple)
    recommended: bool = False
    description: str = ""


def load_ollama_model_catalog(path: Path | None = None) -> dict[str, OllamaModelInfo]:
    """Returns model_id -> `OllamaModelInfo` for every entry in the YAML file.

    Args:
        path: Override for the YAML file location (mainly for tests).
            Defaults to `config/ollama_models.yaml`.

    Returns:
        An empty dict if the file is missing -- this metadata is a
        best-effort enrichment, not a hard dependency; callers (see
        `describe_model` below) must work fine with less/no metadata.
    """
    resolved_path = path or _DEFAULT_PATH
    if not resolved_path.exists():
        return {}

    raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    catalog: dict[str, OllamaModelInfo] = {}
    for entry in raw.get("models", []):
        model_id = entry.get("id")
        if not model_id:
            continue
        catalog[model_id] = OllamaModelInfo(
            id=model_id,
            display_name=(entry.get("display_name") or model_id).strip(),
            parameter_size=(entry.get("parameter_size") or "").strip(),
            context_length=entry.get("context_length"),
            resource_level=(entry.get("resource_level") or "unknown").strip(),
            capabilities=tuple(entry.get("capabilities") or []),
            recommended=bool(entry.get("recommended", False)),
            description=(entry.get("description") or "").strip(),
        )
    return catalog


def _fallback_display_name(model_id: str) -> str:
    """Turns a bare Ollama model id (e.g. "phi4:14b") into a readable label
    ("Phi4 14b") for a model that has no curated `ollama_models.yaml` entry --
    an operator must be able to list an arbitrary model in
    `OLLAMA_ALLOWED_MODELS` and have it show up sensibly, not be silently
    dropped just because this hand-authored file hasn't been updated yet."""
    name = model_id.split(":", 1)[0]
    return name.replace("-", " ").replace("_", " ").title() + (
        f" {model_id.split(':', 1)[1]}" if ":" in model_id else ""
    )


def describe_model(model_id: str, catalog: dict[str, OllamaModelInfo]) -> OllamaModelInfo:
    """Returns `catalog[model_id]` if present, else a generic fallback.

    The fallback assumes `[text, sql]` capabilities (the minimum any model
    listed in `OLLAMA_ALLOWED_MODELS` is being configured for -- this
    application only ever offers *this* registry for Text-to-SQL
    generation), never `recommended`, and an empty description rather than
    inventing one -- see this module's own docstring for why an unlisted
    model is a normal, supported case, not an error.
    """
    if model_id in catalog:
        return catalog[model_id]
    return OllamaModelInfo(
        id=model_id,
        display_name=_fallback_display_name(model_id),
        capabilities=("text", "sql"),
    )
