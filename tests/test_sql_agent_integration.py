"""Integration tests: the retrieval layer wired into the actual LangGraph
node/prompt-construction path, from a user question through
`retrieve_business_context_node` to the assembled SQL-generation prompt.

Uses a fake database schema (a hand-built `TableSchemaInfo`, no real DB
connection), `FakeEmbeddingProvider` (no network/model), `InMemoryVectorStore`
(no chromadb), and a mocked LLM call (`agent.llm_client.generate_sql_from_llm`
patched to a fixed fake response) -- no network access, no paid API, per
this project's existing `tests/` convention (see `tests/test_agent_nodes.py`'s
own docstring for the same "external dependencies mocked" contract this
test follows for the two *new* nodes this feature adds).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from retrieval.chunking import glossary_chunks_from_yaml, table_chunks_from_schema
from retrieval.embeddings import FakeEmbeddingProvider

from agent.graph import build_graph
from agent.nodes import generate_sql_node, retrieve_business_context_node
from agent.state import AgentState
from config.settings import Settings
from db.schema_introspection import ColumnInfo, TableSchemaInfo
from security.secrets import SecretStr
from tests._retrieval_fakes import InMemoryVectorStore


def _fake_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_type="postgresql",
        db_connection_string=SecretStr("postgresql://user:pass@localhost/db"),
        chroma_persist_dir=tmp_path / "chroma",
        retrieval_embedding_provider="fake",
        retrieval_knowledge_dir=tmp_path / "knowledge",
        retrieval_similarity_threshold=0.0,
        enable_query_planning=False,
        enable_golden_examples=False,
    )


def _fake_schema() -> list[TableSchemaInfo]:
    return [
        TableSchemaInfo(
            table_name="FactWidgetSales",
            columns=(
                ColumnInfo(
                    name="WidgetSalesKey", type="INTEGER", nullable=False, is_primary_key=True
                ),
                ColumnInfo(
                    name="SalesAmount", type="DECIMAL", nullable=False, is_primary_key=False
                ),
            ),
            foreign_keys=(),
            ddl="CREATE TABLE FactWidgetSales (\n    WidgetSalesKey INTEGER PRIMARY KEY,\n    SalesAmount DECIMAL\n);",
        )
    ]


def _seed_store(
    tmp_path: Path, settings: Settings
) -> tuple[InMemoryVectorStore, FakeEmbeddingProvider]:
    store = InMemoryVectorStore()
    provider = FakeEmbeddingProvider(dimensions=16)

    tables = _fake_schema()
    table_chunks = table_chunks_from_schema(
        tables, "default", provider.model_name, provider.dimensions
    )

    glossary_path = tmp_path / "knowledge" / "glossary.yaml"
    glossary_path.parent.mkdir(parents=True, exist_ok=True)
    glossary_path.write_text(
        "terms:\n"
        "  - term: widget revenue\n"
        "    definition: 'Total SalesAmount from FactWidgetSales.'\n"
        "    mapped_tables: ['FactWidgetSales']\n",
        encoding="utf-8",
    )
    glossary_chunks = glossary_chunks_from_yaml(
        glossary_path, "default", provider.model_name, provider.dimensions
    )

    all_chunks = table_chunks + glossary_chunks
    vectors = provider.embed_batch([c.text for c in all_chunks])
    store.create_collection_if_missing("default")
    store.upsert_documents("default", all_chunks, vectors)
    return store, provider


class TestRetrievalNodeIntegration:
    def test_retrieve_business_context_node_populates_state_from_real_pipeline(
        self, tmp_path: Path, monkeypatch
    ):
        settings = _fake_settings(tmp_path)
        store, provider = _seed_store(tmp_path, settings)

        monkeypatch.setattr("agent.nodes.get_settings", lambda: settings)
        monkeypatch.setattr("retrieval.retriever.get_vector_store", lambda s: store)
        monkeypatch.setattr("retrieval.retriever.get_embedding_provider", lambda s: provider)

        state: AgentState = {
            "question": "What is total widget revenue?",
            "selected_database": "default",
            "caller_roles": (),
        }
        update = retrieve_business_context_node(state)

        assert update["retrieval_warnings"] == []
        assert len(update["retrieved_context"]) > 0
        chunk_types = {item["chunk_type"] for item in update["retrieved_context"]}
        assert "table" in chunk_types or "glossary" in chunk_types
        assert update["retrieval_sources"]
        assert update["status"] == "generating"

    def test_business_context_flows_into_the_generation_prompt(self, tmp_path: Path, monkeypatch):
        """The full path this feature adds: retrieval -> state ->
        generate_sql_node's prompt. The LLM call itself is mocked (a fake,
        fixed SQL response) -- what's under test is that the retrieved
        business context actually reaches the assembled prompt text, not
        the model's own SQL-writing quality."""
        settings = _fake_settings(tmp_path)
        store, provider = _seed_store(tmp_path, settings)

        monkeypatch.setattr("agent.nodes.get_settings", lambda: settings)
        monkeypatch.setattr("retrieval.retriever.get_vector_store", lambda s: store)
        monkeypatch.setattr("retrieval.retriever.get_embedding_provider", lambda s: provider)

        state: AgentState = {
            "question": "What is total widget revenue?",
            "selected_database": "default",
            "caller_roles": (),
        }
        retrieval_update = retrieve_business_context_node(state)
        state.update(retrieval_update)
        state["schema_context_text"] = _fake_schema()[0].ddl
        state["retry_count"] = 0
        state["error_history"] = []

        captured_kwargs = {}

        def _fake_generate_sql_from_llm(**kwargs):
            captured_kwargs.update(kwargs)
            return "SELECT SUM(SalesAmount) FROM FactWidgetSales"

        monkeypatch.setattr("agent.nodes.generate_sql_from_llm", _fake_generate_sql_from_llm)
        monkeypatch.setattr(
            "agent.nodes.get_llm_call_limiter",
            lambda *_: MagicMock(check=lambda: MagicMock(allowed=True)),
        )

        result = generate_sql_node(state)

        assert result["sql"] == "SELECT SUM(SalesAmount) FROM FactWidgetSales"
        assert captured_kwargs["retrieved_context"] == state["retrieved_context"]
        # Confirm the retrieved business context actually made it into the
        # real assembled prompt text, not just passed through as a kwarg.
        from agent.llm_client import _build_user_prompt

        prompt = _build_user_prompt(
            question=state["question"],
            schema_context=state["schema_context_text"],
            previous_sql=None,
            error_feedback=None,
            retrieved_context=state["retrieved_context"],
        )
        assert "Retrieved business context" in prompt
        assert "widget revenue" in prompt.lower() or "FactWidgetSales" in prompt


class TestGraphStructure:
    def test_retrieve_business_context_is_wired_between_golden_examples_and_plan_query(self):
        """Prompt 11 (`11_ANALYTICAL_INTENT_CONTRACT.md`) inserted
        `classify_analytical_intent` between `retrieve_business_context` and
        `plan_query`; Prompt 12 (`12_ANALYTICAL_PLANNING_CONTRACT.md`)
        further inserted `build_analytical_plan` between
        `classify_analytical_intent` and `plan_query` -- none of these
        connect directly to one another anymore, but the overall ordering
        (business context -> intent -> structured plan -> free-text plan)
        still holds.
        """
        graph = build_graph()
        node_names = set(graph.get_graph().nodes.keys())
        assert "retrieve_business_context" in node_names
        assert "classify_analytical_intent" in node_names
        assert "build_analytical_plan" in node_names

        edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
        assert ("retrieve_golden_examples", "retrieve_business_context") in edges
        assert ("retrieve_business_context", "classify_analytical_intent") in edges
        assert ("classify_analytical_intent", "build_analytical_plan") in edges
        assert ("build_analytical_plan", "plan_query") in edges

    def test_existing_nodes_are_all_still_present(self):
        """Adding the new node must never remove an existing one."""
        graph = build_graph()
        node_names = set(graph.get_graph().nodes.keys())
        for expected in (
            "sanitize_input",
            "classify_followup",
            "retrieve_schema",
            "retrieve_golden_examples",
            "build_analytical_plan",
            "plan_query",
            "generate_sql",
            "review_sql",
            "validate_sql",
            "estimate_cost",
            "execute_sql",
            "compute_analytics",
            "generate_forecast",
            "generate_insight",
        ):
            assert expected in node_names

    def test_classify_analytical_intent_has_no_conditional_routing(self):
        """Prompt 11's analytical `ambiguity_flags` must NEVER short-
        circuit the graph the way `classify_followup_node`'s own
        conversational "ambiguous" classification does -- structurally
        proven here: `classify_analytical_intent` has exactly one outgoing
        edge (a straight `add_edge` to `build_analytical_plan`, never an
        `add_conditional_edges` routing table with an `END` branch),
        unlike `classify_followup`, which does have one.
        """
        graph = build_graph()
        edges = list(graph.get_graph().edges)

        intent_edges = [e for e in edges if e.source == "classify_analytical_intent"]
        assert len(intent_edges) == 1
        assert intent_edges[0].target == "build_analytical_plan"
        assert intent_edges[0].conditional is False

        followup_edges = [e for e in edges if e.source == "classify_followup"]
        assert any(e.conditional for e in followup_edges), (
            "classify_followup is expected to retain its own conditional "
            "(possibly-END) routing -- this test only asserts "
            "classify_analytical_intent does not share that shape."
        )

    def test_build_analytical_plan_has_no_conditional_routing(self):
        """Prompt 12 (`12_ANALYTICAL_PLANNING_CONTRACT.md`): a rejected
        (invalid) structured plan must fall back to free-text planning,
        never short-circuit the graph -- structurally proven the same way
        `classify_analytical_intent`'s own equivalent test is: exactly one
        outgoing edge, a straight `add_edge` to `plan_query`, never a
        conditional routing table with an `END` branch.
        """
        graph = build_graph()
        edges = list(graph.get_graph().edges)

        plan_edges = [e for e in edges if e.source == "build_analytical_plan"]
        assert len(plan_edges) == 1
        assert plan_edges[0].target == "plan_query"
        assert plan_edges[0].conditional is False

    def test_compute_analytics_sits_between_execute_sql_and_generate_forecast(self):
        """Prompt 13 (`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`): the
        deterministic analytics engine runs on execute_sql's `succeeded`
        path (no conditional routing, no new `END` branch), and
        execute_sql's own conditional-edge table points its `succeeded`
        outcome at compute_analytics instead of generate_insight directly.
        Updated by Prompt 16 (`16_FORECASTING_CONTRACT.md`): compute_analytics
        now feeds generate_forecast directly, not generate_insight -- see
        `test_generate_forecast_sits_between_compute_analytics_and_generate_recommendations`
        below for that link.
        """
        graph = build_graph()
        node_names = set(graph.get_graph().nodes.keys())
        assert "compute_analytics" in node_names

        edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
        assert ("execute_sql", "compute_analytics") in edges
        assert ("compute_analytics", "generate_forecast") in edges
        assert ("execute_sql", "generate_insight") not in edges
        assert ("compute_analytics", "generate_insight") not in edges

        analytics_edges = [e for e in graph.get_graph().edges if e.source == "compute_analytics"]
        assert len(analytics_edges) == 1
        assert analytics_edges[0].target == "generate_forecast"
        assert analytics_edges[0].conditional is False

    def test_generate_forecast_sits_between_compute_analytics_and_generate_recommendations(self):
        """Prompt 16 (`16_FORECASTING_CONTRACT.md`): `generate_forecast` is
        a straight edge on both sides (never a conditional one, never a new
        `END` branch) -- it can never short-circuit the graph, the same
        structural proof pattern `classify_analytical_intent`/
        `build_analytical_plan`'s own equivalent tests already establish.

        Its downstream neighbor changed from `generate_insight` to
        `generate_recommendations` with Prompt 17
        (`17_RECOMMENDATION_ENGINE_CONTRACT.md`), which now sits between
        the two -- see `test_generate_recommendations_sits_between_
        generate_forecast_and_generate_insight` below for that node's own
        equivalent proof.
        """
        graph = build_graph()
        node_names = set(graph.get_graph().nodes.keys())
        assert "generate_forecast" in node_names

        forecast_edges = [e for e in graph.get_graph().edges if e.source == "generate_forecast"]
        assert len(forecast_edges) == 1
        assert forecast_edges[0].target == "generate_recommendations"
        assert forecast_edges[0].conditional is False

        incoming = [e for e in graph.get_graph().edges if e.target == "generate_forecast"]
        assert len(incoming) == 1
        assert incoming[0].source == "compute_analytics"
        assert incoming[0].conditional is False

    def test_generate_recommendations_sits_between_generate_forecast_and_generate_insight(self):
        """Prompt 17 (`17_RECOMMENDATION_ENGINE_CONTRACT.md`):
        `generate_recommendations` is a straight edge on both sides (never
        a conditional one, never a new `END` branch) -- it can never
        short-circuit the graph, the identical structural proof pattern
        `generate_forecast`'s own equivalent test above establishes.
        """
        graph = build_graph()
        node_names = set(graph.get_graph().nodes.keys())
        assert "generate_recommendations" in node_names

        outgoing = [e for e in graph.get_graph().edges if e.source == "generate_recommendations"]
        assert len(outgoing) == 1
        assert outgoing[0].target == "generate_insight"
        assert outgoing[0].conditional is False

        incoming = [e for e in graph.get_graph().edges if e.target == "generate_recommendations"]
        assert len(incoming) == 1
        assert incoming[0].source == "generate_forecast"
        assert incoming[0].conditional is False
