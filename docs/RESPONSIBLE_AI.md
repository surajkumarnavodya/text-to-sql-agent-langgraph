# Responsible AI

How this project's actual design choices — not aspirations — reflect
responsible-AI principles. Every claim below cites the code or the eval
artifact it's backed by; where a principle isn't fully met, that's said
plainly rather than smoothed over. See `docs/COMPLIANCE.md` for how these
map against named external frameworks, and `docs/RISK_REGISTER.md` for the
open gaps.

## Transparency

- **The generated SQL is always shown, never hidden behind a summarized
  answer.** `ui/app.py` renders the SQL text in an editable box before any
  result is shown; `api/main.py`'s `/ask` response includes `sql` alongside
  `result_rows`. A user (or API caller) can always see exactly what query
  produced an answer.
- **The retry/self-correction process is visible, not black-boxed.** Every
  attempt — successful or not — is recorded in `attempt_history` and
  rendered as a "Retry timeline" in the UI (`ui/app.py::_render_attempt_timeline`).
  This was a deliberate architectural choice (`docs/ARCHITECTURE.md`'s "§1
  The LangGraph state machine": "a small, explicit `StateGraph`... not a
  free-form ReAct-style agent") specifically so this trail is inspectable.
- **Real accuracy numbers are published, including the unflattering ones.**
  `docs/EVALUATION.md` reports the actual latest benchmark run (35% final
  accuracy) rather than only qualitative feature-list claims. A reader
  deciding whether to trust this tool's output has the real number, not a
  marketing gloss.
- **Rejections don't over-explain to a potential attacker, but they're
  never silent either.** A rejected question gets a calm, standardized
  message (`agent/nodes.py`, `agent/input_guard.py`) — enough for a
  legitimate user to understand something didn't work, not enough detail to
  hand an adversary a map of exactly what pattern tripped it.

## Human oversight

- **Nothing executes without a human-visible intermediate step.** The
  agent's own internal retry-loop executions are real but never shown; the
  UI's "Confirm and Run" button is the actual gate before a human sees a
  result, and it re-validates whatever SQL text is currently in the box —
  including hand-edits — rather than trusting the agent's last internal
  attempt (`CLAUDE.md`'s "SQL is untrusted output, always").
- **The API path preserves the same property structurally**, even though
  there's no UI confirmation step for a programmatic caller: the caller
  receives the full state (SQL, results, retry history) and decides what
  to do with it — the API never takes an action beyond returning data from
  a read-only, validated query.

## Fairness and limitations

- **Accuracy is not uniform across question types**, and that's
  documented, not hidden: `docs/EVALUATION.md`'s per-category breakdown
  shows `window_function_correctness` and
  `ambiguous_question_handling_accuracy` both measured at 0.0% in the
  latest run, next to `security_rejection_accuracy` at 100%. A user should
  trust this tool more for straightforward aggregation/filter questions
  than for ambiguous or window-function-heavy ones — `README.md`'s "Known
  limitations" already said this qualitatively; `docs/EVALUATION.md` now
  backs it with numbers.
- **Model dependence is disclosed.** `llama3.1:8b` is one model choice
  among several the architecture supports (`OLLAMA_MODEL` is config-driven,
  not hardcoded); accuracy is a property of the configured model, not a
  fixed property of this codebase.
- **No claim of correctness beyond what's measured.** Nothing in this
  project asserts SQL generation is "accurate" or "reliable" as an
  unqualified claim — every accuracy statement in the documentation traces
  back to a specific benchmark run with a specific model against a
  specific database.

## Data minimization and privacy

- **Fully local by design.** Ollama runs on the user's own machine; no
  question, schema, or result data leaves it for LLM inference (`README.md`'s
  "why I built this"). No hosted-API key, no third-party data processor in
  the LLM path.
- **Schema retrieval is scoped, not exhaustive.** Only the top-k relevant
  tables are ever put in a prompt (`SCHEMA_TOP_K`), not the whole schema —
  originally a context-budget decision (`docs/ARCHITECTURE.md`'s "§3
  Schema-retrieval pipeline"), but it also means less of the schema's
  metadata is ever exposed to the model than would be with a naive
  dump-everything approach.
- **Value sampling is privacy-aware by construction, not by a denylist.**
  `db/value_sampling.py`'s cardinality cap (max 20 distinct values) means
  high-cardinality columns — the ones most likely to be names, emails, or
  other PII — structurally never qualify for sampling into a prompt,
  without needing to enumerate every possible PII column name in advance.
  This is a side effect of a different design goal (disambiguating coded
  columns), documented honestly as such, not sold as a purpose-built
  privacy control.
- **An explicit, if currently unpopulated, sensitivity-classification
  layer exists on top of that** (`config/sensitive_columns.yaml`,
  `docs/GOVERNANCE.md`'s "Data classification policy") for the columns the
  cardinality heuristic alone wouldn't catch (a small, closed set of
  sensitive categories, say). See `docs/RISK_REGISTER.md`'s R-002 for the
  honest state of that layer today: real code, zero columns classified
  until a human does it per deployment.

## Untrusted input, treated consistently

Both what a user types and what the database itself contains are treated
as adversarial input, at the same trust level, not just the former:

- **Typed questions** pass `agent/input_guard.py` before anything else.
- **Retrieved schema and sampled values** are normalized
  (`security/sanitization.py`) and scanned (detection-only) for
  injection-shaped content before ever reaching a prompt — because
  `SECURITY.md`'s own reasoning is direct about this: "anyone who can write
  to the underlying database can write text that ends up inside an LLM
  prompt." Treating only the chat box as adversarial and the database as
  trusted would have been an incomplete threat model.

## What this project does not claim

- Not fairness-audited across demographic groups — this is a Text-to-SQL
  tool over structured business data, not a system making decisions about
  people, so that axis of responsible-AI assessment is largely inapplicable
  to what it does, but is named here rather than silently skipped.
- Not independently red-teamed — see `docs/RISK_REGISTER.md`'s R-005 and
  `SECURITY.md`'s own disclosure.
- Not guaranteed safe for any database — the read-only DB role requirement
  (`SECURITY.md`) is load-bearing, not optional, and this project cannot
  enforce it from inside the app.

## Cross-references

- [`../SECURITY.md`](../SECURITY.md) — technical controls.
- [`COMPLIANCE.md`](COMPLIANCE.md) — external framework mapping.
- [`GOVERNANCE.md`](GOVERNANCE.md) — ownership and process.
- [`RISK_REGISTER.md`](RISK_REGISTER.md) — open risks named above, tracked.
- [`EVALUATION.md`](EVALUATION.md) — the actual accuracy numbers referenced
  throughout this document.
