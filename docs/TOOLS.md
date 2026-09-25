# Tool / MCP Abstraction (`agent/tools/`)

## What this is

A generic, governed way to describe and invoke this application's real
capabilities — SQL, document RAG, policy RAG, web search, media search,
media generation — by name, with a uniform contract: name, description,
category (READ/WRITE), required permission, timeout, retry policy, and
structured audit logging on every call.

This is the first increment of a broader roadmap (see
[`PLATFORM_TRANSFORMATION_ASSESSMENT.md`](PLATFORM_TRANSFORMATION_ASSESSMENT.md))
toward evolving this project into a multi-source "Enterprise AI
Intelligence Platform." It was scoped and built as its own, independently
useful deliverable — not a partial feature — but it is intentionally the
*foundation* for later steps (a research-planner upgrade, a future
external MCP client), not a consumer-facing feature on its own.

## What this is not

**Not a rewrite of `agent/orchestrator/nodes.py`.** That module's five
hardcoded LangGraph nodes (`sql_subgraph_node`, `document_rag_node`,
`policy_rag_node`, `web_search_node`, `generation_node`, `media_search_node`)
are completely unmodified. `agent.orchestrator.graph.run_orchestrated`
still calls them directly in production, exactly as before this package
existed. Every `Tool` in `agent/tools/definitions.py` wraps the *same*
underlying function those nodes already call — there is no duplicated
retrieval/generation/validation logic anywhere in this package.

## Why the orchestrator wasn't rewired to use it (yet)

`agent/orchestrator/nodes.py` is a ~1,150-line, heavily security-reviewed
module: it already does its own permission filtering
(`router_node`'s `SOURCE_PERMISSIONS` check), its own session-level
expensive-source cost ceiling, its own human-approval gate for media
generation, and its own carefully fail-open/fail-closed handling per
source. Swapping those node bodies to call through `ToolRegistry.execute`
would either duplicate that logic (permission-checked twice, in two
different ways, with real drift risk) or require carefully threading the
registry's own checks through code that already has its own — a
non-trivial, real-regression-risk change to a working, tested, audited
code path, for no immediate behavioral benefit (nothing today needs to
choose a tool *dynamically*; the orchestrator's routing is a fixed graph
edge per source). Per this project's own engineering principle ("choose
the smallest safe architectural change"), that integration is deferred to
the next roadmap step, where it becomes load-bearing: a research planner
that needs to select and invoke tools dynamically by name is the natural,
first real consumer.

## Package layout

- `agent/tools/types.py` — `Tool` (name, description, category, handler,
  permission, timeout, retry policy, optional input/output schema),
  `ToolCategory` (`READ`/`WRITE`), `RetryPolicy`, `ToolResult`, and the
  exception hierarchy (`ToolError` → `ToolNotFoundError`,
  `ToolPermissionError`, `ToolTimeoutError`).
- `agent/tools/registry.py` — `ToolRegistry`: `register`/`get`/`list_tools`,
  and the governed `execute(name, input_data, *, caller_roles, actor)`.
- `agent/tools/definitions.py` — `build_default_registry()`, registering
  the six real tools.

## The six tools

| Tool | Category | Permission | Wraps |
|---|---|---|---|
| `sql_query` | READ | `EXECUTE_SQL` | `agent.graph.run_agent` |
| `document_search` | READ | `DOCUMENTS_READ` | `rag.graph.run_rag(collection="documents")` |
| `policy_search` | READ | `POLICY_RAG_QUERY` | `rag.graph.run_rag(collection="policies")` |
| `web_search` | READ | `WEB_SEARCH` | `search.web_search.web_search` |
| `media_search` | READ | `MEDIA_SEARCH` | `media.search.search_media` |
| `media_generation` | **WRITE** | `MEDIA_GENERATE` | `agent.orchestrator.nodes.execute_generation` |

`media_generation` is the only WRITE tool — it spends real, metered
provider credit. The `ToolCategory.WRITE` tag itself grants no extra
authorization; the actual human-approval gate
(`Settings.require_generation_approval`) still lives entirely in
`generation_node`/`execute_generation`, upstream of and unrelated to this
package.

## What `ToolRegistry.execute` enforces, uniformly

1. **Permission check** (fail-closed) — `agent.authz.has_role_permission`,
   the exact same check `router_node` already performs for its own
   sources. Raises `ToolPermissionError`, logs a `tool_permission_denied`
   audit event.
2. **Hard per-attempt timeout** — thread-based (daemon thread + `join`),
   mirroring `db.execution._execute_with_timeout`'s own pattern, the only
   portable way to bound an arbitrary Python call's wall-clock time on
   Windows. A backstop, not the only layer — each wrapped function already
   has its own inner timeouts (DB query timeout, HTTP client timeout).
3. **Retry policy** — `max_attempts=1` by default for every registered
   tool; most of the wrapped functions already have their own internal
   retry/self-correction loop, so an outer retry would only add latency.
4. **Audit logging** — one `security.audit_log.log_security_event` call
   per outcome: `tool_permission_denied`, `tool_executed`, or
   `tool_execution_failed`.
5. **Never raises a handler's own exception** — a failed tool call returns
   a `ToolResult(success=False, error=...)`, never crashes the caller,
   matching this codebase's "a source failure must not crash the whole
   run" posture.

## Testing

- `tests/test_tools_registry.py` — the registry in isolation (fake
  handlers): registration, permission enforcement, timeout, retry,
  audit-log call sites.
- `tests/test_tools_definitions.py` — confirms `build_default_registry()`
  is wired to the real underlying functions by monkeypatching each one and
  asserting the tool called through with the right arguments — not a
  reimplementation.

27 tests, all passing; the full existing suite (1,406 tests before this
change) passes unmodified alongside them.

## Known limitations (disclosed, not hidden)

- **Per-tool timeouts are hand-picked constants**
  (`agent/tools/definitions.py`), not yet wired to
  `config.settings.Settings`. Kept out of this increment to keep its
  surface area small (no changes to the security-reviewed `Settings`
  class). A legitimate follow-up once a real consumer needs to tune them.
- **No live consumer yet.** This package is fully functional and tested,
  but nothing in the production request path calls it today — see "Why
  the orchestrator wasn't rewired" above for why that's a deliberate,
  reasoned choice for this increment rather than an oversight.
- **No `input_schema`/`output_schema` validation is actually exercised.**
  `Tool` supports optional schema classes for a future MCP-client
  consumer, but none of the six registered tools set one — each already
  has a well-typed underlying function signature, so dict-shape validation
  would be redundant today.
