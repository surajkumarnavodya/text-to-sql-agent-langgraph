"""Configuration-driven Ollama model registry for Text-to-SQL model selection.

This module is the one place that combines three genuinely separate
concepts, kept deliberately distinct throughout this codebase (see
`GET /models` in `api/main.py` and the React model picker it feeds):

  1. **Configured/allowed** -- `Settings.ollama_allowed_models`, an
     operator's own `.env` choice (`OLLAMA_ALLOWED_MODELS`) of which models
     this application will ever offer for Text-to-SQL, always including
     `Settings.ollama_model` (the default). This is the only thing that
     actually gates what a caller may request via `AskRequest.model` --
     see `validate_model_selection` below.
  2. **Installed locally** -- whatever `ollama.Client().list()` reports the
     *connected* Ollama server actually has pulled, right now. Queried live
     only by `GET /models` (and, unrelated to this module, `GET /health`'s
     own pre-existing vision-model check) -- never on every `/ask`, per this
     feature's own performance requirement (a `/ask` that omits `model`
     entirely, the overwhelming common case, costs nothing extra; seeing a
     genuinely uninstalled model surfaces naturally via the real generation
     call's own existing `OllamaUnavailableError` handling instead of a
     second speculative round-trip -- see `agent.llm_client`'s per-call
     error messages, which now name the actual model that was tried).
  3. **This application's own curated display metadata** --
     `config/ollama_models.yaml` (display name, size, capabilities,
     description) -- purely cosmetic enrichment, never a gate. A model
     listed in `OLLAMA_ALLOWED_MODELS` with no catalog entry still works
     fine (see `config.ollama_models.describe_model`'s fallback).

**Deliberately NOT a hard-coded list of "every Ollama model that exists."**
The online Ollama Library (https://ollama.com/library) changes over time and
is never queried at runtime by this module -- it was only ever a *research*
input, used once (2026-09-27, see `config/settings.py`'s
`_DEFAULT_OLLAMA_ALLOWED_MODELS` and `config/ollama_models.yaml`) to pick a
small, practical starter set. An operator is free to list any model name in
`OLLAMA_ALLOWED_MODELS`, whether or not this file's catalog or that starter
set has ever heard of it.
"""

from __future__ import annotations

import logging

from agent.llm_client import get_ollama_client
from config.ollama_models import OllamaModelInfo, describe_model, load_ollama_model_catalog
from config.settings import Settings

logger = logging.getLogger(__name__)


class InvalidModelSelectionError(ValueError):
    """Raised by `validate_model_selection` when a caller-supplied model
    string isn't in `Settings.ollama_allowed_models`.

    A plain `ValueError` subclass, not an `agent.exceptions.AgentError` --
    this is a malformed-request condition (like a Pydantic validation
    failure), meant to become an HTTP 400 at the API boundary
    (`api/main.py`'s `/ask`), not a graceful `AgentState` "failed" run the
    way an `AgentError` (e.g. a genuinely-configured-but-uninstalled model
    failing at generation time) does. See that module's `/ask` handler.
    """

    def __init__(self, model: str, allowed_models: tuple[str, ...]) -> None:
        self.model = model
        self.allowed_models = allowed_models
        super().__init__(
            f"Model {model!r} is not one of the models this application allows for "
            f"Text-to-SQL generation. Allowed: {', '.join(allowed_models)}."
        )


def installed_model_names(list_response: object) -> set[str]:
    """Extracts pulled model names from an `ollama.Client().list()` result --
    tolerant of both the real client's `ListResponse` (a `.models` attribute
    of `Model` objects, each with its own `.model` attribute) and a plain
    `{"models": [...]}` dict (this project's own test doubles, and what the
    raw `/api/tags` HTTP response itself looks like), where each entry may
    be a dict with a `"model"` and/or `"name"` key. Never raises -- an
    unrecognized shape just yields an empty set, which callers already
    treat as "nothing confirmed installed" rather than crashing.

    The single shared implementation for both `GET /models` (this module's
    `discover_installed_models`) and `GET /health`'s pre-existing
    vision-model-availability check (`api/main.py`) -- moved here rather
    than kept private to `api/main.py` so both call sites share one
    implementation instead of two copies drifting apart.
    """
    models = getattr(list_response, "models", None)
    if models is None and isinstance(list_response, dict):
        models = list_response.get("models")
    names: set[str] = set()
    for model in models or []:
        name = getattr(model, "model", None)
        if name is None and isinstance(model, dict):
            name = model.get("model") or model.get("name")
        if name:
            names.add(name)
    return names


def discover_installed_models(settings: Settings) -> set[str]:
    """Live query against the *connected* Ollama instance for which models
    are actually pulled -- reuses the same process-wide cached
    `ollama.Client` every generation call already shares
    (`agent.llm_client.get_ollama_client`), so this costs one small
    `/api/tags`-equivalent round trip, not a second client/connection.

    Called only by `GET /models` (and `GET /health`, independently, for its
    own narrower vision-model check) -- never from the `/ask` request path,
    per this module's own docstring. Fails open to an empty set (never
    raises) so a momentarily-unreachable Ollama server degrades the model
    picker to "nothing shown as installed" rather than crashing the whole
    endpoint -- the caller (`api/main.py`'s `GET /models`) still returns
    every *configured* model, just with `installed=False` across the board.
    """
    try:
        return installed_model_names(get_ollama_client(settings).list())
    except Exception as exc:  # noqa: BLE001 - discovery must never crash the caller
        logger.warning("[model_registry] could not reach Ollama for model discovery: %s", exc)
        return set()


class ModelOption(OllamaModelInfo):
    """One selectable model, as `GET /models` reports it -- `OllamaModelInfo`
    (the hand-authored display metadata) plus the three request-time facts
    that metadata alone can never answer: is it the configured default, and
    is it actually installed right now (`installed`/`available`, currently
    identical -- kept as two fields since a future capability-based
    restriction, e.g. "installed but disabled for this workload," would
    only need to change `available`, not this whole shape)."""

    is_default: bool
    installed: bool
    available: bool


def build_model_options(settings: Settings, installed: set[str] | None = None) -> list[ModelOption]:
    """Builds the full `GET /models` listing: every configured/allowed
    model, enriched with display metadata and live installed status.

    Args:
        settings: Application settings.
        installed: Precomputed installed-model set, to avoid a second
            `discover_installed_models` round trip when a caller already
            has one in hand (none of this module's own call sites do
            today, but this keeps the function testable without a real/
            mocked Ollama client for every case). `None` (the default)
            triggers a live discovery call.
    """
    if installed is None:
        installed = discover_installed_models(settings)
    catalog = load_ollama_model_catalog()

    options: list[ModelOption] = []
    for model_id in settings.ollama_allowed_models:
        info = describe_model(model_id, catalog)
        is_installed = model_id in installed
        options.append(
            ModelOption(
                **info.model_dump(),
                is_default=(model_id == settings.ollama_model),
                installed=is_installed,
                available=is_installed,
            )
        )
    return options


def validate_model_selection(model: str | None, settings: Settings) -> str:
    """Resolves and validates a caller-supplied model against the allowlist.

    This is the one place `AskRequest.model` is ever trusted -- see
    `api/main.py`'s `/ask` handler, which calls this *before* the graph
    ever runs, so an invalid selection never spends any LLM/DB work.

    Args:
        model: The caller-supplied model id, or None (use the default).
        settings: Application settings.

    Returns:
        The model name to actually use: `model` itself if valid, or
        `settings.ollama_model` if `model` was None.

    Raises:
        InvalidModelSelectionError: if `model` is not None and not in
            `settings.ollama_allowed_models`. Deliberately does NOT check
            live installed status here -- see this module's own docstring
            for why that check is left to the real generation call instead
            of a second Ollama round trip on every request.
    """
    if model is None:
        return settings.ollama_model
    if model not in settings.ollama_allowed_models:
        raise InvalidModelSelectionError(model, settings.ollama_allowed_models)
    return model
