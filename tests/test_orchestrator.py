"""Unit tests for the multi-source orchestrator (agent/orchestrator/).

Two things matter most here, mirroring CLAUDE.md's constraint that the
existing SQL pipeline must be byte-for-byte unchanged for SQL-only
questions:

1. With `ENABLE_MULTI_SOURCE_ROUTER` off (the default), `run_orchestrated`
   must be a pure pass-through to `agent.graph.run_agent` -- not "the
   orchestrator graph with one destination," but the literal same call, with
   the orchestrator graph never even constructed.
2. With it on and only `sql` configured (still the common case), the router
   must short-circuit (no classification call), and the SQL subgraph node
   must call `agent.graph.run_agent` -- the existing, unmodified graph --
   rather than reimplementing any part of it.

The rest covers the real multi-source behavior: source availability,
LLM-based classification when 2+ sources are configured, fan-out to
multiple subgraphs in one graph step, and synthesis attribution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.orchestrator import graph as orchestrator_graph
from agent.orchestrator import nodes as orchestrator_nodes
from agent.orchestrator.nodes import (
    classify_sources,
    document_rag_node,
    get_available_sources,
    policy_rag_node,
    route_after_router,
    router_node,
    sql_subgraph_node,
    synthesis_node,
    web_search_node,
)
from agent.state import AgentState
from config.settings import Settings
from security.secrets import SecretStr


def _settings(**overrides: object) -> Settings:
    base = dict(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=None,
        db_name="mydb",
        db_user="reader",
        db_password=SecretStr("secret"),
        db_connection_string=None,
        db_schema=None,
        db_odbc_driver="ODBC Driver 17 for SQL Server",
        chroma_persist_dir=Path("/tmp/chroma"),
        chroma_collection_name="schema_ddl",
        embedding_model_name="all-MiniLM-L6-v2",
        schema_top_k=4,
        max_retries=3,
        complex_query_max_retry_bonus=2,
        max_result_rows=1000,
        query_timeout_seconds=15,
        llm_max_tokens=1024,
        insight_max_tokens=120,
        max_question_length=500,
        question_rate_limit_per_minute=10,
        llm_call_rate_limit_per_minute=20,
        cost_estimation_enabled=True,
        cost_estimation_timeout_seconds=3,
        cost_moderate_row_threshold=50_000,
        cost_high_row_threshold=1_000_000,
        log_level="INFO",
        log_redaction_level="standard",
        enable_multi_source_router=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestGetAvailableSources:
    def test_returns_only_sql_by_default(self):
        assert get_available_sources(_settings()) == ["sql"]

    def test_document_rag_needs_both_flag_and_store_connection(self):
        assert get_available_sources(_settings(enable_document_rag=True)) == ["sql"]
        assert get_available_sources(
            _settings(enable_document_rag=True, rag_store_connection_string=SecretStr("x"))
        ) == ["sql", "documents"]

    def test_policy_rag_needs_both_flag_and_store_connection(self):
        assert get_available_sources(
            _settings(enable_policy_rag=True, rag_store_connection_string=SecretStr("x"))
        ) == ["sql", "policy"]

    def test_web_search_needs_both_flag_and_api_key(self):
        assert get_available_sources(_settings(enable_web_search=True)) == ["sql"]
        assert get_available_sources(
            _settings(enable_web_search=True, web_search_api_key=SecretStr("tvly-x"))
        ) == ["sql", "web"]

    def test_all_four_together(self):
        settings = _settings(
            enable_document_rag=True,
            enable_policy_rag=True,
            enable_web_search=True,
            rag_store_connection_string=SecretStr("x"),
            web_search_api_key=SecretStr("tvly-x"),
        )
        assert get_available_sources(settings) == ["sql", "documents", "policy", "web"]


class TestClassifySources:
    def test_parses_a_single_source_response(self, monkeypatch):
        import rag.llm

        monkeypatch.setattr(rag.llm, "call_ollama", lambda *a, **k: "sql")
        sources, reasoning = classify_sources("how many orders?", ["sql", "policy"], _settings())
        assert sources == ["sql"]
        assert "sql" in reasoning

    def test_parses_multiple_comma_separated_sources(self, monkeypatch):
        import rag.llm

        monkeypatch.setattr(rag.llm, "call_ollama", lambda *a, **k: "sql, policy")
        sources, _ = classify_sources(
            "compare X with policy", ["sql", "policy", "web"], _settings()
        )
        assert sources == ["sql", "policy"]

    def test_drops_names_not_in_available(self, monkeypatch):
        import rag.llm

        monkeypatch.setattr(rag.llm, "call_ollama", lambda *a, **k: "sql, web")
        sources, _ = classify_sources("x", ["sql", "policy"], _settings())
        assert sources == ["sql"]

    def test_falls_back_to_every_available_source_on_unparseable_response(self, monkeypatch):
        import rag.llm

        monkeypatch.setattr(rag.llm, "call_ollama", lambda *a, **k: "I'm not sure")
        sources, reasoning = classify_sources("x", ["sql", "policy"], _settings())
        assert sources == ["sql", "policy"]
        assert "unparseable" in reasoning


class TestRouterNode:
    def test_short_circuits_when_only_sql_is_available(self, monkeypatch):
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())
        result = router_node({"question": "how many orders last month?"})
        decision = result["route_decision"]
        assert decision["sources"] == ["sql"]
        assert decision["short_circuited"] is True
        assert "sql" in decision["reasoning"]

    def test_classifies_when_multiple_sources_are_available(self, monkeypatch):
        settings = _settings(enable_policy_rag=True, rag_store_connection_string=SecretStr("x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "reason"),
        )
        result = router_node({"question": "compare sales with policy"})
        decision = result["route_decision"]
        assert decision["sources"] == ["sql", "policy"]
        assert decision["short_circuited"] is False


class TestRouteAfterRouter:
    def test_routes_sql_only_decision_to_sql_subgraph(self):
        state = {"route_decision": {"sources": ["sql"], "reasoning": "x", "short_circuited": True}}
        assert route_after_router(state) == ["sql_subgraph"]

    def test_fans_out_to_multiple_destinations(self):
        state = {
            "route_decision": {
                "sources": ["sql", "policy", "web"],
                "reasoning": "x",
                "short_circuited": False,
            }
        }
        assert route_after_router(state) == ["sql_subgraph", "policy_rag", "web_search"]

    def test_raises_for_an_unwired_source_name(self):
        state = {
            "route_decision": {
                "sources": ["carrier_pigeon"],
                "reasoning": "x",
                "short_circuited": False,
            }
        }
        with pytest.raises(NotImplementedError):
            route_after_router(state)


class TestSqlSubgraphNode:
    def test_calls_run_agent_and_tags_the_source(self, monkeypatch):
        captured: dict[str, object] = {}
        fake_result: AgentState = {"status": "succeeded", "sql": "SELECT 1", "row_count": 1}

        def fake_run_agent(question, conversation_history, enable_insight):
            captured["args"] = (question, conversation_history, enable_insight)
            return fake_result

        monkeypatch.setattr(orchestrator_nodes, "run_agent", fake_run_agent)

        state = {
            "question": "how many orders?",
            "conversation_history": [],
            "enable_insight": True,
        }
        result = sql_subgraph_node(state)

        assert captured["args"] == ("how many orders?", [], True)
        assert result["status"] == "succeeded"
        assert result["sql"] == "SELECT 1"
        assert result["sources_used"] == ["sql"]

    def test_defaults_enable_insight_to_true_when_absent(self, monkeypatch):
        captured: dict[str, object] = {}

        def fake_run_agent(question, conversation_history, enable_insight):
            captured["enable_insight"] = enable_insight
            return {"status": "succeeded"}

        monkeypatch.setattr(orchestrator_nodes, "run_agent", fake_run_agent)
        sql_subgraph_node({"question": "x"})
        assert captured["enable_insight"] is True


class TestDocumentAndPolicyRagNodes:
    def test_document_rag_node_maps_rag_state_to_source_answer(self, monkeypatch):
        import rag.graph

        monkeypatch.setattr(
            rag.graph,
            "run_rag",
            lambda question, collection: {
                "answer": "Found it.",
                "citations": [{"filename": "a.pdf", "chunk_index": 0, "page_number": 1}],
                "status": "succeeded",
            },
        )
        result = document_rag_node({"question": "what does the manual say?"})
        assert result["document_result"]["answer"] == "Found it."
        assert result["sources_used"] == ["documents"]

    def test_policy_rag_node_uses_policy_source_name_not_raw_collection(self, monkeypatch):
        import rag.graph

        monkeypatch.setattr(
            rag.graph,
            "run_rag",
            lambda question, collection: {"answer": "x", "citations": [], "status": "succeeded"},
        )
        result = policy_rag_node({"question": "what is the leave policy?"})
        # collection is "policies" (plural, matches rag.store.Collection), but
        # the orchestrator-level source name is "policy" (singular, matches
        # get_available_sources/_DESTINATION_NODE_NAMES) -- this test pins
        # down that translation happens correctly.
        assert result["sources_used"] == ["policy"]

    def test_document_rag_node_degrades_gracefully_when_store_not_configured(self, monkeypatch):
        import rag.graph
        from rag.store import RagStoreNotConfiguredError

        def _raise(question, collection):
            raise RagStoreNotConfiguredError("not configured")

        monkeypatch.setattr(rag.graph, "run_rag", _raise)
        result = document_rag_node({"question": "x"})
        assert result["document_result"]["status"] == "failed"


class TestWebSearchNode:
    def test_returns_insufficient_when_no_results(self, monkeypatch):
        settings = _settings(enable_web_search=True, web_search_api_key=SecretStr("tvly-x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import search.web_search

        monkeypatch.setattr(search.web_search, "web_search", lambda query, settings: [])
        result = web_search_node({"question": "something obscure"})
        assert result["web_result"]["status"] == "insufficient_information"
        assert result["sources_used"] == ["web"]

    def test_summarizes_results_and_labels_them_external(self, monkeypatch):
        settings = _settings(enable_web_search=True, web_search_api_key=SecretStr("tvly-x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import search.web_search
        from search.web_search import WebResult

        monkeypatch.setattr(
            search.web_search,
            "web_search",
            lambda query, settings: [
                WebResult(title="T", url="https://x.test", snippet="s", retrieved_at="now")
            ],
        )
        import rag.llm

        monkeypatch.setattr(
            rag.llm, "call_ollama", lambda *a, **k: "According to a live web search: X."
        )
        result = web_search_node({"question": "what's new today?"})
        assert result["web_result"]["status"] == "succeeded"
        assert "web search" in result["web_result"]["answer"].lower()
        assert result["web_result"]["citations"][0]["filename"] == "https://x.test"

    def test_not_configured_degrades_gracefully(self, monkeypatch):
        settings = _settings()  # enable_web_search False, no key
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        result = web_search_node({"question": "x"})
        assert result["web_result"]["status"] == "failed"


class TestSynthesisNode:
    def test_is_a_pass_through_for_a_single_source(self):
        assert synthesis_node({"sources_used": ["sql"]}) == {}

    def test_attributes_each_source_separately_for_multiple_sources(self):
        state = {
            "sources_used": ["sql", "policy"],
            "status": "succeeded",
            "row_count": 3,
            "policy_result": {"answer": "Policy says X.", "citations": [], "status": "succeeded"},
        }
        result = synthesis_node(state)
        synthesized = result["synthesized_answer"]
        assert "Database" in synthesized
        assert "Policy" in synthesized
        assert "Policy says X." in synthesized


class TestRunOrchestrated:
    def test_flag_off_is_a_pure_pass_through_to_run_agent(self, monkeypatch):
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: _settings())

        fake_result: AgentState = {"status": "succeeded", "sql": "SELECT 1"}
        captured: dict[str, object] = {}

        def fake_run_agent(question, conversation_history, enable_insight):
            captured["args"] = (question, conversation_history, enable_insight)
            return fake_result

        monkeypatch.setattr(orchestrator_graph, "run_agent", fake_run_agent)

        def _should_not_build(*args, **kwargs):
            raise AssertionError(
                "build_orchestrator_graph must not be called when the router is off"
            )

        monkeypatch.setattr(orchestrator_graph, "build_orchestrator_graph", _should_not_build)

        result = orchestrator_graph.run_orchestrated("how many orders?", None, True)

        assert result is fake_result
        assert captured["args"] == ("how many orders?", None, True)

    def test_flag_on_routes_through_the_graph_to_sql(self, monkeypatch):
        settings = _settings(enable_multi_source_router=True)
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: settings)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "run_agent",
            lambda question, conversation_history, enable_insight: {
                "status": "succeeded",
                "sql": "SELECT 1",
                "row_count": 3,
            },
        )

        final_state = orchestrator_graph.run_orchestrated("how many orders?", None, True)

        assert final_state["status"] == "succeeded"
        assert final_state["sql"] == "SELECT 1"
        assert final_state["sources_used"] == ["sql"]
        assert final_state["route_decision"]["sources"] == ["sql"]
        assert final_state["route_decision"]["short_circuited"] is True
        assert final_state.get("synthesized_answer") is None

    def test_flag_on_fans_out_to_sql_and_policy_and_synthesizes(self, monkeypatch):
        settings = _settings(
            enable_multi_source_router=True,
            enable_policy_rag=True,
            rag_store_connection_string=SecretStr("x"),
        )
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: settings)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "both relevant"),
        )
        monkeypatch.setattr(
            orchestrator_nodes,
            "run_agent",
            lambda question, conversation_history, enable_insight: {
                "status": "succeeded",
                "sql": "SELECT 1",
                "row_count": 5,
            },
        )
        import rag.graph

        monkeypatch.setattr(
            rag.graph,
            "run_rag",
            lambda question, collection: {
                "answer": "Policy allows 20 days leave.",
                "citations": [],
                "status": "succeeded",
            },
        )

        final_state = orchestrator_graph.run_orchestrated(
            "compare leave taken with policy", None, True
        )

        assert set(final_state["sources_used"]) == {"sql", "policy"}
        assert final_state["policy_result"]["answer"] == "Policy allows 20 days leave."
        assert "Policy allows 20 days leave." in final_state["synthesized_answer"]
        assert "Database" in final_state["synthesized_answer"]
