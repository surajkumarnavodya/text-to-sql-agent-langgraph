# Changelog

Newest first. The README's "News and Updates" section shows only the three most
recent entries. This file keeps the full record. Each entry names the contract
document that holds the design, the API, the configuration and the limitations.

## 2026-10-06: AI analysis layers (Prompts 33-35)

All three layers are optional and off by default. None of them changes `/ask`.

### Added

- **AI Data Analyst** (`POST /analyst/investigate`, flag `ENABLE_DATA_ANALYST_AGENT`).
  A bounded, multi-step LangGraph loop that splits a business question, runs each
  part through the governed SQL pipeline, follows anomalies with fixed-template
  follow-ups, and returns an evidence-linked report. Budgets for steps,
  sub-questions, LLM calls, follow-ups and time are hard server-side stops.
  Contract: [`33_AI_DATA_ANALYST_AGENT_CONTRACT.md`](33_AI_DATA_ANALYST_AGENT_CONTRACT.md).
- **Multi-agent supervisor** (`POST /analyst/supervise`, flag `ENABLE_MULTI_AGENT_SUPERVISOR`).
  A deterministic supervisor over eight typed specialists. Each specialist has
  its own tool allowlist and truth-level ceiling. The tool gateway refuses
  identity fields in tool input. Circuit breakers are scoped per tenant. Claims
  must cite evidence the supervisor issued. Conflicts are resolved by a fixed rule.
  A benchmark (`eval/multiagent_benchmark.py`) confirms the supervisor makes the
  same SQL and planner calls as the analyst.
  Contract: [`34_MULTI_AGENT_SUPERVISOR_CONTRACT.md`](34_MULTI_AGENT_SUPERVISOR_CONTRACT.md).
- **Semantic intelligence** (`/semantic-intelligence/*`, flag `ENABLE_SEMANTIC_INTELLIGENCE`).
  Deterministic detection of synonym candidates, term clusters, ambiguous terms,
  conflicting metric definitions, candidate business rules, metric relationships
  and change impact over the governed catalog. Findings are versioned, carry an
  owner, and are ordered for review by risk rather than volume. Finding rollback
  and catalog-version rollback both create new versions and never rewrite history.
  Contract: [`35_SEMANTIC_INTELLIGENCE_CONTRACT.md`](35_SEMANTIC_INTELLIGENCE_CONTRACT.md).
- Database migration `a1f5c9e3b7d2` (additive): the `semantic_findings` table.
- Settings: 14 new variables, all documented in `docs/CONFIGURATION.md` and `.env.example`.

### Changed

- `rag/llm.py`: `call_ollama` accepts an optional `model` override. The default
  is unchanged for every existing caller.

### Security

- Every new route requires an authenticated local account with the relevant
  permission. Cross-tenant resources return 404, not 403.
- The analyst and the supervisor execute SQL only through the existing governed
  pipeline, under the caller's own roles.
- Findings and claims are always labelled `ai_inference` unless they come from a
  published catalog entry or an observed database value. Nothing inferred is
  presented as fact.
- Known, not fixed: `ToolRegistry.execute` lets tool input override
  `caller_roles`. The new supervisor gateway blocks this for everything it runs,
  but the shared registry still allows it. See the Prompt 34 contract's risk section.

### Verification

- Backend suite: 3,993 tests passing on the final code, including `tests/security/`.
- The three layers were verified with mocked LLM and SQL boundaries and with
  synthetic catalog data. They have not been run against a live model or a real
  tenant's catalog. See each contract's limitations section.
