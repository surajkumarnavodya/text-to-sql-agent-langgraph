"""
ai_generator_client.py

Standalone, unified client for AI image/video generation APIs (Pixazo,
Cloudflare Workers AI, Leonardo.Ai, Ima Studio). NOT part of the Text-to-SQL
agent/API in this repo -- no other module imports this, it reads its own
`.env` keys, and it has no wiring into agent/, api/, or frontend/.

VERIFIED vs. UNVERIFIED endpoints -- read this before debugging "nothing
generates"
--------------------------------------------------------------------------
- Cloudflare Workers AI: the real, documented REST base is
  `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}`
  (https://developers.cloudflare.com/workers-ai/) -- this is confirmed
  against Cloudflare's own docs, not guessed.
- Pixazo and Ima Studio: I do not have verified API documentation for
  either of these two services. The endpoints below
  (`_BASE_URL_PIXAZO`, `_BASE_URL_IMA_STUDIO`) are a best-effort guess at a
  conventional REST shape (`POST {base}/images/generations` etc.) -- they
  are very likely wrong. Both providers' dashboards almost always show a
  ready-made cURL/code example once you generate an API key (look for a
  "Docs"/"API"/"Playground" tab) -- copy the exact base URL, path, and
  request body from there and update `_BASE_URL_PIXAZO` /
  `_BASE_URL_IMA_STUDIO` and the payload keys in
  `_generate_image_pixazo` / `_generate_image_ima_studio` to match.
- Leonardo.Ai: base URL and flow (submit job -> poll) are from Leonardo's
  public API reference (https://docs.leonardo.ai/reference) at time of
  writing -- reasonably confident, not independently re-verified today.

Running this file directly against Pixazo or Ima Studio right now will very
likely raise `ProviderRequestError` with a 404 (wrong path) or a DNS/
connection error (wrong host) -- that response body is the fastest way to
find the real endpoint, not a bug to silently swallow. This client
deliberately never hides that detail (see `_raise_for_status` and the
`__main__` block below).
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# Mirrors config/settings.py's own pattern: load this repo's .env once at
# import time so PIXAZO_API_KEY/CLOUDFLARE_*/IMA_STUDIO_API_KEY are picked
# up automatically -- no separate `load_dotenv()` call needed by callers.
load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ai_generator_client")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AIGeneratorError(Exception):
    """Base exception for everything this client raises."""


class ConfigurationError(AIGeneratorError):
    """Missing/invalid provider configuration (e.g. no API key set)."""


class UnsupportedProviderError(AIGeneratorError):
    """Requested provider, or provider+capability combo, isn't implemented."""


class AuthenticationError(AIGeneratorError):
    """HTTP 401/403 -- bad, missing, or expired API key/token."""


class CreditLimitExceededError(AIGeneratorError):
    """
    Raised on HTTP 429, or a provider-specific "out of credits" /
    "free-tier daily limit reached" response returned with a different
    status code.

    How to handle this in your calling code:
      1. Treat it as retryable-LATER, not retryable-now -- free tiers are
         typically time-windowed (often a daily reset).
      2. Check `retry_after` (seconds; populated only if the provider sent
         a `Retry-After` header). If None, check the provider's own
         dashboard for the plan's reset cadence.
      3. Surface a distinct "generation quota reached" state to the end
         user instead of a generic failure.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderRequestError(AIGeneratorError):
    """
    Any other non-2xx response, or a network-level failure. `status_code`
    and `response_body` carry the provider's raw error -- always print
    these when debugging a "not generating" symptom; the message alone is
    truncated for readability.
    """

    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class Provider(str, Enum):
    PIXAZO = "pixazo"
    CLOUDFLARE = "cloudflare"
    LEONARDO = "leonardo"
    IMA_STUDIO = "ima_studio"


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved, read-only config for one provider instance."""
    provider: Provider
    api_key: str
    account_id: Optional[str] = None   # Cloudflare only
    timeout: float = 60.0


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigurationError(
            f"Missing required environment variable '{name}'. Set it in this "
            f"repo's .env (already the case for PIXAZO_API_KEY/"
            f"CLOUDFLARE_AUTH_TOKEN+CLOUDFLARE_ACCOUNT_ID/IMA_STUDIO_API_KEY as "
            f"of this writing) -- never hardcode API keys in source."
        )
    return value


def load_config_from_env(provider: "str | Provider", timeout: float = 60.0) -> ProviderConfig:
    """
    Reads credentials for the requested provider from environment variables:
        PIXAZO_API_KEY
        CLOUDFLARE_AUTH_TOKEN  (+ CLOUDFLARE_ACCOUNT_ID)
        LEONARDO_API_KEY
        IMA_STUDIO_API_KEY
    """
    provider = Provider(provider)

    if provider is Provider.PIXAZO:
        return ProviderConfig(provider=provider, api_key=_require_env("PIXAZO_API_KEY"), timeout=timeout)

    if provider is Provider.CLOUDFLARE:
        return ProviderConfig(
            provider=provider,
            api_key=_require_env("CLOUDFLARE_AUTH_TOKEN"),
            account_id=_require_env("CLOUDFLARE_ACCOUNT_ID"),
            timeout=timeout,
        )

    if provider is Provider.LEONARDO:
        return ProviderConfig(provider=provider, api_key=_require_env("LEONARDO_API_KEY"), timeout=timeout)

    if provider is Provider.IMA_STUDIO:
        return ProviderConfig(provider=provider, api_key=_require_env("IMA_STUDIO_API_KEY"), timeout=timeout)

    raise UnsupportedProviderError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Endpoint constants -- see module docstring for verified vs. guessed
# ---------------------------------------------------------------------------

_BASE_URL_PIXAZO = "https://pixazo.ai/api/v1"                      # UNVERIFIED -- confirm with Pixazo docs
_BASE_URL_IMA_STUDIO = "https://api.imastudio.ai/v1"                # UNVERIFIED -- confirm with Ima Studio docs
_BASE_URL_LEONARDO = "https://cloud.leonardo.ai/api/rest/v1"
_CLOUDFLARE_BASE_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"

_PIXAZO_IMAGE_MODELS = {"flux-schnell", "sd3.5"}
_CLOUDFLARE_IMAGE_MODEL = "@cf/stabilityai/stable-diffusion-xl-base-1.0"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AIGeneratorClient:
    """
    Unified wrapper around Pixazo, Cloudflare Workers AI, Leonardo.Ai, and
    Ima Studio image/video generation. Switching providers is a config
    change -- `generate_image` / `generate_video` have the same signature
    regardless of which provider is active.

    Example:
        client = AIGeneratorClient(provider="cloudflare")
        result = client.generate_image("a red fox in snow", width=1024, height=1024)
    """

    def __init__(
        self,
        provider: "str | Provider",
        *,
        config: Optional[ProviderConfig] = None,
        session: Optional[requests.Session] = None,
    ):
        self.provider = Provider(provider)
        self.config = config or load_config_from_env(self.provider)
        if self.config.provider is not self.provider:
            raise ConfigurationError("Supplied ProviderConfig does not match requested provider")
        self._session = session or requests.Session()

    # ---- universal public API -------------------------------------------

    def generate_image(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        model: Optional[str] = None,
    ) -> dict:
        """
        Generate a single image. Returns a normalized dict:
            {"provider": str, "raw": <provider JSON>,
             "image_url": str | None, "image_b64": str | None, ...}
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive integers")

        logger.info("generate_image: provider=%s width=%s height=%s model=%s", self.provider.value, width, height, model)

        if self.provider is Provider.PIXAZO:
            return self._generate_image_pixazo(prompt, width, height, model)
        if self.provider is Provider.CLOUDFLARE:
            return self._generate_image_cloudflare(prompt, width, height, model)
        if self.provider is Provider.LEONARDO:
            return self._generate_image_leonardo(prompt, width, height, model)
        if self.provider is Provider.IMA_STUDIO:
            return self._generate_image_ima_studio(prompt, width, height, model)
        raise UnsupportedProviderError(f"{self.provider} does not support generate_image")

    def generate_video(
        self,
        prompt: str,
        image_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """
        Generate a video: text-to-video if image_url is None, else
        image-to-video. Video generation is commonly asynchronous -- the
        return dict carries a `job_id`/`status` when the provider hasn't
        finished synchronously.
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        logger.info("generate_video: provider=%s image_url=%s model=%s", self.provider.value, bool(image_url), model)

        if self.provider is Provider.PIXAZO:
            return self._generate_video_pixazo(prompt, image_url, model)
        if self.provider is Provider.LEONARDO:
            return self._generate_video_leonardo(prompt, image_url, model)
        raise UnsupportedProviderError(
            f"{self.provider} has no video generation endpoint wired up in this client. "
            f"Cloudflare Workers AI had no GA text/image-to-video model at the time this "
            f"wrapper was written, and Ima Studio's API surface hasn't been confirmed to "
            f"include video at all -- verify on their dashboard before assuming otherwise."
        )

    # ---- shared HTTP plumbing --------------------------------------------

    def _request(self, method: str, url: str, *, headers: dict, **kwargs) -> requests.Response:
        logger.debug("%s %s", method, url)
        try:
            response = self._session.request(method, url, headers=headers, timeout=self.config.timeout, **kwargs)
        except requests.exceptions.Timeout as exc:
            raise ProviderRequestError(
                f"Request to {self.provider.value} timed out after {self.config.timeout}s (url={url})"
            ) from exc
        except requests.exceptions.RequestException as exc:
            # Covers DNS failures / connection refused -- the most likely
            # symptom of an unverified base URL (Pixazo/Ima Studio) being
            # wrong, e.g. "Failed to resolve 'pixazo.ai'".
            raise ProviderRequestError(f"Network error calling {self.provider.value} at {url}: {exc}") from exc

        self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.ok:
            return

        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = response.text

        # Always logged (not just raised) so a caller who only catches
        # AIGeneratorError broadly still sees the raw provider response
        # that explains *why* nothing generated.
        logger.error("%s returned HTTP %s for %s: %s", self.provider.value, status, response.url, body)

        if status in (401, 403):
            raise AuthenticationError(
                f"{self.provider.value} rejected the request as unauthorized (HTTP {status}) "
                f"at {response.url}. Check that the configured API key/token is present, "
                f"correctly scoped, and not expired. Response: {body}"
            )

        if status == 429 or self._looks_like_credit_limit(body):
            raise CreditLimitExceededError(
                f"{self.provider.value} reported rate limiting or a credit/free-tier limit "
                f"(HTTP {status}). Response: {body}",
                retry_after=self._parse_retry_after(response),
            )

        if status == 404:
            raise ProviderRequestError(
                f"{self.provider.value} returned HTTP 404 at {response.url} -- this almost "
                f"always means the endpoint path is wrong, not that generation failed. "
                f"If this is Pixazo or Ima Studio, their exact API base URL/path is "
                f"UNVERIFIED in this client (see the module docstring) -- check the "
                f"provider's dashboard for the real path and update _BASE_URL_PIXAZO / "
                f"_BASE_URL_IMA_STUDIO accordingly. Response: {body}",
                status_code=status,
                response_body=body,
            )

        raise ProviderRequestError(
            f"{self.provider.value} returned HTTP {status} at {response.url}: {body}",
            status_code=status,
            response_body=body,
        )

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _looks_like_credit_limit(body: Any) -> bool:
        text = str(body).lower()
        markers = (
            "insufficient credit", "insufficient_credit", "credit limit",
            "daily limit", "quota exceeded", "quota_exceeded",
            "free tier limit", "out of credits",
        )
        return any(marker in text for marker in markers)

    # ---- Pixazo (UNVERIFIED endpoint -- see module docstring) --------------

    def _pixazo_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def _generate_image_pixazo(self, prompt: str, width: int, height: int, model: Optional[str]) -> dict:
        model = model or "flux-schnell"
        if model not in _PIXAZO_IMAGE_MODELS:
            raise UnsupportedProviderError(f"Pixazo image model '{model}' not in {_PIXAZO_IMAGE_MODELS}")
        url = f"{_BASE_URL_PIXAZO}/images/generations"
        payload = {"model": model, "prompt": prompt, "width": width, "height": height}
        data = self._request("POST", url, headers=self._pixazo_headers(), json=payload).json()
        return {
            "provider": self.provider.value,
            "raw": data,
            "image_url": data.get("image_url") or data.get("url"),
            "image_b64": data.get("image_base64") or data.get("b64_json"),
        }

    def _generate_video_pixazo(self, prompt: str, image_url: Optional[str], model: Optional[str]) -> dict:
        model = model or "ltx-video"
        url = f"{_BASE_URL_PIXAZO}/videos/generations"
        payload: dict[str, Any] = {"model": model, "prompt": prompt}
        if image_url:
            payload["image_url"] = image_url
        data = self._request("POST", url, headers=self._pixazo_headers(), json=payload).json()
        return {
            "provider": self.provider.value,
            "raw": data,
            "job_id": data.get("job_id") or data.get("id"),
            "video_url": data.get("video_url") or data.get("url"),
            "status": data.get("status", "unknown"),
        }

    # ---- Cloudflare Workers AI (verified base URL) -------------------------

    def _cloudflare_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def _generate_image_cloudflare(self, prompt: str, width: int, height: int, model: Optional[str]) -> dict:
        model = model or _CLOUDFLARE_IMAGE_MODEL
        base = _CLOUDFLARE_BASE_TEMPLATE.format(account_id=self.config.account_id)
        url = f"{base}/{model}"
        payload = {"prompt": prompt, "width": width, "height": height}
        response = self._request("POST", url, headers=self._cloudflare_headers(), json=payload)

        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("image/"):
            # SDXL on Workers AI returns raw image bytes by default, not JSON.
            return {
                "provider": self.provider.value,
                "raw": {"content_type": content_type},
                "image_url": None,
                "image_b64": base64.b64encode(response.content).decode("ascii"),
            }

        data = response.json()
        result = data.get("result", data)
        return {
            "provider": self.provider.value,
            "raw": data,
            "image_url": None,
            "image_b64": result.get("image") if isinstance(result, dict) else None,
        }

    # ---- Leonardo.Ai --------------------------------------------------------

    def _leonardo_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    def _generate_image_leonardo(self, prompt: str, width: int, height: int, model: Optional[str]) -> dict:
        url = f"{_BASE_URL_LEONARDO}/generations"
        payload: dict[str, Any] = {"prompt": prompt, "width": width, "height": height, "num_images": 1}
        if model:
            payload["modelId"] = model
        data = self._request("POST", url, headers=self._leonardo_headers(), json=payload).json()
        job = data.get("sdGenerationJob", data)
        generation_id = job.get("generationId") if isinstance(job, dict) else None
        return {
            "provider": self.provider.value,
            "raw": data,
            "image_url": None,
            "image_b64": None,
            "generation_id": generation_id,
        }

    def poll_leonardo_generation(self, generation_id: str) -> dict:
        url = f"{_BASE_URL_LEONARDO}/generations/{generation_id}"
        data = self._request("GET", url, headers=self._leonardo_headers()).json()
        generation = data.get("generations_by_pk", data)
        images = generation.get("generated_images", []) if isinstance(generation, dict) else []
        return {
            "provider": self.provider.value,
            "raw": data,
            "status": generation.get("status") if isinstance(generation, dict) else None,
            "image_urls": [img.get("url") for img in images],
        }

    def _generate_video_leonardo(self, prompt: str, image_url: Optional[str], model: Optional[str]) -> dict:
        if image_url:
            url = f"{_BASE_URL_LEONARDO}/generations-motion-svd"
            payload: dict[str, Any] = {"imageId": image_url}
        else:
            url = f"{_BASE_URL_LEONARDO}/generations-text-to-video"
            payload = {"prompt": prompt}
        if model:
            payload["modelId"] = model

        data = self._request("POST", url, headers=self._leonardo_headers(), json=payload).json()
        job = data.get("motionSvdGenerationJob") or data.get("generationId") or data
        job_id = job.get("generationId") if isinstance(job, dict) else job
        return {"provider": self.provider.value, "raw": data, "job_id": job_id, "video_url": None, "status": "submitted"}

    # ---- Ima Studio (UNVERIFIED endpoint -- see module docstring) ----------

    def _ima_studio_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    def _generate_image_ima_studio(self, prompt: str, width: int, height: int, model: Optional[str]) -> dict:
        url = f"{_BASE_URL_IMA_STUDIO}/images/generations"
        payload: dict[str, Any] = {"prompt": prompt, "width": width, "height": height}
        if model:
            payload["model"] = model
        data = self._request("POST", url, headers=self._ima_studio_headers(), json=payload).json()
        return {
            "provider": self.provider.value,
            "raw": data,
            "image_url": data.get("image_url") or data.get("url"),
            "image_b64": data.get("image_base64") or data.get("b64_json"),
        }


if __name__ == "__main__":
    # Diagnostic smoke test -- run directly to see EXACTLY what a provider
    # returns, instead of guessing why "nothing generates":
    #   ./.venv/Scripts/python.exe ai_generator_client.py pixazo
    #   ./.venv/Scripts/python.exe ai_generator_client.py cloudflare
    #   ./.venv/Scripts/python.exe ai_generator_client.py ima_studio
    provider_arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AI_PROVIDER", "cloudflare")
    print(f"--- Testing provider: {provider_arg} ---")
    try:
        client = AIGeneratorClient(provider=provider_arg)
        result = client.generate_image("a watercolor fox in snow", width=768, height=768)
        print("SUCCESS")
        print({k: (v if k != "image_b64" or not v else f"<{len(v)} base64 chars>") for k, v in result.items()})
    except ConfigurationError as exc:
        print(f"CONFIG ERROR: {exc}")
    except AuthenticationError as exc:
        print(f"AUTH ERROR (bad/expired key): {exc}")
    except CreditLimitExceededError as exc:
        print(f"QUOTA ERROR (retry_after={exc.retry_after}): {exc}")
    except ProviderRequestError as exc:
        print(f"REQUEST ERROR (status={exc.status_code}): {exc}")
        print(f"Raw response body: {exc.response_body}")
    except AIGeneratorError as exc:
        print(f"GENERATION FAILED: {exc}")
