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

**A second, narrower gap the per-test fixture alone cannot close, found
while enabling `LOCAL_AUTH_ENABLED=true` in this machine's own `.env` for
real end-to-end testing:** ~20 test files (`tests/test_api_*.py`,
`tests/test_connection.py`, others) build a **module-level**
`_BASE_SETTINGS = Settings(...)` constant, executed once at module import
time -- which, per pytest's own collection order, happens *before* any
test in that module has run, and therefore before the autouse fixture
below has ever fired even once. Any `Settings` field that module-level
call doesn't explicitly pass still reads the real, polluted `os.environ`
at that one import moment, and gets baked permanently into
`_BASE_SETTINGS.__dict__` -- every test in the file that rebuilds via
`Settings(**{**_BASE_SETTINGS.__dict__, **overrides})` (this codebase's
own documented pattern, see e.g. `tests/test_connection.py::_settings`'s
docstring) then inherits that one-time-polluted value on every call,
completely bypassing the per-test `monkeypatch.delenv` below, since the
value is being passed as an explicit constructor kwarg, not read from
`os.environ` again. Concretely: `LOCAL_AUTH_ENABLED=true` in this
developer's `.env` made `Settings.auth_mode` silently resolve to
`"local"` instead of the value ~77 tests across 11 files actually
expected (`"none"`/`"static_token"`/`"oidc"`), since `auth_mode` prioritizes
`local_auth_enabled` first (see that property's own docstring) --
discovered, not hypothetical.

Fixed the only way a *module-level* snapshot can be protected from a
*per-test* fixture: strip the same env vars here too, as plain top-level
code in this file, so it runs once at collection time -- pytest always
imports a directory's `conftest.py` before collecting sibling test
modules in that directory, so this runs before any file's own
module-level `Settings(...)` call does. The fixture below remains
necessary on top of this: it also undoes whatever a test's own
`monkeypatch.setenv` call did, between tests within a session, which a
one-time strip at collection can't.
"""

from __future__ import annotations

import os

import pytest

from config.settings import Settings

for _field_name in Settings.model_fields:
    os.environ.pop(_field_name.upper(), None)
del _field_name


@pytest.fixture(autouse=True)
def _isolate_settings_from_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


@pytest.fixture(autouse=True)
def _clear_process_singleton_caches() -> None:
    """Clears the `functools.cache`/`lru_cache`-backed singletons built once
    per process (compiled LangGraph graphs, the cached Ollama client, the
    cached Chroma client) before each test.

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

    `_get_ask_executor` (added by the enterprise scalability assessment's
    graceful-shutdown work, `api/main.py`) is the same category of hazard
    with a sharper failure mode: a test that exercises real shutdown
    (`_shutdown_ask_executor`) leaves the cached `ThreadPoolExecutor`
    permanently unusable (`.shutdown()` is irreversible) -- without clearing
    it here, every *later* test in the process that happens to request the
    same `max_workers` key would get handed that already-shut-down instance
    and fail with `RuntimeError: cannot schedule new futures after
    shutdown`, not a clear assertion mismatch.
    """
    from identity.db import _cached_identity_engine, _cached_session_factory
    from moderation.store import _cached_moderation_engine

    from agent.graph import build_graph
    from agent.llm_client import _get_ollama_client
    from agent.orchestrator.graph import build_orchestrator_graph
    from api.main import _get_ask_executor
    from embeddings.schema_indexer import _cached_chroma_client

    build_graph.cache_clear()
    build_orchestrator_graph.cache_clear()
    _get_ollama_client.cache_clear()
    _cached_chroma_client.cache_clear()
    _cached_moderation_engine.cache_clear()
    _cached_identity_engine.cache_clear()
    _cached_session_factory.cache_clear()
    _get_ask_executor.cache_clear()
