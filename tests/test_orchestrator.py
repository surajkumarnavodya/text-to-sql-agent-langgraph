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
    _looks_like_attachment_only_question,
    attachment_node,
    classify_sources,
    document_rag_node,
    generation_node,
    get_available_sources,
    media_search_node,
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
        # enable_media_search/media_library_path default on/set at the
        # Settings level (see config/settings.py) -- explicitly disabled
        # here so this fixture's "only sql available" baseline matches
        # every other optional source, which defaults off.
        enable_media_search=False,
        media_library_path=None,
    )
    base.update(overrides)
    return Settings(**base)


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

    def test_media_generation_needs_both_flag_and_api_key(self):
        assert get_available_sources(_settings(enable_media_generation=True)) == ["sql"]
        assert get_available_sources(
            _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        ) == ["sql", "generation"]

    def test_media_search_needs_both_flag_and_library_path(self, tmp_path):
        assert get_available_sources(_settings(enable_media_search=True)) == ["sql"]
        assert get_available_sources(
            _settings(enable_media_search=True, media_library_path=tmp_path)
        ) == ["sql", "media_search"]

    def test_attachments_only_available_when_the_request_has_attachments(self):
        assert get_available_sources(_settings(), has_attachments=False) == ["sql"]
        assert get_available_sources(_settings(), has_attachments=True) == ["sql", "attachments"]

    def test_attachments_still_gated_by_the_feature_flag(self):
        settings = _settings(enable_chat_attachments=False)
        assert get_available_sources(settings, has_attachments=True) == ["sql"]


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

    def test_picks_generation_for_a_generate_phrased_question(self, monkeypatch):
        """Only tests the plumbing (the LLM's response is mocked, so this
        doesn't validate real classification judgment -- that needs a
        manual/eval run against live Ollama, same documented limitation as
        rag/search in CLAUDE.md's "Known gaps")."""
        import rag.llm

        monkeypatch.setattr(rag.llm, "call_ollama", lambda *a, **k: "generation")
        sources, _ = classify_sources(
            "create an image of monthly spend", ["sql", "generation"], _settings()
        )
        assert sources == ["generation"]

    def test_picks_sql_for_a_show_me_data_question_even_with_generation_available(
        self, monkeypatch
    ):
        import rag.llm

        monkeypatch.setattr(rag.llm, "call_ollama", lambda *a, **k: "sql")
        sources, _ = classify_sources(
            "show me the top 5 accounts", ["sql", "generation"], _settings()
        )
        assert sources == ["sql"]

    def test_generation_prompt_includes_few_shot_guidance_only_when_available(self, monkeypatch):
        """Regression guard: the GENERATE few-shot examples/counter-rule
        must actually reach the LLM prompt when 'generation' is offered,
        and must NOT bloat the prompt when it isn't configured at all."""
        import rag.llm

        captured: dict[str, str] = {}

        def _capture(system_prompt, user_prompt, settings, max_tokens):
            captured["system_prompt"] = system_prompt
            return "sql"

        monkeypatch.setattr(rag.llm, "call_ollama", _capture)

        classify_sources("x", ["sql", "generation"], _settings())
        assert "Create an image of the top 5 merchants" in captured["system_prompt"]
        assert "show me" in captured["system_prompt"].lower()
        assert "do NOT add" in captured["system_prompt"]

        classify_sources("x", ["sql", "policy"], _settings())
        assert "Create an image of the top 5 merchants" not in captured["system_prompt"]


class TestLooksLikeAttachmentOnlyQuestion:
    """Unit tests for the deterministic, zero-LLM-call pre-check that
    closes the reported bug: an attachment-only question like "Extract
    text" must never route to SQL just because a database happens to
    always be configured. See TestRouterNode's own
    `test_extract_text_with_image_skips_classification_entirely` below for
    the end-to-end proof this pre-check actually prevents
    `classify_sources` from being called at all."""

    @pytest.mark.parametrize(
        "question",
        [
            "Extract text",
            "Extract text from this image.",
            "extract image text",
            "Extract image text",
            "Read this image.",
            "What does this image say?",
            "What is written in this screenshot?",
            "Summarize this PDF.",
            "What does this document contain?",
            "Read the attached document.",
            "Resize this image.",
            "Remove text from this image.",
            "Translate this attached file.",
            "describe this image",
            "Explain this screenshot.",
        ],
    )
    def test_matches_the_reported_bugs_own_repro_questions(self, question):
        assert _looks_like_attachment_only_question(question) is True

    @pytest.mark.parametrize(
        "question",
        [
            "How many employees are in the database?",
            "Show monthly revenue from the database.",
            "Find all orders for customer 123.",
            "Query the sales table.",
            "Generate SQL for active users.",
            "Use this PDF's business definitions to query the database.",
            "Compare this uploaded report with database results.",
            "Generate SQL based on the metrics shown in this image.",
            "Use the attached file to filter database records.",
            "Summarize this file and compare it with our database records.",
        ],
    )
    def test_a_single_database_keyword_anywhere_defers_to_the_classifier(self, question):
        assert _looks_like_attachment_only_question(question) is False

    def test_an_unrecognized_ambiguous_phrasing_also_defers_to_the_classifier(self):
        # Per Rule E (ambiguous requests): "Analyze this" alone isn't in the
        # pattern list, so this deterministic pre-check must NOT force a
        # route decision either way -- it falls through to whatever
        # existing behavior handles an ambiguous question (the classifier,
        # or -- unaffected by this change -- clarification handling
        # elsewhere in the stack).
        assert _looks_like_attachment_only_question("Analyze this") is False


class TestAttachmentVsSqlGuidance:
    def test_prompt_includes_guidance_only_when_both_attachments_and_sql_are_available(
        self, monkeypatch
    ):
        """Regression guard, mirroring
        test_generation_prompt_includes_few_shot_guidance_only_when_available's
        own shape: the attachment-vs-sql few-shot guidance is defense-in-depth
        for the questions the deterministic pre-check above doesn't
        confidently catch -- it must reach the prompt when both sources are
        options, and must not bloat the prompt otherwise."""
        import rag.llm

        captured: dict[str, str] = {}

        def _capture(system_prompt, user_prompt, settings, max_tokens):
            captured["system_prompt"] = system_prompt
            return "attachments"

        monkeypatch.setattr(rag.llm, "call_ollama", _capture)

        classify_sources("x", ["sql", "attachments"], _settings())
        assert "Extract text from this image" in captured["system_prompt"]
        assert "do not add 'sql'" in captured["system_prompt"].lower()

        classify_sources("x", ["sql", "policy"], _settings())
        assert "Extract text from this image" not in captured["system_prompt"]

        # "attachments" available without "sql" can't happen in practice
        # (get_available_sources always includes "sql"), but confirms the
        # guidance is gated on both, not just "attachments" alone.
        classify_sources("x", ["attachments", "documents"], _settings())
        assert "Extract text from this image" not in captured["system_prompt"]


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
        result = router_node({"question": "compare sales with policy", "caller_roles": ("admin",)})
        decision = result["route_decision"]
        assert decision["sources"] == ["sql", "policy"]
        assert decision["short_circuited"] is False

    def test_attachments_are_forced_into_the_route_even_if_the_classifier_omits_them(
        self, monkeypatch
    ):
        """The user explicitly attached a file to this question -- the
        classifier's job is only to decide whether some OTHER source is
        ALSO needed, never whether to include attachments at all (see
        router_node's own docstring). Question deliberately names a
        database keyword ("database records") so the deterministic
        attachment-only pre-check (`_looks_like_attachment_only_question`)
        defers to the classifier here, exactly as it's designed to for a
        genuinely mixed question -- see TestLooksLikeAttachmentOnlyQuestion
        below for that pre-check's own dedicated tests."""
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql"], "classifier picked only sql"),
        )
        result = router_node(
            {
                "question": "summarize this file and compare it with our database records",
                "pending_attachment_ids": ["att_1"],
            }
        )
        decision = result["route_decision"]
        assert "attachments" in decision["sources"]
        assert "sql" in decision["sources"]

    # -- The actual reported bug: attachment-only questions must never
    # reach SQL generation/schema retrieval -- and its fix. --

    def test_extract_text_with_image_skips_classification_entirely(self, monkeypatch):
        """The exact reported bug: an image attached plus "Extract text"
        used to sometimes end up with "sql" in the route (the LLM
        classifier had no reason not to pick it, since a database is
        always configured/available). This asserts the strongest possible
        version of the fix -- classify_sources is never even called, so it
        cannot possibly pick "sql" for this question."""
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())

        def _must_not_be_called(question, available, settings):
            raise AssertionError(
                "classify_sources must not be called for an unambiguous " "attachment-only question"
            )

        monkeypatch.setattr(orchestrator_nodes, "classify_sources", _must_not_be_called)

        result = router_node(
            {"question": "Extract text from this image.", "pending_attachment_ids": ["att_1"]}
        )
        decision = result["route_decision"]
        assert decision["sources"] == ["attachments"]
        assert "sql" not in decision["sources"]
        assert decision["requires_database"] is False
        assert decision["short_circuited"] is True

    @pytest.mark.parametrize(
        "question",
        [
            "Read this image.",
            "What does this image say?",
            "Summarize this PDF.",
            "Read the attached document.",
            "Resize this image.",
            "Remove text from this image.",
            # The exact phrase from a later real bug report -- "extract
            # text" alone matched, but "extract image text" (a different
            # word order) originally did not, so it fell through to the
            # LLM classifier instead of the deterministic override.
            "extract image text",
        ],
    )
    def test_every_bug_repro_question_never_calls_the_classifier(self, monkeypatch, question):
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())

        def _must_not_be_called(q, available, settings):
            raise AssertionError(f"classify_sources must not be called for {q!r}")

        monkeypatch.setattr(orchestrator_nodes, "classify_sources", _must_not_be_called)

        result = router_node({"question": question, "pending_attachment_ids": ["att_1"]})
        decision = result["route_decision"]
        assert decision["sources"] == ["attachments"]
        assert decision["requires_database"] is False

    def test_mixed_attachment_and_database_question_still_reaches_the_classifier(self, monkeypatch):
        """Rule C (attachment plus database request): the deterministic
        pre-check must defer to the classifier -- never silently force
        attachment-only -- once the question itself names a database
        keyword, so "compare this file with the database" keeps working."""
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "attachments"], "classified"),
        )
        result = router_node(
            {
                "question": "Show database records matching the category shown in this image.",
                "pending_attachment_ids": ["att_1"],
            }
        )
        decision = result["route_decision"]
        assert decision["sources"] == ["sql", "attachments"]
        assert decision["requires_database"] is True
        assert decision["short_circuited"] is False

    def test_explicit_database_question_without_attachments_is_unaffected(self, monkeypatch):
        """Rule B (explicit database request): unchanged behavior -- no
        attachments means the deterministic pre-check never applies at
        all, so a plain database question still short-circuits to "sql"
        exactly as it did before this fix."""
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())
        result = router_node({"question": "Show monthly revenue from the database."})
        decision = result["route_decision"]
        assert decision["sources"] == ["sql"]
        assert decision["requires_database"] is True

    def test_no_attachments_means_attachments_is_not_a_route_option(self, monkeypatch):
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())
        result = router_node({"question": "how many orders?", "pending_attachment_ids": []})
        assert "attachments" not in result["route_decision"]["sources"]

    # -- SEC-10: session-scoped expensive-source (generation/web) ceiling --

    def test_drops_expensive_sources_once_session_limit_is_reached(self, monkeypatch):
        import uuid

        settings = _settings(
            enable_web_search=True,
            web_search_api_key=SecretStr("tvly-x"),
            session_expensive_source_limit=1,
            session_expensive_source_window_seconds=3600.0,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes, "classify_sources", lambda q, avail, s: (["sql", "web"], "reason")
        )
        session_id = f"test-session-{uuid.uuid4().hex}"

        first = router_node(
            {"question": "q1", "session_id": session_id, "caller_roles": ("admin",)}
        )
        assert first["route_decision"]["sources"] == ["sql", "web"]

        second = router_node(
            {"question": "q2", "session_id": session_id, "caller_roles": ("admin",)}
        )
        assert second["route_decision"]["sources"] == ["sql"]
        assert "expensive-source limit" in second["route_decision"]["reasoning"]

    def test_no_session_id_never_triggers_the_ceiling(self, monkeypatch):
        """The ceiling only applies when a caller supplies a session_id
        (e.g. eval/runner.py and standalone scripts never do) -- it must
        never silently cap a caller that has no session concept at all."""
        settings = _settings(
            enable_web_search=True,
            web_search_api_key=SecretStr("tvly-x"),
            session_expensive_source_limit=1,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes, "classify_sources", lambda q, avail, s: (["sql", "web"], "reason")
        )
        first = router_node({"question": "q1", "caller_roles": ("admin",)})
        second = router_node({"question": "q2", "caller_roles": ("admin",)})
        assert first["route_decision"]["sources"] == ["sql", "web"]
        assert second["route_decision"]["sources"] == ["sql", "web"]

    def test_ceiling_does_not_affect_a_different_session(self, monkeypatch):
        import uuid

        settings = _settings(
            enable_web_search=True,
            web_search_api_key=SecretStr("tvly-x"),
            session_expensive_source_limit=1,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes, "classify_sources", lambda q, avail, s: (["sql", "web"], "reason")
        )
        session_a = f"test-session-{uuid.uuid4().hex}"
        session_b = f"test-session-{uuid.uuid4().hex}"

        router_node({"question": "q1", "session_id": session_a, "caller_roles": ("admin",)})
        # A different session's budget is untouched by session_a's usage.
        second = router_node(
            {"question": "q2", "session_id": session_b, "caller_roles": ("admin",)}
        )
        assert second["route_decision"]["sources"] == ["sql", "web"]

    def test_caller_subject_is_preferred_over_session_id_for_the_ceiling(self, monkeypatch):
        """2026 Phase 2: when a real authenticated identity exists
        (`caller_subject`, set from `security.oidc.AuthIdentity.subject`),
        the cost ceiling is scoped to *that*, not the client-supplied,
        unauthenticated `session_id` -- so rotating `session_id` alone (a
        documented, real evasion technique -- see
        `get_session_expensive_source_limiter`'s own docstring) no longer
        resets the budget for an authenticated caller."""
        settings = _settings(
            enable_web_search=True,
            web_search_api_key=SecretStr("tvly-x"),
            session_expensive_source_limit=1,
            session_expensive_source_window_seconds=3600.0,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes, "classify_sources", lambda q, avail, s: (["sql", "web"], "reason")
        )

        first = router_node(
            {
                "question": "q1",
                "session_id": "session-a",
                "caller_subject": "user-123",
                "caller_roles": ("admin",),
            }
        )
        assert first["route_decision"]["sources"] == ["sql", "web"]

        # Same authenticated subject, but a *different* session_id --
        # the old, session_id-only evasion trick -- must still be capped.
        second = router_node(
            {
                "question": "q2",
                "session_id": "session-b",
                "caller_subject": "user-123",
                "caller_roles": ("admin",),
            }
        )
        assert second["route_decision"]["sources"] == ["sql"]
        assert "expensive-source limit" in second["route_decision"]["reasoning"]


class TestRouterNodeAuthorization:
    """2026 Phase 2 security review: regression coverage for closing
    2026 Phase 1's AGT-01/R-008 finding -- the LLM router's own
    classification decision used to be sufficient by itself to reach the
    access-sensitive "policies" collection or trigger paid media
    generation, with no independent check of whether the *caller* was
    actually permitted to. `router_node` now drops any LLM-selected source
    the caller's role(s) don't grant, before `route_after_router` can ever
    fan out to that source's subgraph node -- verified here at the
    `router_node` level (the LLM classification itself is mocked, exactly
    like the rest of `TestRouterNode` above); `tests/test_api_authz.py`
    covers the same principle end-to-end through the real `/ask` route.
    """

    def _settings_with_policy_and_web(self, **overrides):
        return _settings(
            enable_policy_rag=True,
            rag_store_connection_string=SecretStr("x"),
            enable_web_search=True,
            web_search_api_key=SecretStr("tvly-x"),
            **overrides,
        )

    def test_viewer_role_loses_policy_and_web_but_keeps_sql(self, monkeypatch):
        """Horizontal/vertical privilege escalation check: a low-privilege
        role ("viewer" grants neither POLICY_RAG_QUERY nor WEB_SEARCH in
        the default role map, see agent/authz.py) must never reach either
        source purely by having the LLM classifier pick them."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy", "web"], "all relevant"),
        )

        result = router_node({"question": "compare policy with sales", "caller_roles": ("viewer",)})

        assert result["route_decision"]["sources"] == ["sql"]

    def test_missing_caller_roles_is_treated_as_no_permissions(self, monkeypatch):
        """A state dict that never set caller_roles at all (e.g. an older
        caller, or a bug upstream) must fail closed -- the same as an
        explicit empty tuple -- never fail open and grant every source."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "relevant"),
        )

        result = router_node({"question": "compare policy with sales"})

        assert result["route_decision"]["sources"] == ["sql"]

    def test_invalid_unrecognized_role_grants_nothing(self, monkeypatch):
        """A role name `agent.authz.ROLE_PERMISSIONS` doesn't recognize
        (a typo, a role this app hasn't been configured for, a role from
        an identity provider claim this deployment never mapped) must
        grant no permissions -- fail closed on an invalid role, not fail
        open."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "relevant"),
        )

        result = router_node(
            {"question": "compare policy with sales", "caller_roles": ("not-a-real-role",)}
        )

        assert result["route_decision"]["sources"] == ["sql"]

    def test_analyst_role_keeps_policy_but_user_role_does_not(self, monkeypatch):
        """Confirms the default role hierarchy actually differentiates --
        not every non-viewer role is equivalent."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "relevant"),
        )

        analyst_result = router_node(
            {"question": "compare policy with sales", "caller_roles": ("analyst",)}
        )
        user_result = router_node(
            {"question": "compare policy with sales", "caller_roles": ("user",)}
        )

        assert analyst_result["route_decision"]["sources"] == ["sql", "policy"]
        assert user_result["route_decision"]["sources"] == ["sql"]

    def test_denial_falls_back_to_sql_alone_never_to_an_empty_route(self, monkeypatch):
        """If every LLM-selected source is denied, the route must still
        resolve to something runnable ("sql"), never an empty destination
        list `route_after_router` would have nothing to do with."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["policy", "web"], "relevant"),
        )

        result = router_node({"question": "compare policy with sales", "caller_roles": ("viewer",)})

        assert result["route_decision"]["sources"] == ["sql"]

    def test_denial_is_audit_logged_without_leaking_content(self, monkeypatch, caplog):
        import logging

        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "relevant"),
        )

        with caplog.at_level(logging.WARNING, logger="security.audit"):
            router_node({"question": "compare policy with sales", "caller_roles": ("viewer",)})

        events = [
            r.message for r in caplog.records if "event=orchestrator_source_denied" in r.message
        ]
        assert len(events) == 1
        assert "policy_rag_query" in events[0]
        assert "'viewer'" in events[0]

    def test_admin_role_reaches_every_source(self, monkeypatch):
        """Sanity check against over-restriction: the highest default role
        must still be able to reach every source the LLM picks."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy", "web"], "all relevant"),
        )

        result = router_node({"question": "compare policy with sales", "caller_roles": ("admin",)})

        assert result["route_decision"]["sources"] == ["sql", "policy", "web"]

    def test_denial_sets_a_human_readable_notice(self, monkeypatch):
        """A denied source must leave a trace the caller actually sees --
        AskResponse.permission_denied_notice -- not just a server log line,
        since the fallback source's own answer (e.g. a SQL off-topic
        rejection) gives no hint the real cause was a role restriction."""
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["policy", "web"], "relevant"),
        )

        result = router_node({"question": "compare policy with sales", "caller_roles": ("viewer",)})

        assert result["permission_denied_notice"] is not None
        assert "restricted policy documents" in result["permission_denied_notice"]
        assert "live web search" in result["permission_denied_notice"]

    def test_no_denial_leaves_notice_none(self, monkeypatch):
        settings = self._settings_with_policy_and_web()
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda q, avail, s: (["sql", "policy"], "relevant"),
        )

        result = router_node({"question": "compare policy with sales", "caller_roles": ("admin",)})

        assert result["permission_denied_notice"] is None


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

    def test_routes_media_search_to_media_search_node(self):
        state = {
            "route_decision": {
                "sources": ["sql", "media_search"],
                "reasoning": "x",
                "short_circuited": False,
            }
        }
        assert route_after_router(state) == ["sql_subgraph", "media_search"]

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

        def fake_run_agent(
            question,
            conversation_history,
            enable_insight,
            caller_roles,
            model=None,
            forecast_horizon=None,
        ):
            captured["args"] = (question, conversation_history, enable_insight, caller_roles, model)
            captured["forecast_horizon"] = forecast_horizon
            return fake_result

        monkeypatch.setattr(orchestrator_nodes, "run_agent", fake_run_agent)

        state = {
            "question": "how many orders?",
            "conversation_history": [],
            "enable_insight": True,
        }
        result = sql_subgraph_node(state)

        assert captured["args"] == ("how many orders?", [], True, (), None)
        assert result["status"] == "succeeded"
        assert result["sql"] == "SELECT 1"
        assert result["sources_used"] == ["sql"]

    def test_defaults_enable_insight_to_true_when_absent(self, monkeypatch):
        captured: dict[str, object] = {}

        def fake_run_agent(
            question,
            conversation_history,
            enable_insight,
            caller_roles,
            model=None,
            forecast_horizon=None,
        ):
            captured["enable_insight"] = enable_insight
            return {"status": "succeeded"}

        monkeypatch.setattr(orchestrator_nodes, "run_agent", fake_run_agent)
        sql_subgraph_node({"question": "x"})
        assert captured["enable_insight"] is True


class TestMediaSearchNode:
    def test_returns_insufficient_when_no_hits(self, monkeypatch):
        settings = _settings(enable_media_search=True)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media.search

        monkeypatch.setattr(media.search, "search_media", lambda query, settings, **k: [])

        result = media_search_node({"question": "find the photo of the site inspection"})

        assert result["media_search_result"]["status"] == "insufficient_information"
        assert result["media_search_result"]["hits"] == []
        assert result["sources_used"] == ["media_search"]

    def test_composes_answer_and_carries_structured_hits(self, monkeypatch):
        settings = _settings(enable_media_search=True)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media.search
        from media.search import MediaHit

        monkeypatch.setattr(
            media.search,
            "search_media",
            lambda query, settings, **k: [
                MediaHit(
                    media_id="seg1",
                    media_type="video",
                    caption="a crane lifts a steel beam",
                    similarity=0.9,
                    timestamp_start=10.0,
                    timestamp_end=15.0,
                )
            ],
        )
        import rag.llm

        monkeypatch.setattr(
            rag.llm, "call_ollama", lambda *a, **k: "Found a video of a crane lifting a beam."
        )

        result = media_search_node({"question": "show me the clip where the crane lifts the beam"})

        assert result["media_search_result"]["status"] == "succeeded"
        assert "crane" in result["media_search_result"]["answer"].lower()
        assert result["media_search_result"]["hits"] == [
            {
                "media_id": "seg1",
                "media_type": "video",
                "caption": "a crane lifts a steel beam",
                "timestamp_start": 10.0,
                "timestamp_end": 15.0,
            }
        ]
        assert result["sources_used"] == ["media_search"]

    def test_degrades_gracefully_on_a_search_failure(self, monkeypatch):
        settings = _settings(enable_media_search=True)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media.search

        def _raise(query, settings, **k):
            raise RuntimeError("chroma unavailable")

        monkeypatch.setattr(media.search, "search_media", _raise)

        result = media_search_node({"question": "find the photo"})

        assert result["media_search_result"]["status"] == "failed"
        assert result["sources_used"] == ["media_search"]


class TestDocumentAndPolicyRagNodes:
    def test_document_rag_node_maps_rag_state_to_source_answer(self, monkeypatch):
        import rag.graph

        monkeypatch.setattr(
            rag.graph,
            "run_rag",
            lambda question, collection, **kwargs: {
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
            lambda question, collection, **kwargs: {
                "answer": "x",
                "citations": [],
                "status": "succeeded",
            },
        )
        result = policy_rag_node({"question": "what is the leave policy?"})
        # collection is "policies" (plural, matches rag.store.Collection), but
        # the orchestrator-level source name is "policy" (singular, matches
        # get_available_sources/_DESTINATION_NODE_NAMES) -- this test pins
        # down that translation happens correctly.
        assert result["sources_used"] == ["policy"]

    def test_document_rag_node_threads_caller_roles_through(self, monkeypatch):
        """2026 Phase 3 security review: caller_roles must reach run_rag,
        not just be read from state and discarded -- otherwise
        rag/graph.py's restricted_roles gate would silently never see the
        real caller when reached through the orchestrator."""
        import rag.graph

        captured = {}

        def _run_rag(question, collection, **kwargs):
            captured["caller_roles"] = kwargs.get("caller_roles")
            return {"answer": "x", "citations": [], "status": "succeeded"}

        monkeypatch.setattr(rag.graph, "run_rag", _run_rag)
        document_rag_node({"question": "x", "caller_roles": ("analyst", "user")})
        assert captured["caller_roles"] == ("analyst", "user")

    def test_document_rag_node_degrades_gracefully_when_store_not_configured(self, monkeypatch):
        import rag.graph
        from rag.store import RagStoreNotConfiguredError

        def _raise(question, collection, **kwargs):
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

    def test_uses_the_configured_answer_token_budget(self, monkeypatch):
        """Regression guard: the answer-drafting call must use
        `Settings.web_search_answer_max_tokens` (a larger, dedicated budget
        for a structured, in-depth answer), not a small hardcoded value."""
        settings = _settings(
            enable_web_search=True,
            web_search_api_key=SecretStr("tvly-x"),
            web_search_answer_max_tokens=1200,
        )
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

        captured = {}

        def _capture(system_prompt, user_prompt, settings, max_tokens, **kwargs):
            captured["max_tokens"] = max_tokens
            return "According to a live web search: X."

        monkeypatch.setattr(rag.llm, "call_ollama", _capture)

        web_search_node({"question": "what's new today?"})

        assert captured["max_tokens"] == 1200


class TestGenerationNode:
    """Mocks `media_gen`'s client construction and `generate_image`/
    `generate_video`/`download_media_bytes` -- never a real network call,
    even though image generation is separately confirmed working against a
    real IMA account (see `media_gen/client.py`'s module docstring); these
    tests stay fully mocked like the rest of this suite regardless.
    """

    @pytest.fixture(autouse=True)
    def _reset_media_generation_limiter(self):
        """Same reasoning as test_agent_nodes.py's `_reset_llm_call_limiter`
        -- `get_media_generation_limiter` is a module-level singleton, so
        tests in this class would otherwise share one running counter.
        Unlike that fixture, this clears the singleton back to unconstructed
        (rather than calling the getter with one fixed limit/window) since
        `test_rate_limit_blocks_after_the_configured_number_of_calls` below
        needs its own `_settings(media_gen_rate_limit=1, ...)` to actually
        take effect on first construction, not be silently ignored because
        an earlier test already built the singleton with a different limit."""
        import agent.rate_limit as rate_limit_module

        rate_limit_module._media_generation_limiter = None
        yield
        rate_limit_module._media_generation_limiter = None

    def test_not_configured_degrades_gracefully(self, monkeypatch):
        settings = _settings()  # enable_media_generation False, no key
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        result = generation_node({"question": "generate a picture of a cat"})
        assert result["generation_result"]["status"] == "failed"
        assert result["sources_used"] == ["generation"]

    def test_basic_prompt_safety_check_rejects_before_any_api_call(self, monkeypatch):
        settings = _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("generate_image must not be called when the safety check rejects")

        monkeypatch.setattr(media_gen, "generate_image", _fail_if_called)

        result = generation_node({"question": "generate an explicit image of X"})
        assert result["generation_result"]["status"] == "failed"
        assert "content policy" in result["generation_result"]["answer"].lower()

    # -- SEC-03: human-in-the-loop approval gate --------------------------

    def test_defaults_to_requiring_approval(self):
        """Settings.require_generation_approval defaults to True -- secure
        by default, since this is the only source that spends real money."""
        assert _settings().require_generation_approval is True

    def test_proposes_without_calling_the_provider_when_approval_required(self, monkeypatch):
        """The whole point of the gate: no real, metered API call happens
        just because the router picked "generation" -- only a proposal."""
        settings = _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("no provider call may happen before human confirmation")

        monkeypatch.setattr(media_gen, "generate_image", _fail_if_called)
        monkeypatch.setattr(media_gen, "generate_video", _fail_if_called)
        monkeypatch.setattr(media_gen, "download_media_bytes", _fail_if_called)

        result = generation_node({"question": "generate a picture of a cat"})
        assert result["generation_result"]["status"] == "pending_approval"
        assert result["generation_result"]["media_id"] is None
        assert "cat" in result["generation_result"]["answer"]

    def test_pending_approval_does_not_consume_the_rate_limit(self, monkeypatch):
        """Proposing costs nothing -- the media-generation rate limiter
        must only be checked at actual execution time (confirm), not here,
        or a user could exhaust their budget just by asking questions that
        get proposed and never confirmed."""
        settings = _settings(
            enable_media_generation=True,
            ima_api_key=SecretStr("ima_x"),
            media_gen_rate_limit=1,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)

        first = generation_node({"question": "generate a picture of a cat"})
        second = generation_node({"question": "generate a picture of a dog"})
        assert first["generation_result"]["status"] == "pending_approval"
        assert second["generation_result"]["status"] == "pending_approval"

    def test_not_configured_fails_fast_even_with_approval_required(self, monkeypatch):
        """No reason to make a human click 'confirm' just to learn
        generation was never going to work -- this is a free config check,
        not a provider call, so it happens at propose time too."""
        settings = _settings(enable_media_generation=True)  # no ima_api_key
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        result = generation_node({"question": "generate a picture of a cat"})
        assert result["generation_result"]["status"] == "failed"
        assert result["generation_result"]["media_id"] is None

    def test_executes_directly_when_approval_disabled(self, monkeypatch):
        """Settings.require_generation_approval=false restores the
        previous fully-autonomous behavior, opt-in only."""
        settings = _settings(
            enable_media_generation=True,
            ima_api_key=SecretStr("ima_x"),
            require_generation_approval=False,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(
            media_gen,
            "generate_image",
            lambda client, prompt: media_gen.MediaResult(
                status="completed", url="https://cdn.example/img.png", model="seedream-4.5"
            ),
        )
        monkeypatch.setattr(
            media_gen,
            "download_media_bytes",
            lambda url, timeout=30.0, max_bytes=0: (b"bytes", "image/png"),
        )
        result = generation_node({"question": "generate a picture of a cat"})
        assert result["generation_result"]["status"] == "succeeded"
        assert result["generation_result"]["media_id"]


class TestExecuteGeneration:
    """Unit tests for `execute_generation` -- the function that actually
    calls IMA, whether reached via `generation_node` (approval disabled) or
    `POST /generate/confirm` (approval required, the default). Mocks
    `media_gen`'s client construction and `generate_image`/`generate_video`/
    `download_media_bytes` -- never a real network call, even though image
    generation is separately confirmed working against a real IMA account
    (see `media_gen/client.py`'s module docstring)."""

    @pytest.fixture(autouse=True)
    def _reset_media_generation_limiter(self):
        """Same reasoning as test_agent_nodes.py's `_reset_llm_call_limiter`
        -- `get_media_generation_limiter` is a module-level singleton, so
        tests in this class would otherwise share one running counter.
        Unlike that fixture, this clears the singleton back to unconstructed
        (rather than calling the getter with one fixed limit/window) since
        `test_rate_limit_blocks_after_the_configured_number_of_calls` below
        needs its own `_settings(media_gen_rate_limit=1, ...)` to actually
        take effect on first construction, not be silently ignored because
        an earlier test already built the singleton with a different limit."""
        import agent.rate_limit as rate_limit_module

        rate_limit_module._media_generation_limiter = None
        yield
        rate_limit_module._media_generation_limiter = None

    def test_successful_generation_returns_media_id(self, monkeypatch):
        """The raw provider URL must never reach `MediaGenerationResult` --
        only an opaque `media_id` to fetch via `GET /media/{media_id}`
        (api/media.py) -- and `answer` must never contain a link either."""
        settings = _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(
            media_gen,
            "generate_image",
            lambda client, prompt: media_gen.MediaResult(
                status="completed", url="https://cdn.example/img.png", model="seedream-4.5"
            ),
        )
        monkeypatch.setattr(
            media_gen,
            "download_media_bytes",
            lambda url, timeout=30.0, max_bytes=0: (b"bytes", "image/png"),
        )
        result = orchestrator_nodes.execute_generation(
            "generate a chart-style image of top sales", "image", settings
        )
        assert result["status"] == "succeeded"
        assert result["media_id"]
        assert result["media_type"] == "image"
        assert "http" not in result["answer"]
        assert "://" not in result["answer"]

        cached = media_gen.get_media_cache().get(result["media_id"])
        assert cached is not None
        assert cached.data == b"bytes"
        assert cached.content_type == "image/png"

    def test_download_failure_after_successful_generation_fails_closed(self, monkeypatch):
        """If the generated asset's bytes can't be downloaded, the whole
        result must be reported as failed rather than falling back to
        exposing the raw provider URL."""
        settings = _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(
            media_gen,
            "generate_image",
            lambda client, prompt: media_gen.MediaResult(
                status="completed", url="https://cdn.example/img.png", model="seedream-4.5"
            ),
        )

        def _fail_download(url, timeout=30.0, max_bytes=0):
            raise media_gen.MediaGenerationError("boom")

        monkeypatch.setattr(media_gen, "download_media_bytes", _fail_download)

        result = orchestrator_nodes.execute_generation(
            "generate a picture of a cat", "image", settings
        )
        assert result["status"] == "failed"
        assert result["media_id"] is None

    def test_video_kind_calls_generate_video_not_generate_image(self, monkeypatch):
        settings = _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(
            media_gen,
            "download_media_bytes",
            lambda url, timeout=30.0, max_bytes=0: (b"bytes", "video/mp4"),
        )

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("generate_image must not be called for a video kind")

        monkeypatch.setattr(media_gen, "generate_image", _fail_if_called)
        monkeypatch.setattr(
            media_gen,
            "generate_video",
            lambda client, prompt, duration_seconds=None: media_gen.MediaResult(
                status="completed", url="https://cdn.example/video.mp4", model="wan-2.6"
            ),
        )

        result = orchestrator_nodes.execute_generation(
            "animate the growth in spend over the year", "video", settings
        )
        assert result["status"] == "succeeded"
        assert result["media_type"] == "video"

    def test_video_duration_setting_is_passed_through(self, monkeypatch):
        settings = _settings(
            enable_media_generation=True,
            ima_api_key=SecretStr("ima_x"),
            media_gen_video_duration_seconds=10,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(
            media_gen,
            "download_media_bytes",
            lambda url, timeout=30.0, max_bytes=0: (b"bytes", "video/mp4"),
        )
        captured: dict[str, object] = {}

        def _capture(client, prompt, duration_seconds=None):
            captured["duration_seconds"] = duration_seconds
            return media_gen.MediaResult(
                status="completed", url="https://cdn.example/video.mp4", model="seedance-2.0"
            )

        monkeypatch.setattr(media_gen, "generate_video", _capture)

        orchestrator_nodes.execute_generation("animate the growth over the year", "video", settings)
        assert captured["duration_seconds"] == 10

    def test_provider_failure_surfaces_as_clean_message(self, monkeypatch):
        """A rejection/error from IMA (e.g. insufficient credits, an
        invalid key, no model available for the account) must become a
        clean MediaGenerationResult, never an uncaught exception."""
        settings = _settings(enable_media_generation=True, ima_api_key=SecretStr("ima_x"))
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(
            media_gen,
            "generate_image",
            lambda client, prompt: media_gen.MediaResult(
                status="failed", error="IMA API error 404 on POST /v1/images/generate: ..."
            ),
        )
        result = orchestrator_nodes.execute_generation(
            "generate a picture of a cat", "image", settings
        )
        assert result["status"] == "failed"
        assert "Image generation failed" in result["answer"]

    def test_rate_limit_blocks_after_the_configured_number_of_calls(self, monkeypatch):
        """Confirms the sliding-window limiter is actually checked before
        every call into media_gen -- not just constructed and ignored."""
        settings = _settings(
            enable_media_generation=True,
            ima_api_key=SecretStr("ima_x"),
            media_gen_rate_limit=1,
            media_gen_rate_window_seconds=60.0,
        )
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        import media_gen

        call_count = {"n": 0}

        def _fake_generate_image(client, prompt):
            call_count["n"] += 1
            return media_gen.MediaResult(status="completed", url="https://cdn.example/img.png")

        monkeypatch.setattr(media_gen, "get_ima_client", lambda settings: object())
        monkeypatch.setattr(media_gen, "generate_image", _fake_generate_image)
        monkeypatch.setattr(
            media_gen,
            "download_media_bytes",
            lambda url, timeout=30.0, max_bytes=0: (b"bytes", "image/png"),
        )

        first = orchestrator_nodes.execute_generation(
            "generate a picture of a cat", "image", settings
        )
        second = orchestrator_nodes.execute_generation(
            "generate a picture of a dog", "image", settings
        )

        assert first["status"] == "succeeded"
        assert second["status"] == "failed"
        assert "too many" in second["answer"].lower()
        assert call_count["n"] == 1  # the second call never reached media_gen at all

    def test_safety_check_rejects_before_any_api_call(self, monkeypatch):
        import media_gen

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("generate_image must not be called when the safety check rejects")

        monkeypatch.setattr(media_gen, "generate_image", _fail_if_called)

        result = orchestrator_nodes.execute_generation(
            "generate an explicit image of X", "image", _settings()
        )
        assert result["status"] == "failed"
        assert "content policy" in result["answer"].lower()


class TestSynthesisNode:
    def test_is_a_pass_through_for_a_single_sql_source(self):
        """`sql_subgraph_node`'s own merge already set `status` -- synthesis
        must never override it."""
        assert synthesis_node({"sources_used": ["sql"]}) == {}

    def test_sets_status_succeeded_for_a_single_non_sql_source(self):
        """Regression test: before this fix, a documents/policy/web/
        generation-only run's top-level `status` stayed stuck at
        `run_orchestrated`'s initial "pending" forever, since nothing else
        in the graph ever set it for a non-SQL source."""
        state = {
            "sources_used": ["generation"],
            "generation_result": {
                "answer": "Image generated successfully.",
                "citations": [],
                "status": "succeeded",
                "media_id": "abc123",
                "media_type": "image",
                "model": "seedream",
            },
        }
        assert synthesis_node(state) == {"status": "succeeded"}

    def test_pending_approval_generation_does_not_report_status_failed(self):
        """Regression test for a real reported bug: a generation request
        awaiting human confirmation (Settings.require_generation_approval)
        was incorrectly reported as `status="failed"`, showing a
        misleading "agent could not produce a working query" banner in
        both UIs before the user had even had a chance to confirm or
        decline. Nothing has failed -- status must stay untouched
        (run_orchestrated's initial "pending"), not "succeeded" either."""
        state = {
            "sources_used": ["generation"],
            "generation_result": {
                "answer": 'This will generate a new image for: "a cat". Confirm to proceed.',
                "citations": [],
                "status": "pending_approval",
                "media_id": None,
                "media_type": "image",
                "model": None,
            },
        }
        assert synthesis_node(state) == {}

    def test_pending_approval_alongside_another_empty_source_still_not_failed(self):
        state = {
            "sources_used": ["generation", "web"],
            "generation_result": {
                "answer": 'This will generate a new image for: "a cat". Confirm to proceed.',
                "citations": [],
                "status": "pending_approval",
                "media_id": None,
                "media_type": "image",
                "model": None,
            },
            "web_result": {
                "answer": "No web results found for this question.",
                "citations": [],
                "status": "insufficient_information",
            },
        }
        result = synthesis_node(state)
        assert "status" not in result

    def test_sets_status_failed_when_the_single_non_sql_source_found_nothing(self):
        state = {
            "sources_used": ["web"],
            "web_result": {
                "answer": "No web results found for this question.",
                "citations": [],
                "status": "insufficient_information",
            },
        }
        assert synthesis_node(state) == {"status": "failed"}

    def test_sets_status_succeeded_for_a_restricted_single_source(self):
        """A restricted match means something relevant was found, just not
        shown -- that's still a "succeeded" outcome, not a failure."""
        state = {
            "sources_used": ["policy"],
            "policy_result": {
                "answer": "This question touches restricted policy content.",
                "citations": [],
                "status": "restricted",
            },
        }
        assert synthesis_node(state) == {"status": "succeeded"}

    def test_multi_source_without_sql_also_sets_status(self):
        state = {
            "sources_used": ["generation", "web"],
            "generation_result": {
                "answer": "Image generated successfully.",
                "citations": [],
                "status": "succeeded",
                "media_id": "abc123",
                "media_type": "image",
                "model": "seedream",
            },
            "web_result": {
                "answer": "According to a live web search: ...",
                "citations": [],
                "status": "succeeded",
            },
        }
        result = synthesis_node(state)
        assert result["status"] == "succeeded"
        assert "synthesized_answer" in result

    def test_generation_never_contributes_a_text_bullet_to_synthesized_answer(self):
        """Regression test for a real reported bug: the router sometimes
        picks ["generation", "web"] (or similar) for a plain "generate an
        image of X" question, not just ["generation"] alone. Both UIs
        always render the actual image/video from `generation_result`
        separately (outside this text), so folding "Image generated
        successfully." into the combined text would be redundant -- and
        is exactly what made the image look "missing" (the UI showed only
        the web source's synthesized text)."""
        state = {
            "sources_used": ["generation", "web"],
            "generation_result": {
                "answer": "Image generated successfully.",
                "citations": [],
                "status": "succeeded",
                "media_id": "abc123",
                "media_type": "image",
                "model": "seedream",
            },
            "web_result": {
                "answer": "According to a live web search: cats are popular pets.",
                "citations": [],
                "status": "succeeded",
            },
        }
        synthesized = synthesis_node(state)["synthesized_answer"]
        assert "cats are popular pets" in synthesized
        assert "Image generated successfully" not in synthesized
        assert "Generated Media" not in synthesized

    def test_successful_generation_alone_suppresses_the_not_found_fallback(self):
        """If generation succeeds but the only other source came up empty,
        the generic "I couldn't find anything" message must not appear --
        something *was* found (the image), even though generation itself
        never contributes a text bullet."""
        state = {
            "sources_used": ["generation", "web"],
            "generation_result": {
                "answer": "Image generated successfully.",
                "citations": [],
                "status": "succeeded",
                "media_id": "abc123",
                "media_type": "image",
                "model": "seedream",
            },
            "web_result": {
                "answer": "No web results found for this question.",
                "citations": [],
                "status": "insufficient_information",
            },
        }
        result = synthesis_node(state)
        assert result["status"] == "succeeded"
        assert (
            result["synthesized_answer"]
            != "I couldn't find any relevant information to answer that question."
        )

    def test_multi_source_with_sql_never_overrides_sql_status(self):
        state = {
            "sources_used": ["sql", "policy"],
            "status": "failed",
            "failure_explanation": "Gave up after 3 attempts.",
            "policy_result": {"answer": "Policy says X.", "citations": [], "status": "succeeded"},
        }
        result = synthesis_node(state)
        assert "status" not in result  # sql_subgraph_node's own status stands

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

    def test_media_search_contributes_a_text_bullet_unlike_generation(self):
        """Unlike `generation_result` (a freshly-created asset with no
        natural text answer), `media_search_result` has a real citable
        answer and behaves like document/policy/web here -- it shows up in
        the combined synthesized text, not just as its own rendered card."""
        state = {
            "sources_used": ["sql", "media_search"],
            "status": "succeeded",
            "row_count": 3,
            "media_search_result": {
                "answer": "Found a photo of the site inspection.",
                "citations": [],
                "status": "succeeded",
                "hits": [],
            },
        }
        synthesized = synthesis_node(state)["synthesized_answer"]
        assert "Media Library" in synthesized
        assert "Found a photo of the site inspection." in synthesized

    def test_suppresses_an_empty_source_when_another_one_succeeded(self):
        """The database answered; the policy collection found nothing --
        the user should see only the database's answer, not a "nothing in
        the policies collection" aside."""
        state = {
            "sources_used": ["sql", "policy"],
            "status": "succeeded",
            "row_count": 3,
            "policy_result": {
                "answer": "I couldn't find any relevant information to answer that question.",
                "citations": [],
                "status": "insufficient_information",
            },
        }
        synthesized = synthesis_node(state)["synthesized_answer"]
        assert "Database" in synthesized
        assert "3 row(s)" in synthesized
        assert "Policy" not in synthesized
        assert "couldn't find" not in synthesized

    def test_suppresses_a_failed_sql_result_when_another_source_succeeded(self):
        state = {
            "sources_used": ["sql", "policy"],
            "status": "failed",
            "failure_explanation": "Gave up after 3 attempts.",
            "policy_result": {"answer": "Policy says X.", "citations": [], "status": "succeeded"},
        }
        synthesized = synthesis_node(state)["synthesized_answer"]
        assert "Policy says X." in synthesized
        assert "Database" not in synthesized
        assert "Gave up" not in synthesized

    def test_generic_not_found_message_when_every_source_is_empty(self):
        state = {
            "sources_used": ["sql", "policy", "web"],
            "status": "failed",
            "failure_explanation": "Gave up after 3 attempts.",
            "policy_result": {
                "answer": "I couldn't find any relevant information to answer that question.",
                "citations": [],
                "status": "insufficient_information",
            },
            "web_result": {
                "answer": "No web results found for this question.",
                "citations": [],
                "status": "insufficient_information",
            },
        }
        synthesized = synthesis_node(state)["synthesized_answer"]
        assert synthesized == "I couldn't find any relevant information to answer that question."

    def test_restricted_result_is_shown_even_alongside_a_successful_one(self):
        """A restricted match means relevant content exists but can't be
        shown -- that's worth telling the user, unlike a genuinely empty
        result, so it must never be suppressed just because another source
        also answered."""
        state = {
            "sources_used": ["sql", "policy"],
            "status": "succeeded",
            "row_count": 2,
            "policy_result": {
                "answer": "This question touches policy content classified 'compensation', ...",
                "citations": [],
                "status": "restricted",
            },
        }
        synthesized = synthesis_node(state)["synthesized_answer"]
        assert "Database" in synthesized
        assert "Policy" in synthesized
        assert "classified 'compensation'" in synthesized


class TestRunOrchestrated:
    def test_flag_off_is_a_pure_pass_through_to_run_agent(self, monkeypatch):
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: _settings())

        fake_result: AgentState = {"status": "succeeded", "sql": "SELECT 1"}
        captured: dict[str, object] = {}

        def fake_run_agent(
            question,
            conversation_history,
            enable_insight,
            caller_roles,
            model=None,
            tenant_id=None,
            forecast_horizon=None,
        ):
            captured["args"] = (
                question,
                conversation_history,
                enable_insight,
                caller_roles,
                model,
                tenant_id,
                forecast_horizon,
            )
            return fake_result

        monkeypatch.setattr(orchestrator_graph, "run_agent", fake_run_agent)

        def _should_not_build(*args, **kwargs):
            raise AssertionError(
                "build_orchestrator_graph must not be called when the router is off"
            )

        monkeypatch.setattr(orchestrator_graph, "build_orchestrator_graph", _should_not_build)

        result = orchestrator_graph.run_orchestrated("how many orders?", None, True)

        assert result is fake_result
        assert captured["args"] == ("how many orders?", None, True, (), None, None, None)

    def test_flag_on_routes_through_the_graph_to_sql(self, monkeypatch):
        settings = _settings(enable_multi_source_router=True)
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: settings)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            orchestrator_nodes,
            "run_agent",
            lambda question, conversation_history, enable_insight, caller_roles, model=None, forecast_horizon=None: {
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
            lambda question, conversation_history, enable_insight, caller_roles, model=None, forecast_horizon=None: {
                "status": "succeeded",
                "sql": "SELECT 1",
                "row_count": 5,
            },
        )
        import rag.graph

        monkeypatch.setattr(
            rag.graph,
            "run_rag",
            lambda question, collection, **kwargs: {
                "answer": "Policy allows 20 days leave.",
                "citations": [],
                "status": "succeeded",
            },
        )

        final_state = orchestrator_graph.run_orchestrated(
            "compare leave taken with policy", None, True, caller_roles=("admin",)
        )

        assert set(final_state["sources_used"]) == {"sql", "policy"}
        assert final_state["policy_result"]["answer"] == "Policy allows 20 days leave."
        assert "Policy allows 20 days leave." in final_state["synthesized_answer"]
        assert "Database" in final_state["synthesized_answer"]

    def test_flag_off_with_attachments_still_runs_the_orchestrator_graph(self, monkeypatch):
        """Attaching a file is a per-request opt-in the caller makes
        explicitly -- it must not require also enabling the unrelated
        multi-source router feature (see run_orchestrated's own docstring)."""
        settings = _settings(enable_multi_source_router=False)
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: settings)
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: settings)

        def _should_not_be_called(*args, **kwargs):
            raise AssertionError(
                "run_agent must not be called directly when attachments are present"
            )

        monkeypatch.setattr(orchestrator_graph, "run_agent", _should_not_be_called)
        # attachment_node imports run_attachment_qa locally (from
        # attachments.graph import run_attachment_qa) at call time, not at
        # agent.orchestrator.nodes's own module scope -- patch it at its
        # real source location.
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

    def test_no_attachments_and_flag_off_is_still_the_pure_pass_through(self, monkeypatch):
        monkeypatch.setattr(orchestrator_graph, "get_settings", lambda: _settings())
        fake_result: AgentState = {"status": "succeeded", "sql": "SELECT 1"}
        monkeypatch.setattr(
            orchestrator_graph,
            "run_agent",
            lambda question, conversation_history, enable_insight, caller_roles, model=None, tenant_id=None, forecast_horizon=None: fake_result,
        )
        result = orchestrator_graph.run_orchestrated("how many orders?", attachment_ids=None)
        assert result is fake_result


class TestAttachmentNode:
    def test_calls_run_attachment_qa_and_shapes_the_result(self, monkeypatch):
        import attachments.graph as nodes_module

        monkeypatch.setattr(
            nodes_module,
            "run_attachment_qa",
            lambda question, ids, settings=None, owner_subject=None: {
                "status": "succeeded",
                "answer": "Revenue was 42000, per report.txt.",
                "used_attachment_ids": ["att_1"],
                "vision_unavailable": False,
            },
        )
        result = attachment_node(
            {"question": "what's the revenue?", "pending_attachment_ids": ["att_1"]}
        )
        assert result["sources_used"] == ["attachments"]
        assert result["attachment_result"]["status"] == "succeeded"
        assert result["attachment_result"]["used_attachment_ids"] == ["att_1"]

    def test_no_attachments_status_maps_to_insufficient_information(self, monkeypatch):
        import attachments.graph as nodes_module

        monkeypatch.setattr(
            nodes_module,
            "run_attachment_qa",
            lambda question, ids, settings=None, owner_subject=None: {
                "status": "no_attachments",
                "answer": None,
                "used_attachment_ids": [],
                "vision_unavailable": False,
            },
        )
        result = attachment_node({"question": "hi", "pending_attachment_ids": []})
        assert result["attachment_result"]["status"] == "insufficient_information"

    def test_never_raises_when_run_attachment_qa_fails(self, monkeypatch):
        import attachments.graph as nodes_module

        monkeypatch.setattr(
            nodes_module,
            "run_attachment_qa",
            lambda question, ids, settings=None, owner_subject=None: {
                "status": "failed",
                "answer": None,
                "used_attachment_ids": [],
                "vision_unavailable": False,
            },
        )
        result = attachment_node({"question": "hi", "pending_attachment_ids": ["att_1"]})
        assert result["attachment_result"]["status"] == "failed"
        assert result["attachment_result"]["answer"]  # a fallback message, never empty/None
