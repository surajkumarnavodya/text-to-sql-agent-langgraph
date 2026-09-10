"""Shared Ollama call helper for the RAG subgraph's three LLM steps (grade,
rewrite, generate) -- factored out of `agent/llm_client.py` rather than
importing that module's SQL-specific functions, but built on the exact same
client construction and error handling (see `OllamaUnavailableError`'s use
here, matching `agent.llm_client`'s own).
"""

from __future__ import annotations

import logging

import httpx
import ollama

from agent.exceptions import OllamaUnavailableError
from config.settings import Settings

logger = logging.getLogger(__name__)


def call_ollama(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    max_tokens: int,
    temperature: float = 0.0,
) -> str:
    """Runs one Ollama chat call and returns the response text.

    Raises:
        OllamaUnavailableError: if the Ollama server can't be reached --
            same failure shape `agent.llm_client`'s own functions raise, so
            callers (the RAG subgraph nodes) handle it identically to the
            SQL pipeline's own LLM-call failures.
    """
    client = ollama.Client(
        host=settings.ollama_host, timeout=settings.ollama_request_timeout_seconds
    )
    try:
        response = client.chat(
            model=settings.ollama_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"num_predict": max_tokens, "temperature": temperature},
        )
    except (ollama.ResponseError, ConnectionError, TimeoutError, OSError, httpx.HTTPError) as exc:
        raise OllamaUnavailableError(
            f"Could not reach Ollama at {settings.ollama_host} with model "
            f"'{settings.ollama_model}': {exc}."
        ) from exc

    return (
        response.get("message", {}).get("content", "")
        if isinstance(response, dict)
        else getattr(getattr(response, "message", None), "content", "")
    ).strip()
