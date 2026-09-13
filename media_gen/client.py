"""HTTP client for the IMA Studio (imastudio.com) media generation API.

VERIFIED against IMA's own real source (not guessed): cloned
`github.com/imastuido/ima-all-ai` (a clawhub skill bundle) and read its
actual runtime implementation --
`scripts/ima_runtime/shared/{client,catalog,task_creation,task_execution,
rule_resolution}.py` and `references/operations/api-contract-and-errors.md`.
That is the ground truth this module is built from. Every endpoint path,
header, and payload/response field below comes from that source, not from
IMA's public marketing pages (which don't publish this).

Real API shape (confirmed, not assumed):
  1. `GET  /open/v1/product/list` -- discover available models/pricing for
     a task_type/category. Response: `{"code": 0|200, "data": [<tree>]}`,
     a NESTED tree; only `type == "3"` nodes are actual generation models
     (leaves). Each leaf carries `model_id`, `id` (version), `name`,
     `form_config` (default parameters), `credit_rules` (pricing/attribute
     profiles -- required at create time, not optional).
  2. `POST /open/v1/tasks/create` -- submit a generation task. EVERY task
     type (including plain text-to-image) is asynchronous -- there is no
     synchronous image endpoint. Response:
     `{"code": 0|200, "data": {"id": "<task_id>"}}`.
  3. `POST /open/v1/tasks/detail` -- poll `{"task_id": ...}` until a
     terminal state. Response: `{"code": ..., "data": {"medias": [...]}}`;
     each media's `resource_status` is 0=pending, 1=ready, 2=failed,
     3=deleted (a `status` field can additionally read "failed" even at
     resource_status=1 for some models -- check both).

Headers: `Authorization: Bearer <api_key>`, `Content-Type: application/json`,
`x-app-source: ima_skills`, `x_app_language: en`.

Business errors are carried in the response body's own `code` field (0 or
200 = success) -- NOT the HTTP status code alone. A `401` means an invalid
key; `4008` means insufficient credits; `500`/`6009`/`6010` are
retryable-with-degraded-params in the reference implementation (this
module does not implement that retry/degradation logic -- see "Known
simplifications" below).

Known simplifications vs. the reference `ima_runtime` package (deliberate,
not accidental -- that package is ~30 files handling a full agent-facing
CLI with user-choosable models/virtual-UI-field-mapping/preference-memory/
Pixverse-specific-params/create-time-retry-with-degradation; this app only
ever needs "generate one image from a prompt with no user-facing model
picker"):
  - Model selection: picks the FIRST available `type == "3"` leaf for the
    requested category, rather than supporting `--model-id`/`--version-id`
    selection. Good enough since this app doesn't expose a model picker.
  - Credit rule selection: uses the rule flagged `attributes.default ==
    "enabled"`, else the first rule -- matches
    `select_credit_rule_by_params(rules, {})`'s exact behavior for the
    no-extra-params case (this module never sends extra_params).
  - Virtual UI form fields (`is_ui_virtual=true`) are skipped entirely
    (only literal `field.value` defaults are used) -- fine since nothing
    here exposes user-choosable quality/resolution/etc. knobs.
  - No create-time retry-with-degradation on `500`/`6009`/`6010` -- a
    failure surfaces as a clean `MediaGenerationError` instead.
If any of these become limiting, `references/models/
product-list-and-create-params.md` (in the cloned reference) documents the
full resolution algorithm to extend this against.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

from config.settings import ConfigurationError, Settings
from security.redaction import redact_secrets


class MediaGenerationNotConfiguredError(ConfigurationError):
    """Raised when media generation is enabled but `IMA_API_KEY` isn't set.

    Mirrors `rag.store.RagStoreNotConfiguredError` /
    `search.web_search.WebSearchNotConfiguredError` -- a distinct subclass
    so a caller that wants to degrade gracefully (the orchestrator's
    availability check) can catch this specifically.
    """


_DEFAULT_SAFE_MESSAGE = (
    "Media generation failed due to a provider error. Please try again in a moment."
)

# IMA business codes (payload["code"]) this app gives a friendly,
# actionable message for -- see the module docstring's "Business errors"
# paragraph for where these values come from (IMA's own reference
# implementation, not guessed). Every other/unrecognized code falls back
# to `_DEFAULT_SAFE_MESSAGE`; the full raw code/message is never lost --
# it stays in `str(exc)` for logs (see `MediaResult.detail`).
_SAFE_MESSAGES_BY_CODE: dict[int, str] = {
    4008: (
        "The connected IMA Studio account has run out of credits "
        "(insufficient points). Top up the account balance at "
        "imastudio.com to continue generating images/videos."
    ),
    401: (
        "The configured IMA Studio API key was rejected. Check " "IMA_API_KEY in your .env file."
    ),
}


def _safe_message_for_payload(payload: Any) -> str:
    """Maps a business error code (if the payload carries one) to a
    friendly, actionable message -- see `_SAFE_MESSAGES_BY_CODE`."""
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int) and code in _SAFE_MESSAGES_BY_CODE:
            return _SAFE_MESSAGES_BY_CODE[code]
    return _DEFAULT_SAFE_MESSAGE


class MediaGenerationError(RuntimeError):
    """Raised for any non-success response from the IMA API (business
    `code != 0/200`, an HTTP transport error, or a terminal task failure),
    or a network failure reaching it. `status_code`/`payload` carry the raw
    provider error for logging -- `str(exc)` itself is already redacted
    (see `_request` below).

    Same two-message convention as `agent/exceptions.py::AgentError`/
    `media/exceptions.py::MediaSearchError`/`voice/exceptions.py::VoiceError`:
    `str(exc)` is the full internal detail (safe to log, includes the raw
    provider payload), `.safe_message` is a short, actionable sentence --
    e.g. "insufficient points" becomes a message telling the user to top up
    their account, rather than surfacing IMA's raw `{'code': 4008, ...}`
    dict repr directly in the UI, which is what happened before this
    existed (a real, reported bug)."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any = None,
        *,
        safe_message: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.safe_message = safe_message or _safe_message_for_payload(payload)


class IMAEndpoints:
    """Real, verified endpoint paths -- see module docstring for the source."""

    PRODUCT_LIST = "/open/v1/product/list"
    TASK_CREATE = "/open/v1/tasks/create"
    TASK_DETAIL = "/open/v1/tasks/detail"


Status = Literal["completed", "pending", "processing", "failed"]


@dataclass(frozen=True)
class MediaResult:
    """Normalized result shape every `generate_*` function returns.

    `error` is always the short, user-facing message (`MediaGenerationError
    .safe_message`) -- safe to put directly in an API response/UI. `detail`
    carries the full internal detail (`str(exc)`, including the raw
    provider payload) for logs only -- see `agent.orchestrator.nodes
    .execute_generation`, the only place that reads it."""

    status: Status
    url: str | None = None
    provider: str = "ima_studio"
    model: str | None = None
    job_id: str | None = None
    error: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.url is not None


class IMAClient:
    """Thin HTTP client for the three real IMA endpoints (product list,
    task create, task detail) -- see module docstring for the verified
    contract this implements.

    Construct via `get_ima_client(settings)` below rather than directly, so
    the "is this even configured" check happens in one place (mirroring
    `search/web_search.py`'s own `_tavily_search`-vs-public-entry-point
    split).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.imastudio.com",
        timeout: float = 30.0,
        session: requests.Session | None = None,
        language: str = "en",
    ):
        if not api_key:
            raise ValueError("api_key is required to construct IMAClient")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Reuse a single session for connection pooling -- avoids a new
        # TCP/TLS handshake per generation call, same reasoning as
        # `agent.llm_client._get_ollama_client`'s process-lifetime client.
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "x-app-source": "ima_skills",
                "x_app_language": language,
            }
        )

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        """Makes the HTTP call and enforces the business-error contract:
        the response body's own `code` (0 or 200 = success) is the source
        of truth, never the raw HTTP status alone (see module docstring)."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout)
        except requests.RequestException as exc:
            # Network/DNS errors can echo back request internals (rarely
            # the key itself, but redact defensively -- same "text this app
            # did not construct itself" rule applied to DB driver errors).
            raise MediaGenerationError(
                f"Network error calling IMA API: {redact_secrets(str(exc))}"
            ) from exc

        payload = _safe_json(resp)
        if resp.status_code >= 400 or payload is None:
            raise MediaGenerationError(
                f"IMA API HTTP error {resp.status_code} on {method} {path}: "
                f"{redact_secrets(str(payload if payload is not None else resp.text[:500]))}",
                status_code=resp.status_code,
                payload=payload,
            )

        code = payload.get("code")
        if code not in (0, 200):
            raise MediaGenerationError(
                f"IMA API business error on {method} {path}: code={code} message={payload.get('message')}",
                status_code=resp.status_code,
                payload=payload,
            )
        return payload

    def get_product_list(
        self, category: str, app: str = "ima", platform: str = "web"
    ) -> list[dict]:
        """`category` is the same string as `task_type` (e.g.
        "text_to_image") -- confirmed by the reference CLI's own
        `--task-type ... --list-models` usage and by `category` appearing
        verbatim as both the product-list query param and the create
        payload's `parameters[0].category` field."""
        payload = self._request(
            "GET",
            IMAEndpoints.PRODUCT_LIST,
            params={"app": app, "platform": platform, "category": category},
        )
        return payload.get("data") or []

    def create_task(self, payload: dict) -> str:
        data = self._request("POST", IMAEndpoints.TASK_CREATE, json=payload)
        task_id = (data.get("data") or {}).get("id")
        if not task_id:
            raise MediaGenerationError(f"IMA task create returned no task_id: {data}", payload=data)
        return task_id

    def get_task_detail(self, task_id: str) -> dict:
        return self._request("POST", IMAEndpoints.TASK_DETAIL, json={"task_id": task_id})

    def poll_task(
        self,
        task_id: str,
        interval_seconds: float = 5.0,
        timeout_seconds: float = 600.0,
    ) -> dict:
        """Polls until a terminal state, returning the first completed
        media dict. `resource_status`: 0=pending, 1=dispatched, 2=failed,
        3=deleted. IMPORTANT (confirmed via a real live call, not the docs):
        `resource_status == 1` does NOT by itself mean the asset is ready --
        a real response was observed with `resource_status: 1`,
        `status: "processing"`, and `url`/`watermark_url` both null well
        before the image was actually done. The real terminal signal is
        `resource_status == 1` AND at least one of
        `url`/`watermark_url`/`preview_url` is populated -- exactly what
        `ima_runtime.shared.task_execution.poll_task`'s own
        `if result_url:` gate checks (a detail this module's first pass
        dropped and had to be fixed after hitting it live)."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            detail = self.get_task_detail(task_id)
            medias = (detail.get("data") or {}).get("medias") or []

            for media in medias:
                resource_status = media.get("resource_status")
                resource_status = 0 if resource_status in (None, "") else int(resource_status)
                if resource_status == 2:
                    raise MediaGenerationError(
                        f"IMA generation failed: {media.get('error_msg') or media.get('remark') or 'unknown error'}",
                        payload=media,
                    )
                if resource_status == 3:
                    raise MediaGenerationError("IMA generation task was deleted", payload=media)

            if medias and all(
                (0 if m.get("resource_status") in (None, "") else int(m.get("resource_status")))
                == 1
                for m in medias
            ):
                failed = [m for m in medias if (m.get("status") or "").strip().lower() == "failed"]
                if failed:
                    err = failed[0].get("error_msg") or failed[0].get("remark") or "unknown error"
                    raise MediaGenerationError(f"IMA generation failed: {err}", payload=failed[0])

                def _has_result_url(m: dict) -> bool:
                    return bool(m.get("url") or m.get("watermark_url") or m.get("preview_url"))

                if all(_has_result_url(m) for m in medias):
                    return medias[0]
                # resource_status==1 but no asset URL yet (e.g. status
                # still "processing") -- not terminal, keep polling. See
                # this method's docstring for why this check exists.

            time.sleep(interval_seconds)

        raise MediaGenerationError(
            f"IMA generation task {task_id} timed out after {timeout_seconds}s"
        )


def _find_first_model_leaf(product_tree: list[dict]) -> dict | None:
    """Walks the nested product-list tree for the first `type == "3"` leaf
    (an actual generation model, as opposed to a category/grouping node).
    No `model_id` filtering -- see module docstring's "Known
    simplifications": this app doesn't expose a model picker, so "first
    available for this category" is the whole policy."""
    for node in product_tree:
        if node.get("type") == "3":
            return node
        found = _find_first_model_leaf(node.get("children") or [])
        if found is not None:
            return found
    return None


def _extract_model_params(node: dict) -> dict:
    """Simplified port of `ima_runtime.shared.catalog.extract_model_params`
    -- see module docstring's "Known simplifications" for what's omitted
    (virtual UI field resolution, non-default rule matching)."""
    credit_rules = node.get("credit_rules") or []
    if not credit_rules:
        raise MediaGenerationError(
            f"IMA model '{node.get('model_id')}' version '{node.get('id')}' has no credit_rules "
            "-- cannot determine attribute_id/credit for task creation."
        )

    form_params: dict = {}
    for field in node.get("form_config") or []:
        field_name = field.get("field")
        if not field_name or field.get("is_ui_virtual"):
            continue
        value = field.get("value")
        if value is not None:
            form_params[field_name] = value

    default_rule = next(
        (
            rule
            for rule in credit_rules
            if (rule.get("attributes") or {}).get("default") == "enabled"
        ),
        None,
    )
    selected_rule = default_rule or credit_rules[0]
    attribute_id = selected_rule.get("attribute_id", 0)
    if not attribute_id:
        raise MediaGenerationError(
            f"IMA model '{node.get('model_id')}' resolved attribute_id=0 -- would cause "
            "'Invalid product attribute' on create."
        )

    return {
        "attribute_id": attribute_id,
        "credit": selected_rule.get("points", 0),
        "model_id": node.get("model_id", ""),
        "model_name": node.get("name", ""),
        "model_version": node.get("id", ""),
        "form_params": form_params,
    }


def build_create_payload(task_type: str, model_params: dict, prompt: str) -> dict:
    """Builds the exact `POST /open/v1/tasks/create` payload shape,
    verified against `ima_runtime.shared.task_creation.build_create_payload`."""
    inner = dict(model_params["form_params"])
    inner["prompt"] = prompt
    inner["n"] = 1
    inner["input_images"] = []
    inner["cast"] = {"points": model_params["credit"], "attribute_id": model_params["attribute_id"]}

    return {
        "task_type": task_type,
        "enable_multi_model": False,
        "src_img_url": [],
        "parameters": [
            {
                "attribute_id": model_params["attribute_id"],
                "model_id": model_params["model_id"],
                "model_name": model_params["model_name"],
                "model_version": model_params["model_version"],
                "app": "ima",
                "platform": "web",
                "category": task_type,
                "credit": model_params["credit"],
                "parameters": inner,
            }
        ],
    }


def create_and_poll(
    client: IMAClient,
    task_type: str,
    prompt: str,
    poll_interval_seconds: float = 5.0,
    poll_timeout_seconds: float = 600.0,
    form_overrides: dict | None = None,
) -> tuple[dict, str]:
    """High-level helper: discover a model for `task_type`, create the
    task, poll to completion. Returns `(media, model_name)`.

    `form_overrides` replaces specific `form_config` field values on top of
    the model's own defaults (e.g. `{"duration": 10}` for a video model
    that exposes a "duration" field) -- see `generate_video`'s
    `duration_seconds` parameter for the concrete use case this exists
    for. Applied after `_extract_model_params` builds the default
    `form_params`, so an override always wins over the model's own
    default; an override for a field the selected model doesn't actually
    have is simply added as an extra parameter IMA will itself reject if
    it isn't recognized -- this function does not validate overrides
    against the model's real `form_config` options/min/max (that would
    need the full product-list response threaded through here; today's
    only caller validates its own input range instead, see
    `Settings.media_gen_video_duration_seconds`'s docstring).

    Raises `MediaGenerationError` if no model is available for `task_type`
    on this account, or on any create/poll failure -- never silently
    returns a partial result.
    """
    product_tree = client.get_product_list(category=task_type)
    leaf = _find_first_model_leaf(product_tree)
    if leaf is None:
        raise MediaGenerationError(
            f"No IMA model available for task_type={task_type!r} on this account."
        )

    model_params = _extract_model_params(leaf)
    if form_overrides:
        model_params["form_params"].update(form_overrides)
    payload = build_create_payload(task_type, model_params, prompt)
    task_id = client.create_task(payload)
    media = client.poll_task(
        task_id, interval_seconds=poll_interval_seconds, timeout_seconds=poll_timeout_seconds
    )
    return media, model_params["model_name"]


def get_ima_client(settings: Settings) -> IMAClient:
    """Public entry point -- checks configuration before constructing a
    client, mirroring `search.web_search`'s `WebSearchNotConfiguredError`
    check. Callers (the orchestrator node) should catch
    `MediaGenerationNotConfiguredError` the same way they already catch
    `RagStoreNotConfiguredError`/`WebSearchNotConfiguredError`."""
    if not settings.ima_api_key:
        raise MediaGenerationNotConfiguredError(
            "Media generation is enabled but IMA_API_KEY is not set."
        )
    return IMAClient(
        api_key=settings.ima_api_key.get_secret_value(), base_url=settings.ima_api_base_url
    )


def _safe_json(resp: requests.Response) -> dict | None:
    try:
        return resp.json()
    except ValueError:
        return None
