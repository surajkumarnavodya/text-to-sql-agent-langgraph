"""Tests for `agent.tools.definitions.build_default_registry` -- confirms
the registry is wired to the six real source functions this application
already calls from `agent/orchestrator/nodes.py`, not a duplicate/fake
implementation. Each test monkeypatches the *real* underlying function
(imported locally inside each handler, so patching the home module's
attribute is picked up at call time) and asserts the tool called it with
the right arguments and returned its result unmodified.

Deliberately does NOT touch `agent/orchestrator/nodes.py` -- that module's
own hardcoded node functions are untouched by this feature and are still
covered by `tests/test_orchestrator.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.authz import Permission
from agent.tools.definitions import build_default_registry
from agent.tools.types import ToolCategory


class TestRegistryShape:
    def test_registers_exactly_six_tools(self) -> None:
        registry = build_default_registry()
        names = {t.name for t in registry.list_tools()}
        assert names == {
            "sql_query",
            "document_search",
            "policy_search",
            "web_search",
            "media_search",
            "media_generation",
        }

    def test_categories_and_permissions(self) -> None:
        registry = build_default_registry()
        expected = {
            "sql_query": (ToolCategory.READ, Permission.EXECUTE_SQL),
            "document_search": (ToolCategory.READ, Permission.DOCUMENTS_READ),
            "policy_search": (ToolCategory.READ, Permission.POLICY_RAG_QUERY),
            "web_search": (ToolCategory.READ, Permission.WEB_SEARCH),
            "media_search": (ToolCategory.READ, Permission.MEDIA_SEARCH),
            "media_generation": (ToolCategory.WRITE, Permission.MEDIA_GENERATE),
        }
        for name, (category, permission) in expected.items():
            tool = registry.get(name)
            assert tool.category == category, name
            assert tool.permission == permission, name

    def test_only_media_generation_is_write(self) -> None:
        registry = build_default_registry()
        write_tools = {t.name for t in registry.list_tools(ToolCategory.WRITE)}
        assert write_tools == {"media_generation"}

    def test_permission_denied_without_a_granting_role(self) -> None:
        registry = build_default_registry()
        from agent.tools.types import ToolPermissionError

        with pytest.raises(ToolPermissionError):
            registry.execute("sql_query", {"question": "x"}, caller_roles=())


class TestSqlQueryToolWiring:
    def test_calls_run_agent_with_expected_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.graph as agent_graph_module

        captured: dict[str, Any] = {}

        def _fake_run_agent(question, conversation_history, enable_insight, caller_roles):  # type: ignore[no-untyped-def]
            captured.update(
                question=question,
                conversation_history=conversation_history,
                enable_insight=enable_insight,
                caller_roles=caller_roles,
            )
            return {"status": "succeeded", "sql": "SELECT 1"}

        monkeypatch.setattr(agent_graph_module, "run_agent", _fake_run_agent)

        registry = build_default_registry()
        result = registry.execute(
            "sql_query",
            {"question": "How many orders?", "enable_insight": False},
            caller_roles=("user",),
        )

        assert result.success is True
        assert result.output == {"status": "succeeded", "sql": "SELECT 1"}
        assert captured["question"] == "How many orders?"
        assert captured["enable_insight"] is False
        assert captured["caller_roles"] == ("user",)


class TestRagToolWiring:
    def test_document_search_calls_run_rag_with_documents_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rag.graph as rag_graph_module

        captured: dict[str, Any] = {}

        def _fake_run_rag(question, collection, settings, caller_roles):  # type: ignore[no-untyped-def]
            captured.update(question=question, collection=collection, caller_roles=caller_roles)
            return {"answer": "docs answer", "citations": [], "status": "succeeded"}

        monkeypatch.setattr(rag_graph_module, "run_rag", _fake_run_rag)

        registry = build_default_registry()
        result = registry.execute(
            "document_search", {"question": "leave policy?"}, caller_roles=("viewer",)
        )

        assert result.success is True
        assert captured["collection"] == "documents"
        assert captured["question"] == "leave policy?"

    def test_policy_search_calls_run_rag_with_policies_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rag.graph as rag_graph_module

        captured: dict[str, Any] = {}

        def _fake_run_rag(question, collection, settings, caller_roles):  # type: ignore[no-untyped-def]
            captured.update(collection=collection)
            return {"answer": "policy answer", "citations": [], "status": "succeeded"}

        monkeypatch.setattr(rag_graph_module, "run_rag", _fake_run_rag)

        registry = build_default_registry()
        result = registry.execute(
            "policy_search", {"question": "comp policy?"}, caller_roles=("analyst",)
        )

        assert result.success is True
        assert captured["collection"] == "policies"


class TestWebSearchToolWiring:
    def test_calls_web_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import search.web_search as web_search_module

        captured: dict[str, Any] = {}

        def _fake_web_search(query, settings):  # type: ignore[no-untyped-def]
            captured["query"] = query
            return [{"title": "t", "url": "u", "snippet": "s", "retrieved_at": "now"}]

        monkeypatch.setattr(web_search_module, "web_search", _fake_web_search)

        registry = build_default_registry()
        result = registry.execute(
            "web_search", {"question": "today's news"}, caller_roles=("user",)
        )

        assert result.success is True
        assert captured["query"] == "today's news"
        assert result.output[0]["title"] == "t"


class TestMediaSearchToolWiring:
    def test_calls_search_media(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import media.search as media_search_module

        captured: dict[str, Any] = {}

        def _fake_search_media(query, settings, media_type, top_k):  # type: ignore[no-untyped-def]
            captured.update(query=query, media_type=media_type, top_k=top_k)
            return ["hit1"]

        monkeypatch.setattr(media_search_module, "search_media", _fake_search_media)

        registry = build_default_registry()
        result = registry.execute(
            "media_search",
            {"question": "photo of the site", "media_type": "image", "top_k": 3},
            caller_roles=("viewer",),
        )

        assert result.success is True
        assert captured["query"] == "photo of the site"
        assert captured["media_type"] == "image"
        assert captured["top_k"] == 3
        assert result.output == ["hit1"]


class TestMediaGenerationToolWiring:
    def test_calls_execute_generation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.orchestrator.nodes as orchestrator_nodes_module

        captured: dict[str, Any] = {}

        def _fake_execute_generation(question, kind, settings):  # type: ignore[no-untyped-def]
            captured.update(question=question, kind=kind)
            return {"answer": "generated", "status": "succeeded", "media_id": "abc"}

        monkeypatch.setattr(
            orchestrator_nodes_module, "execute_generation", _fake_execute_generation
        )

        registry = build_default_registry()
        result = registry.execute(
            "media_generation",
            {"question": "a cat", "kind": "image"},
            caller_roles=("analyst",),
        )

        assert result.success is True
        assert captured["question"] == "a cat"
        assert captured["kind"] == "image"
        assert result.output["media_id"] == "abc"

    def test_write_tool_denied_for_role_without_media_generate(self) -> None:
        registry = build_default_registry()
        from agent.tools.types import ToolPermissionError

        # "user" role grants EXECUTE_SQL/WEB_SEARCH but not MEDIA_GENERATE
        # (see agent/authz.py's _USER frozenset) -- media_generation is
        # WRITE and analyst-or-above only.
        with pytest.raises(ToolPermissionError):
            registry.execute(
                "media_generation", {"question": "x", "kind": "image"}, caller_roles=("user",)
            )
