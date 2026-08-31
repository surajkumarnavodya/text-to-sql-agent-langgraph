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
