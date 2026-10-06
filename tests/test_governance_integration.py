"""Governance wired into the live pipeline: request refusal, router containment,
result masking at execution, and the rollback switches."""

from __future__ import annotations

from governance.result_policy import RESTRICTED_MASK

from agent.nodes import execute_sql_node, sanitize_input_node
from agent.orchestrator import nodes as orchestrator_nodes
from agent.state import AgentState
from config.settings import Settings
from security.secrets import SecretStr


def _settings(**overrides: object) -> Settings:
    """A PostgreSQL-configured Settings (execution resolves a real connection config),
    with any field overridden per test. Mirrors tests/test_agent_nodes.py's fixture."""
    base: dict[str, object] = dict(
        db_type="postgresql",
        db_host="db.example.com",
        db_name="mydb",
        db_user="reader",
        db_password=SecretStr("secret"),
    )
    base.update(overrides)
    return Settings(**base)


class TestRequestRefusalAtSanitize:
    def test_viewer_asking_for_an_individual_salary_is_refused_before_any_source_runs(self):
        state: AgentState = {
            "question": "What is the salary of employee 1042?",
            "caller_roles": ("viewer",),
        }
        result = sanitize_input_node(state)
        assert result["status"] == "rejected"
        assert result["rejection_reason"] == "policy_refused"
        assert "Salary information is restricted" in result["rejection_message"]
        assert "question" not in result  # the question is never passed on

    def test_analyst_with_restricted_access_passes_the_same_question_through(self):
        state: AgentState = {
            "question": "What is the salary of employee 1042?",
            "caller_roles": ("analyst",),
        }
        result = sanitize_input_node(state)
        assert result["status"] == "classifying_followup"

    def test_ordinary_aggregate_question_passes_for_a_viewer(self):
        state: AgentState = {
            "question": "Average salary by department",
            "caller_roles": ("viewer",),
        }
        assert sanitize_input_node(state)["status"] == "classifying_followup"

    def test_request_governance_can_be_switched_off_for_rollback(self, monkeypatch):
        monkeypatch.setattr(
            "agent.nodes.get_settings", lambda: _settings(enable_request_governance=False)
        )
        state: AgentState = {
            "question": "What is the salary of employee 1042?",
            "caller_roles": ("viewer",),
        }
        assert sanitize_input_node(state)["status"] == "classifying_followup"


class TestRouterContainsRefusedRequests:
    def _route(self, monkeypatch, question: str, roles: tuple[str, ...]) -> dict:
        monkeypatch.setattr(orchestrator_nodes, "get_settings", lambda: _settings())
        monkeypatch.setattr(
            orchestrator_nodes,
            "get_available_sources",
            lambda settings, has_attachments: ["sql", "web"],
        )
        monkeypatch.setattr(
            orchestrator_nodes,
            "classify_sources",
            lambda question, available, settings: (["web"], "classifier picked web"),
        )
        state = {"question": question, "caller_roles": roles, "pending_attachment_ids": []}
        return orchestrator_nodes.router_node(state)["route_decision"]

    def test_refused_question_is_routed_to_sql_alone_so_no_other_source_answers_it(
        self, monkeypatch
    ):
        decision = self._route(
            monkeypatch, "Which medicine should this patient take?", ("analyst",)
        )
        assert decision["sources"] == ["sql"]
        assert "data-governance refusal" in decision["reasoning"]

    def test_permitted_question_keeps_the_routers_own_choice(self, monkeypatch):
        decision = self._route(monkeypatch, "What is the weather like in Pune?", ("analyst",))
        assert decision["sources"] == ["web"]


class TestResultMaskingAtExecution:
    def _execute(self, monkeypatch, roles: tuple[str, ...], **settings_overrides: object) -> list:
        monkeypatch.setattr("agent.nodes.get_settings", lambda: _settings(**settings_overrides))
        monkeypatch.setattr(
            "agent.nodes.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (
                ["department", "salary"],
                [("Sales", 95000)],
            ),
        )
        monkeypatch.setattr(
            "governance.result_policy.load_sensitive_columns",
            lambda: {("hr_employee", "salary"): "restricted"},
        )
        state: AgentState = {
            "sql": "SELECT department, salary FROM hr_employee",
            "retry_count": 0,
            "selected_database": "default",
            "caller_roles": roles,
        }
        return execute_sql_node(state)["result_rows"]

    def test_restricted_value_never_reaches_state_for_a_viewer(self, monkeypatch):
        rows = self._execute(monkeypatch, ("viewer",))
        assert rows == [("Sales", RESTRICTED_MASK)]

    def test_restricted_value_is_visible_to_a_caller_with_permission(self, monkeypatch):
        rows = self._execute(monkeypatch, ("analyst",))
        assert rows == [("Sales", 95000)]

    def test_result_governance_can_be_switched_off_for_rollback(self, monkeypatch):
        rows = self._execute(monkeypatch, ("viewer",), enable_result_governance=False)
        assert rows == [("Sales", 95000)]
