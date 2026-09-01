"""Custom exceptions for the agent layer.

Kept distinct from generic exceptions (ValueError, duckdb.Error, etc.) so
nodes.py can catch precisely the failure modes called out in the project
spec: Ollama not running, no relevant schema match, malformed LLM output,
query timeout.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for all agent-layer errors."""


class OllamaUnavailableError(AgentError):
    """Raised when the local Ollama server can't be reached or errors out."""


class MalformedLLMOutputError(AgentError):
    """Raised when the LLM response contains no extractable SQL."""


class SchemaRetrievalError(AgentError):
    """Raised when schema retrieval (Chroma) fails or the index is missing."""


class SqlExecutionTimeoutError(AgentError):
    """Raised when query execution exceeds the configured timeout."""


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
