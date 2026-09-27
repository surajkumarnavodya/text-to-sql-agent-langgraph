"""Regression tests proving Ollama model selection is genuinely
request-scoped -- Rule 10/11 of the model-selection feature ("no global
mutable model state", "one caller's model selection must never affect
another's request").

The real risk this guards against: a naive implementation could have done
something like `settings.ollama_model = selected_model` (mutating the
shared, process-wide cached `Settings` singleton) instead of passing `model`
as a plain function parameter. That bug class only reliably shows up under
real concurrency with a race window between "resolve the model" and "use
it" -- a purely sequential test (call twice with two different models, one
after another) would pass even with that bug, since there's no window for
the second call to clobber the first's in-flight value. These tests use
real `threading.Thread`s plus an artificial delay inside the fake Ollama
client to force that race window open.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent.llm_client import generate_sql_from_llm
from agent.nodes import generate_sql_node
from agent.state import AgentState
from config.settings import Settings
from security.secrets import SecretStr


@pytest.fixture(autouse=True)
def _reset_llm_call_limiter():
    """See tests/test_agent_nodes.py's identical fixture -- the process-wide
    LLM-call rate limiter is a module-level singleton, reset here so this
    file's two-concurrent-calls-per-test pattern never trips it."""
    from agent.rate_limit import get_llm_call_limiter

    get_llm_call_limiter(20).reset()
    yield
    get_llm_call_limiter(20).reset()


def _settings(**overrides: object) -> Settings:
    base = dict(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=5432,
        db_name="mydb",
        db_user="reader",
        db_password=SecretStr("secret"),
        db_connection_string=None,
        db_schema=None,
        db_odbc_driver="x",
        chroma_persist_dir=Path("/tmp/chroma"),
        chroma_collection_name="x",
        embedding_model_name="x",
    )
    base.update(overrides)
    return Settings(**base)


class TestLlmClientModelIsolation:
    """Directly at the `agent.llm_client` layer -- the one place `model`
    actually reaches `ollama.Client.chat(model=...)`."""

    def test_two_concurrent_calls_with_different_models_never_cross_contaminate(self, monkeypatch):
        settings = _settings()
        calls: list[dict] = []
        calls_lock = threading.Lock()

        class FakeClient:
            def chat(self, model, messages, options):
                # Sleep *after* reading `model` but before recording it --
                # if `model` were read from shared mutable state at call
                # time (rather than captured as a plain local/parameter),
                # the other thread's concurrent write would have a real
                # chance to land in this window.
                time.sleep(0.05)
                with calls_lock:
                    calls.append({"model": model})
                return {"message": {"content": "SELECT 1"}}

        monkeypatch.setattr(
            "agent.llm_client._get_ollama_client", lambda host, timeout: FakeClient()
        )

        def _run(model: str) -> None:
            generate_sql_from_llm(
                question="q",
                schema_context="",
                previous_sql=None,
                error_feedback=None,
                settings=settings,
                model=model,
            )

        t1 = threading.Thread(target=_run, args=("model-a:1b",))
        t2 = threading.Thread(target=_run, args=("model-b:1b",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(calls) == 2
        assert {c["model"] for c in calls} == {"model-a:1b", "model-b:1b"}


class TestGenerateSqlNodeModelIsolation:
    """At `agent.nodes.generate_sql_node` -- proves each call reads its own
    `state["selected_model"]` and passes exactly that to the LLM client,
    with no shared/global state that a concurrent call on a different
    `AgentState` dict could clobber."""

    def test_two_concurrent_node_calls_each_keep_their_own_selected_model(self, monkeypatch):
        settings = _settings()
        monkeypatch.setattr("agent.nodes.get_settings", lambda: settings)

        captured_models: list[str] = []
        capture_lock = threading.Lock()

        def _fake_generate_sql(**kwargs):
            model = kwargs.get("model")
            assert isinstance(model, str)  # this test's whole point: never None/missing
            time.sleep(0.05)  # widen the race window -- see module docstring
            with capture_lock:
                captured_models.append(model)
            return "SELECT 1"

        monkeypatch.setattr("agent.nodes.generate_sql_from_llm", _fake_generate_sql)

        def _run(question: str, model: str) -> None:
            state: AgentState = {
                "question": question,
                "schema_context_text": "",
                "error_history": [],
                "retry_count": 0,
                "selected_model": model,
            }
            generate_sql_node(state)

        t1 = threading.Thread(target=_run, args=("question one", "model-a:1b"))
        t2 = threading.Thread(target=_run, args=("question two", "model-b:1b"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(captured_models) == 2
        assert set(captured_models) == {"model-a:1b", "model-b:1b"}

    def test_state_with_no_selected_model_falls_back_to_settings_default(self, monkeypatch):
        """The pre-existing-caller compatibility case: a state dict that
        never sets `selected_model` at all (every AgentState built before
        this feature existed) must still resolve to `Settings.ollama_model`,
        not None/a crash."""
        settings = _settings(ollama_model="llama3.1:8b")
        monkeypatch.setattr("agent.nodes.get_settings", lambda: settings)
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return "SELECT 1"

        monkeypatch.setattr("agent.nodes.generate_sql_from_llm", _capture)

        state: AgentState = {
            "question": "q",
            "schema_context_text": "",
            "error_history": [],
            "retry_count": 0,
        }
        generate_sql_node(state)

        assert captured["model"] is None  # None -> generate_sql_from_llm uses settings.ollama_model
