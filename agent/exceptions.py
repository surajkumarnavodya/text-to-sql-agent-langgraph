"""Custom exceptions for the agent layer.

Kept distinct from generic exceptions (ValueError, duckdb.Error, etc.) so
nodes.py can catch precisely the failure modes called out in the project
spec: Ollama not running, no relevant schema match, malformed LLM output,
query timeout.

Every `AgentError` carries two messages, not one:

  - `str(exc)` (the exception's own `args[0]`) -- the full internal detail,
    exactly as before this changed: a raw driver/Chroma/Ollama error, meant
    for logs and for the graph's own retry-feedback prompts (see
    `agent/nodes.py`), where the real text is genuinely useful. Every
    existing `raise SomeAgentError("...")` call site keeps working
    unchanged -- `safe_message` is a keyword-only addition, not a
    signature break.
  - `.safe_message` -- a short, non-technical sentence with no driver
    internals, hostnames, or stack-trace-shaped text, meant for anything
    that reaches an end user (an API response body, a UI toast). Each
    subclass below sets a sensible default so most call sites never need
    to think about it; a call site with genuinely user-relevant detail can
    still pass a more specific one explicitly.

This exists because the full detail and the safe summary were, until now,
the same string everywhere -- `agent/nodes.py` and `api/main.py` both used
to embed `str(exc)` directly into fields the client sees
(`AskResponse.error_history`/`failure_explanation`), which is exactly how
a raw DB/Chroma error (or, for anything not already caught inside
`agent/nodes.py`, a bare stack trace surfacing through FastAPI's default
handler) could reach a response. See `api/main.py`'s global
`AgentError`/`Exception` handlers for where `.safe_message` is actually
used, and `security.redaction.redact_secrets` for the separate,
complementary layer that strips secret-shaped substrings out of the full
detail before *that* is ever logged or fed back into a retry prompt.
"""

from __future__ import annotations

_DEFAULT_SAFE_MESSAGE = (
    "Something went wrong while processing your question. Please try again or rephrase it."
)


class AgentError(Exception):
    """Base class for all agent-layer errors.

    Args:
        detail: The full internal message (driver text, stack-trace-adjacent
            detail, whatever is actually useful for debugging or for the
            retry loop's own error-feedback prompt). This is what
            `str(exc)` returns, unchanged from before `safe_message` existed.
        safe_message: What's safe to show an end user. Defaults to a generic,
            non-technical sentence; subclasses override the default via
            `_default_safe_message`, and any call site can still pass its
            own by naming the keyword explicitly.
    """

    _default_safe_message: str = _DEFAULT_SAFE_MESSAGE

    def __init__(self, detail: str, *, safe_message: str | None = None) -> None:
        super().__init__(detail)
        self.safe_message = safe_message or self._default_safe_message


class OllamaUnavailableError(AgentError):
    """Raised when the local Ollama server can't be reached or errors out."""

    _default_safe_message = (
        "The AI model service is temporarily unavailable. Please try again in a moment."
    )


class MalformedLLMOutputError(AgentError):
    """Raised when the LLM response contains no extractable SQL."""

    _default_safe_message = (
        "I couldn't turn that into a valid query. Please try rephrasing your question."
    )


class SchemaRetrievalError(AgentError):
    """Raised when schema retrieval (Chroma) fails or the index is missing."""

    _default_safe_message = (
        "I couldn't access the database schema right now. Please try again shortly."
    )


class SqlExecutionTimeoutError(AgentError):
    """Raised when query execution exceeds the configured timeout."""

    _default_safe_message = (
        "That query took too long to run. Try narrowing your question (a "
        "smaller date range, an added filter, fewer joined tables)."
    )


class OffTopicQuestionError(AgentError):
    """Raised when the LLM itself judges the question unanswerable as SQL.

    The defense-in-depth backstop for anything that slipped past
    `agent.input_guard`'s cheaper pre-filter: the system prompt instructs
    the model to respond with a fixed sentinel (`agent.llm_client.
    OFF_TOPIC_SENTINEL`) rather than attempt SQL when a question isn't a
    database question at all. `generate_sql_from_llm` raises this instead
    of returning the sentinel as if it were candidate SQL, so it can never
    reach the validator/executor by accident.
    """

    _default_safe_message = "That doesn't look like a question I can answer from the database."
