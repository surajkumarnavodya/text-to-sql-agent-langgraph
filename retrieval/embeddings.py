"""Embedding-provider abstraction: turns chunk/query text into vectors.

Deliberately not tightly coupled to one embedding backend -- `get_embedding_provider`
reads `Settings.retrieval_embedding_provider` and returns one of:

- `"local"` (default): reuses `embeddings.schema_indexer.get_embedding_function`,
  the same bundled ONNX MiniLM-L6-v2 runtime already embedding schema DDL and
  golden examples in this process -- no new model download, no API key, no
  network call. This is what keeps the new retrieval collection in the same
  embedding space convention as everything else Chroma-backed here.
- `"fake"`: a deterministic, hash-derived embedding with no model loading and
  no network access at all -- used by `tests/` so chunking/ingestion/retrieval
  tests run instantly and offline, and available in a real deployment too
  (e.g. a smoke-test environment with no model runtime installed).

Every provider is wrapped in `_with_retry` (bounded exponential backoff) by
`get_embedding_provider`, so retry policy lives in exactly one place rather
than being duplicated per backend.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from config.settings import Settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Raised when embedding text fails after all configured retries.

    Structured, not a bare re-raise of whatever the backend threw -- callers
    (`vector_store.py`'s upsert path, `retriever.py`'s query path) catch this
    specific type to decide "this is a vector-retrieval failure, fall back,"
    the same fail-open contract every other accuracy-aid module in this
    codebase already follows (`plan_query_node`, `retrieve_golden_examples_node`, ...).
    """


class EmbeddingProvider(ABC):
    """Minimal interface every embedding backend implements."""

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embeds a batch of texts in one call. Must preserve input order."""

    def embed_text(self, text: str) -> list[float]:
        """Embeds a single text -- a thin convenience wrapper over `embed_batch`."""
        return self.embed_batch([text])[0]


def _with_retry(
    func_name: str,
    call: Callable[[], list[list[float]]],
    retry_count: int,
    timeout_seconds: int,
) -> list[list[float]]:
    """Bounded exponential backoff around one embedding call.

    `timeout_seconds` is enforced as a soft, elapsed-time budget across all
    attempts combined (not a hard per-call cancellation -- neither the local
    ONNX runtime nor a typical hosted embedding SDK call exposes a clean
    cross-platform cancellation hook from here) -- once the budget is spent,
    the next retry is skipped rather than started, so a hung backend can't
    make this loop run indefinitely regardless of `retry_count`.
    """
    attempt = 0
    start = time.monotonic()
    last_exc: Exception | None = None
    while attempt <= retry_count:
        if attempt > 0:
            elapsed = time.monotonic() - start
            if elapsed >= timeout_seconds:
                break
            backoff = min(0.5 * (2 ** (attempt - 1)), timeout_seconds - elapsed)
            time.sleep(max(backoff, 0))
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - backend error types vary
            last_exc = exc
            logger.warning(
                "[embeddings] %s attempt %d/%d failed: %s",
                func_name,
                attempt + 1,
                retry_count + 1,
                exc,
            )
            attempt += 1
    raise EmbeddingError(f"{func_name} failed after {attempt} attempt(s): {last_exc}") from last_exc


class LocalEmbeddingProvider(EmbeddingProvider):
    """Wraps this project's existing default embedding function (Chroma's
    bundled ONNX MiniLM-L6-v2 runtime, or `sentence-transformers` for a
    non-default `EMBEDDING_MODEL_NAME` -- see
    `embeddings.schema_indexer.get_embedding_function`) behind the
    `EmbeddingProvider` interface, with retry/timeout applied uniformly.
    """

    # all-MiniLM-L6-v2's known output width -- used only to report
    # `dimensions` before the first real call has run; the *actual* value
    # used for validation always comes from a real embedded vector's length
    # (see `embed_batch`), never trusted blindly.
    _DEFAULT_DIMENSIONS = 384

    def __init__(self, settings: Settings) -> None:
        # Imported lazily (not at module import time) so importing
        # `retrieval.embeddings` itself never requires chromadb/onnxruntime
        # to be importable -- `FakeEmbeddingProvider` below has zero such
        # dependency, and tests that only need the fake provider shouldn't
        # be forced to pay for or mock this import.
        from embeddings.schema_indexer import get_embedding_function

        self._settings = settings
        self._embedding_function = get_embedding_function(settings)
        self._model_name = settings.embedding_model_name
        self._dimensions: int | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions or self._DEFAULT_DIMENSIONS

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        def _call() -> list[list[float]]:
            # The embedding function returns numpy arrays of np.float32 --
            # cast every element to a native Python float, since chromadb's
            # collection.upsert() rejects a list containing numpy scalar
            # types outright ("Expected embeddings to be a list of floats
            # or ints, a list of lists, ...", a real error hit while
            # testing this against a live collection, not a hypothetical).
            return [[float(x) for x in vector] for vector in self._embedding_function(texts)]

        vectors = _with_retry(
            "embed_batch",
            _call,
            self._settings.retrieval_embedding_retry_count,
            self._settings.retrieval_embedding_timeout_seconds,
        )
        if vectors and self._dimensions is None:
            self._dimensions = len(vectors[0])
        return vectors


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, network-free embedding for tests (and any environment
    with no real embedding runtime available).

    Not random: the same text always produces the same vector (seeded from
    a SHA-256 digest of the text), so retrieval-order assertions in tests
    are reproducible. Not semantically meaningful -- this does not cluster
    similar text near similar vectors the way a real model would, so it is
    only useful for exercising the *plumbing* (chunking -> hashing ->
    upsert -> query -> rerank), never for judging real retrieval quality.
    """

    def __init__(self, dimensions: int = 32, model_name: str = "fake-hash-embedding") -> None:
        self._dimensions = dimensions
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeats the digest bytes to fill `dimensions`, maps each byte to
        # [-1, 1], then L2-normalizes -- unit-length vectors are what every
        # real embedding model produces too, and what makes cosine-distance
        # scoring in `vector_store.py` behave sensibly in tests.
        raw = [(digest[i % len(digest)] / 127.5) - 1.0 for i in range(self._dimensions)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Factory: returns the configured `EmbeddingProvider`.

    Args:
        settings: Application settings (`retrieval_embedding_provider`,
            `retrieval_fake_embedding_dimensions`).

    Raises:
        ValueError: on an unrecognized provider name -- a configuration
            mistake, not a runtime condition to fail open on.
    """
    provider = settings.retrieval_embedding_provider
    if provider == "fake":
        return FakeEmbeddingProvider(dimensions=settings.retrieval_fake_embedding_dimensions)
    if provider == "local":
        return LocalEmbeddingProvider(settings)
    raise ValueError(f"Unrecognized retrieval_embedding_provider: {provider!r}")


def validate_dimensions(provider: EmbeddingProvider, expected: int | None) -> None:
    """Raises `ValueError` if `provider.dimensions` doesn't match `expected`.

    `expected` is `Settings.retrieval_embedding_dimensions` when explicitly
    configured -- if unset (the default), no check is performed, and the
    index simply uses whatever dimension the configured provider produces.
    Called by `vector_store.py` before any upsert, so a provider/index
    mismatch is caught as a clear configuration error rather than a cryptic
    Chroma dimension-mismatch exception surfacing mid-ingestion.
    """
    if expected is None:
        return
    if provider.dimensions != expected:
        raise ValueError(
            f"Embedding provider {provider.model_name!r} produces "
            f"{provider.dimensions}-dimensional vectors, but "
            f"RETRIEVAL_EMBEDDING_DIMENSIONS is set to {expected}. "
            "Either unset RETRIEVAL_EMBEDDING_DIMENSIONS or switch to a "
            "provider/model that matches it."
        )
