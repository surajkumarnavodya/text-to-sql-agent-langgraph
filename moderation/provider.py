"""Pluggable content-moderation provider -- shaped like
`search/web_search.py`'s `SUPPORTED_SEARCH_PROVIDERS` pattern
(`Settings.moderation_provider` selects the implementation, swapping
providers is a `.env` change once a second one is added, not a code
change).

Only Azure AI Content Safety is implemented today, called directly via its
REST API (`httpx`) rather than the `azure-ai-contentsafety` SDK -- matching
how `search/web_search.py` (Tavily) and `media_gen/client.py` (IMA) both
call their provider's REST API directly rather than pulling in a vendor
SDK for this codebase's other external integrations.

**Real categories, stated honestly:** Azure Content Safety's Analyze Text/
Image APIs return exactly four harm categories -- `Hate`, `SelfHarm`,
`Sexual`, `Violence` -- each scored 0/2/4/6 on its standard four-level
severity scale. There is no "weapons," "drugs," or "synthetic/deepfake"
category here; see `moderation/taxonomy.py`'s module docstring for how
those are covered instead (a text blocklist, and a documented placeholder,
respectively). This module only ever returns results for the four real
Azure categories -- `moderation/gate.py` is responsible for adding the
blocklist-derived and placeholder results on top.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable

import httpx

from config.settings import ConfigurationError, Settings, get_settings
from moderation.exceptions import ModerationNotConfiguredError
from moderation.taxonomy import AZURE_CATEGORY_MAP
from moderation.types import CategoryResult, ModerationChunk

logger = logging.getLogger(__name__)

_API_VERSION = "2023-10-01"
_REQUEST_TIMEOUT_SECONDS = 15
_AZURE_HARM_CATEGORIES = tuple(AZURE_CATEGORY_MAP.keys())  # ("Hate", "SelfHarm", "Sexual", "Violence")

# Azure's own documented max input sizes for a single Analyze call -- a
# request over either limit is rejected by Azure itself with a clean 4xx,
# not silently truncated; `moderation/gate.py`'s image-tiling (large
# images) and PDF page-level chunking already keep chunks well under these
# in practice, so this is a defensive cap, not the primary sizing control.
_MAX_TEXT_LENGTH = 10_000
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _require_configured(settings: Settings) -> tuple[str, str]:
    if not settings.azure_content_safety_endpoint or not settings.azure_content_safety_key:
        raise ModerationNotConfiguredError(
            "AZURE_CONTENT_SAFETY_ENDPOINT and/or AZURE_CONTENT_SAFETY_KEY are not "
            "set in .env, but content is being ingested (media search or "
            "document/policy RAG). Moderation is mandatory whenever either "
            "pipeline is enabled -- set both, or disable ENABLE_MEDIA_SEARCH/"
            "ENABLE_DOCUMENT_RAG/ENABLE_POLICY_RAG.",
            safe_message="Content moderation is not configured; ingestion cannot proceed.",
        )
    endpoint = settings.azure_content_safety_endpoint.rstrip("/")
    return endpoint, settings.azure_content_safety_key.get_secret_value()


def _azure_headers(key: str) -> dict[str, str]:
    return {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}


def _parse_category_results(payload: dict) -> list[CategoryResult]:
    """Maps Azure's `categoriesAnalysis` response array to our taxonomy --
    shared by the text and image analyze calls below, since both return the
    identical shape (`[{"category": "Hate", "severity": 0}, ...]`)."""
    results: list[CategoryResult] = []
    for entry in payload.get("categoriesAnalysis", []):
        azure_category = entry.get("category")
        our_category = AZURE_CATEGORY_MAP.get(azure_category)
        if our_category is None:
            continue  # an Azure category we don't map (shouldn't happen; fail open on this one entry)
        severity = int(entry.get("severity", 0))
        results.append(
            CategoryResult(
                category=our_category,
                triggered=False,  # threshold decision happens in moderation/gate.py, not here
                severity=severity,
                source="provider",
            )
        )
    return results


def _azure_content_safety(chunk: ModerationChunk, settings: Settings) -> list[CategoryResult]:
    """Calls Azure Content Safety's Analyze Text or Analyze Image endpoint,
    matching `chunk.content_type`. Returns one `CategoryResult` per Azure
    harm category (Hate/SelfHarm/Sexual/Violence), `triggered=False` on all
    of them -- `moderation/gate.py` applies `Settings.moderation_severity_threshold`
    to decide which ones actually trigger, so this function is a pure
    "what did the provider see" call, no policy decision.

    Raises:
        ModerationNotConfiguredError: endpoint/key not set.
        httpx.HTTPError: on a network/API failure -- callers (`moderation/gate.py`)
            do not catch this; a moderation-provider failure must not be
            silently treated as "passed" for a safety gate (unlike this
            app's other, accuracy-only fail-open aids).
    """
    endpoint, key = _require_configured(settings)

    if chunk.content_type == "text":
        text = (chunk.text or "")[:_MAX_TEXT_LENGTH]
        if not text.strip():
            return [
                CategoryResult(category=category, triggered=False, severity=0, source="provider")
                for category in AZURE_CATEGORY_MAP.values()
            ]
        response = httpx.post(
            f"{endpoint}/contentsafety/text:analyze?api-version={_API_VERSION}",
            headers=_azure_headers(key),
            json={"text": text, "categories": list(_AZURE_HARM_CATEGORIES), "outputType": "FourSeverityLevels"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    else:
        if chunk.image_path is None:
            raise ValueError("ModerationChunk with content_type='image' must set image_path")
        image_bytes = chunk.image_path.read_bytes()[:_MAX_IMAGE_BYTES]
        response = httpx.post(
            f"{endpoint}/contentsafety/image:analyze?api-version={_API_VERSION}",
            headers=_azure_headers(key),
            json={
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "categories": list(_AZURE_HARM_CATEGORIES),
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    response.raise_for_status()
    return _parse_category_results(response.json())


# Provider name -> call function. Every entry must accept exactly
# (chunk: ModerationChunk, settings: Settings) and return list[CategoryResult]
# (severity/`triggered=False` only -- the threshold decision is
# `moderation/gate.py`'s job, kept out of every provider implementation so
# adding a second provider never needs to duplicate that policy logic).
SUPPORTED_MODERATION_PROVIDERS: dict[str, Callable[[ModerationChunk, Settings], list[CategoryResult]]] = {
    "azure_content_safety": _azure_content_safety,
}


def analyze_chunk(chunk: ModerationChunk, settings: Settings | None = None) -> list[CategoryResult]:
    """Runs the configured provider's analysis on one chunk.

    Raises:
        ConfigurationError: if `Settings.moderation_provider` isn't one of
            `SUPPORTED_MODERATION_PROVIDERS`.
        ModerationNotConfiguredError: if the provider's required config isn't set.
        httpx.HTTPError: on a network/API failure -- deliberately not
            swallowed here, see `_azure_content_safety`'s docstring.
    """
    settings = settings or get_settings()
    provider = settings.moderation_provider
    call = SUPPORTED_MODERATION_PROVIDERS.get(provider)
    if call is None:
        raise ConfigurationError(
            f"Unsupported MODERATION_PROVIDER={provider!r}. Supported values: "
            f"{', '.join(sorted(SUPPORTED_MODERATION_PROVIDERS))}."
        )
    return call(chunk, settings)
