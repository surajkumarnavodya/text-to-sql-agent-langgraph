    def test_flag_off_with_attachments_still_runs_the_orchestrator_graph(self, monkeypatch):
        """Attaching a file is a per-request opt-in the caller makes
        explicitly -- it must not require also enabling the unrelated
        multi-source router feature (see run_orchestrated's own docstring)."""
        settings = _settings(enable_multi_source_router=False)
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: settings)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)

        import rag.llm

        monkeypatch.setattr(
            rag.llm,
            "call_ollama",
            lambda *args, **kwargs: "mocked response",
        )

        def _should_not_be_called(*args, **kwargs):
            raise AssertionError(
                "run_agent must not be called directly when attachments are present"
            )

        monkeypatch.setattr(orchestrator_graph, "run_agent", _should_not_be_called)
        import attachments.graph as attachments_graph_module

        monkeypatch.setattr(
            attachments_graph_module,
            "run_attachment_qa",
            lambda question, ids, settings=None, owner_subject=None: {
                "status": "succeeded",
                "answer": "The file says revenue was 42000.",
                "used_attachment_ids": ids,
                "vision_unavailable": False,
            },
        )

        final_state = orchestrator_graph.run_orchestrated(
            "what does the file say?", None, True, attachment_ids=["att_1"]
        )

        assert final_state["sources_used"] == ["attachments"]
        assert final_state["attachment_result"]["answer"] == "The file says revenue was 42000."
        assert final_state["status"] == "succeeded"
