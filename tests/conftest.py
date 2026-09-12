"""Shared pytest fixtures.

`config/settings.py` calls `load_dotenv()` at import time, so this
machine's real `.env` is already sitting in `os.environ` by the time
pytest starts collecting. That was harmless before the Pydantic
migration -- the old `Settings` was a plain `@dataclass`, so a test
building `Settings(**partial_kwargs)` got the class's hardcoded
defaults for every field it didn't pass, never the real environment.

Now that `Settings` is a `pydantic_settings.BaseSettings`, an
unspecified field falls back to reading `os.environ` (that's the
entire point of `BaseSettings`), not a fixed default -- so the exact
same test code would silently pick up whatever this developer's own
`.env` happens to have set (e.g. `ENABLE_DOCUMENT_RAG=true`), making
the test suite's result depend on the machine it runs on. The fixture
below restores test isolation by stripping every env var one of
`Settings`'s own fields could read before each test runs; individual
tests that want to exercise real env-var parsing (e.g.
`DB_CONNECTIONS`) call `monkeypatch.setenv`/`setattr` themselves,
after this fixture has already cleared the slate.
"""

from __future__ import annotations

import pytest

from config.settings import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


@pytest.fixture(autouse=True)
def _clear_process_singleton_caches() -> None:
    """Clears the `functools.cache`/`lru_cache`-backed singletons built once
    per process (compiled LangGraph graphs, the cached Ollama client) before
    each test.

    These exist for real request-latency reasons (see `agent.graph
    .build_graph`, `agent.orchestrator.graph.build_orchestrator_graph`,
    `agent.llm_client._get_ollama_client`'s docstrings) but a cache keyed
    only on call arguments (or, for `build_graph`, on nothing at all) means
    the *first* test in a session to populate a given cache slot pins
    whatever it built there for every later test that happens to share the
    same key -- concretely, a test that does `monkeypatch.setattr("ollama
    .Client", FakeClient)` and expects `_get_ollama_client(host, timeout)`
    to construct a fresh fake has no effect if an earlier test already
    cached a real client for that same `(host, timeout)` pair. Without this
    fixture that failure mode is silent and order-dependent -- it only
    shows up as a flaky, hard-to-explain failure depending on which tests
    ran first, not as a clear assertion mismatch.
    """
    from agent.graph import build_graph
    from agent.llm_client import _get_ollama_client
    from agent.orchestrator.graph import build_orchestrator_graph

    build_graph.cache_clear()
    build_orchestrator_graph.cache_clear()
    _get_ollama_client.cache_clear()
