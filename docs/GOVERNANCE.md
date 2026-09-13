# Governance

This document states, explicitly and in writing, who is responsible for
this project's data-handling and security decisions, how sensitive
material is classified, how security-relevant changes are controlled, how
deviations from a control are handled, and how often any of this actually
gets looked at again. It exists because "the author thought about it
carefully" (SECURITY.md's own phrase for this project's security posture)
is easier to trust when the thinking is written down and dated, not just
asserted.

This is a solo-maintainer project. Everything below is scoped to what's
proportionate at that scale — a documented discipline one person actually
follows, not a simulation of an enterprise governance function. See
[`COMPLIANCE.md`](COMPLIANCE.md) for how this maps against named external
frameworks, and [`RISK_REGISTER.md`](RISK_REGISTER.md) for the living list
of what this governance process is tracking.

## Ownership

**Suraj Kumar is the sole maintainer and is solely responsible for this
project's data-handling and security decisions** — what gets connected to,
what's classified as sensitive, what SQL is allowed to run, what rate and
cost limits are set, and whether an exception to any of those is granted.
There is no separate security team, review board, or second approver.
That's a real limitation, not a formality — it's stated here so it's
never ambiguous who made a given call, and so anyone evaluating this
project's risk posture knows exactly what "reviewed" means in this
context: reviewed by one person, not independently verified by another
(see SECURITY.md's "has not been through an independent security review").

## Data classification policy

**Current status (as of 2026-09-11): implemented as an enforced control.**
`config/sensitive_columns.yaml` (loaded by `config/sensitive_columns.py`)
exists and is wired into two enforcement points: `db/value_sampling.py`
omits a column classified "restricted" from the schema text shown to the
LLM entirely (name and type, not just its sampled values — see "What
exists today" below for what changed 2026-09-11), and
`agent/nodes.py::validate_sql_node` rejects (retryable) generated SQL that
directly selects a "restricted" column. The file ships empty — the
mechanism exists, but no column has been reviewed and classified yet, so
this has no effect on any real question until that happens. See
`tests/test_sensitive_columns.py` and `tests/test_nodes_security_wiring.py`
for the enforcement's regression coverage.

### The three tiers

Any column exposed to the LLM or rendered in the UI is intended to fall
into exactly one of three tiers:

- **Public** — safe to show in generated SQL, query results, sampled
  values in the schema prompt, and logs without restriction (e.g. product
  category names, calendar attributes).
- **Internal** — fine for this app's normal operation (a local,
  single-user tool against a database the user already has full read
  access to) but not the kind of thing that should be casually sampled
  into an LLM prompt or logged verbatim if it can be avoided (e.g. a
  reseller's business name, a free-text order comment).
- **Restricted** — data that should never be sampled into a prompt and
  never appear in a log line, and that a future multi-user deployment
  would need to gate behind real authorization rather than app-wide access
  (e.g. customer PII — name, email, phone — if this were ever pointed at a
  schema that carried it in the clear).

### What exists today

`config/sensitive_columns.yaml` + `config/sensitive_columns.py`, mirroring
`config/table_descriptions.yaml`'s own pattern exactly: hand-authored,
read fresh on every call (no caching, so a hand-edit takes effect on the
very next question, no rebuild step), deliberately incomplete until a
human reviews and classifies each column that matters for the connected
database. Two independent enforcement points read it:

- `db/value_sampling.py::attach_sample_values` never samples a "restricted"
  column, regardless of how well it would otherwise qualify by the
  existing 20-distinct-value cardinality cap (see `CLAUDE.md`) — that cap
  is a side effect of a heuristic built for a different reason
  (disambiguating coded columns like `ProductLine`), not a deliberate
  sensitivity control, and provides no protection on its own for a
  low-cardinality sensitive column (e.g. a small, closed set of medical or
  demographic categories). **Since 2026-09-11**, the column is omitted
  from the rendered schema text entirely, not just its sample values — its
  name and type never reach the LLM (or the Chroma-embedded schema chunk)
  at all, and a foreign key referencing it is dropped from the rendered
  `FOREIGN KEY (...)` line the same way, so the name can't leak back in
  that path either. This is layered on top of, not instead of, the
  post-generation block below — a model can still ask about (or a user can
  still request) a plausibly-named column that isn't in the schema shown
  to it, so the actual, tested gate remains the one that matters.
  `TableSchemaInfo.columns`/`.foreign_keys` still reflect the real,
  complete schema on the object returned by `attach_sample_values` (needed
  by the validator gate below, which must know a restricted column exists
  in order to block it) — only the rendered `.ddl` text omits it.
- `agent/nodes.py::validate_sql_node` rejects (retryable, not a hard
  safety-violation failure — the model can drop the column and answer
  with what remains) any validated SQL that directly selects a
  "restricted" column, via
  `agent.sql_validator.find_restricted_column_references`.

`config/table_descriptions.yaml` still documents table/column *meaning*,
not sensitivity — it remains a separate file with a separate purpose, not
read as a classification source.

A restricted column's *values*, once a query is allowed to select it, are
not separately redacted in the results table — the block happens at
query-generation time, not as output filtering. Nothing here is
authorization-aware (there's no per-user identity in this app's
single-user model to authorize against) — see "Restricted" tier's own note
below on what a future multi-user deployment would still need to add.

**Extended to policy documents (2026-09-10, optional feature, off by
default):** the same three-tier philosophy — hand-authored classification,
enforced fail-closed, not authorization-aware — now also covers uploaded
policy PDFs, a different data shape (a whole document/chunk, not a
`(table, column)` pair). `rag/store.py`'s `SensitivityCategory` implements
three specific "restricted"-equivalent categories, reviewed and signed off
on for this project: **compensation & pay**, **disciplinary/HR case
content**, and **legal/litigation**. A chunk tagged with any of these is
never summarized into an answer — see `SECURITY.md`'s multi-source section
and `CLAUDE.md`'s "Document/policy agentic RAG" for the mechanism. Same
caveat as the column-level "Restricted" tier: this blocks everyone
equally, it is not per-user access control.

**Not yet extended to the media search library (2026-09-13, optional
feature, off by default):** unlike policy documents above, media search
(`media/`, `ENABLE_MEDIA_SEARCH`) has **no equivalent classification
mechanism at all** for what it indexes (OCR text, ASR transcripts,
generated captions, the images/keyframes themselves) — this is a real,
disclosed gap, not an oversight left undocumented, tracked as
`docs/RISK_REGISTER.md`'s R-012. Treat anything placed in the configured
`MEDIA_LIBRARY_PATH` as visible to anyone who can use this app until a
tagging/gating mechanism for this data shape is built.

### The policy tiers, as enforced

- Every table/column's classification is recorded in that file, not in
  code, comments, or tribal knowledge — the same "config, not scattered
  hardcoding" principle `CLAUDE.md` already applies to model names, paths,
  and connection details.
- **Only the maintainer (currently: Suraj Kumar) may change a column's
  classification tier.** This is a single-approver project; "only the
  maintainer" is the whole access-control model, stated explicitly rather
  than left implicit.
- **No silent edits.** Any change to a column's classification — tightening
  or loosening — must get a dated entry in
  [`security-changelog.md`](security-changelog.md): what changed, from
  which tier to which, and why. A classification change is exactly the
  kind of security-relevant change that changelog exists for (see below).
- Loosening a classification (restricted → internal, or internal → public)
  is treated as a bigger deal than tightening it, and should generally go
  through the exception process below rather than a direct edit, unless
  it's a permanent, considered policy change rather than a temporary need.

## Change control for security-relevant changes

Any change to one of the following is deliberate and documented, not an
incidental side effect of an unrelated commit:

- The SQL validator's allowlist (`agent/sql_validator.py`) — what
  statement shapes are permitted to execute, including the
  `_DISALLOWED_NESTED_TYPES` full-tree write/DDL check and the
  `_DANGEROUS_FUNCTION_NAMES` denylist added 2026-09-01.
- The sensitivity/classification config (`config/sensitive_columns.yaml`
  — see above).
- Rate limits (`QUESTION_RATE_LIMIT_PER_MINUTE`,
  `LLM_CALL_RATE_LIMIT_PER_MINUTE`, `API_ACTION_RATE_LIMIT_PER_MINUTE`,
  `MEDIA_GEN_RATE_LIMIT`/`_WINDOW_SECONDS`,
  `SESSION_EXPENSIVE_SOURCE_LIMIT`/`_WINDOW_SECONDS` — `agent/rate_limit.py`,
  `api/rate_limit.py`, `config/settings.py`).
- Cost-estimation thresholds (`COST_MODERATE_ROW_THRESHOLD`,
  `COST_HIGH_ROW_THRESHOLD`, `COST_ESTIMATION_ENABLED` —
  `db/query_cost.py`, `config/settings.py`).
- The media-generation content-policy check
  (`agent.orchestrator.nodes._DISALLOWED_PROMPT_SUBSTRINGS`) and the
  human-approval gate in front of it (`REQUIRE_GENERATION_APPROVAL` —
  added 2026-09-13, since generation is the one orchestrator source that
  spends real, metered money).

Every such change gets a dated entry in
[`security-changelog.md`](security-changelog.md), separate from ordinary
feature/commit history, so a reviewer (including future-me) can answer
"when and why did the high-cost threshold change" without reconstructing
it from `git log` across unrelated commits. A changelog entry should name:
the date, what changed (old value → new value, or old rule → new rule),
why, and whether it's permanent or tied to an exception (see below).

This is a log, not a gate — nothing here technically blocks a change from
shipping without an entry. The discipline is "I write the entry before I
consider the change done," the same way tests or lint are part of
"done" per `CONTRIBUTING.md`, not a separate optional step.

## Exception process

Sometimes a control needs to be knowingly, temporarily relaxed — raising a
rate limit for a demo, temporarily reclassifying a restricted column for
testing once classification exists, disabling cost estimation to debug an
unrelated issue. That's a legitimate need, not a violation, **provided
it's explicit, logged, and time-boxed** rather than a silent, permanent
drift away from the documented control.

The process, appropriate for a single-maintainer project (a documented
discipline, not automated enforcement):

1. **State it before making the change.** Write down which control is
   being relaxed and why.
2. **Log it as an accepted exception** in
   [`RISK_REGISTER.md`](RISK_REGISTER.md)'s "Accepted exceptions" section:
   what was relaxed, the reason, the expiry/review date, and the severity
   of leaving it relaxed past that date.
3. **Also log it in `security-changelog.md`** if the exception involves one
   of the change-controlled items above (it usually will) — the changelog
   captures *what changed*, the risk register captures *that it's a
   temporary, tracked deviation and when it gets revisited*. The two
   entries should cross-reference each other by date.
4. **Revert or re-review by the stated date.** On the review date, either
   the control is restored, or the exception is explicitly renewed with a
   new date and a restated reason — it never just quietly persists past
   its own expiry unnoticed.

An exception with no expiry date, or one that's been silently extended
past its review date without a new entry, should be treated as a control
that's effectively been abandoned — surface it, don't let it sit.

## Review cadence

Proportionate to a solo-maintainer project actively being developed, not
an enterprise SOC schedule:

- **Re-run the benchmark** (`python scripts/run_benchmark.py
  --check-regression`, comparing against the stored baseline at
  `eval/baselines/latest.json`; exits non-zero if accuracy, retrieval,
  security-rejection, or latency/cost regressed beyond tolerance — see
  `eval/regression.py`) **after any change to the system prompt,
  `agent/input_guard.py`'s patterns, a config default, or the Ollama
  model** — this is the accuracy + adversarial regression check, and it's
  cheap enough that "after every relevant
  change" is the realistic bar, not "periodically."
- **Review `scripts/monitoring_summary.py`'s output weekly** during active
  development (and before/after any change to rate limits, cost
  thresholds, or the input-guard patterns specifically) — see
  [`COMPLIANCE.md`](COMPLIANCE.md) for how this maps to ongoing-monitoring
  expectations. Outside active development, "whenever you next sit down to
  work on this" is a realistic replacement for a fixed weekly cadence — the
  point is that it happens on some cadence, not that it happens on a
  calendar a solo project has no real reason to enforce on itself.
- **Review `RISK_REGISTER.md`'s open items and accepted exceptions** at the
  same weekly-during-active-development cadence — an accepted exception
  past its review date is the specific thing this cadence exists to catch.
- **Revisit this document itself** whenever a new security-relevant
  mechanism is added (e.g. if `sensitive_columns.yaml` enforcement is ever
  built) — the classification-policy section above should flip from
  "not yet implemented" to describing the real mechanism at that point,
  not be left stale.

## Cross-references

This document is one of five that together describe this project's risk
posture — each links back to the others so a reader can start from any one
of them:

- [`SECURITY.md`](../SECURITY.md) — the technical security controls
  themselves (what's enforced, what isn't guaranteed).
- [`COMPLIANCE.md`](COMPLIANCE.md) — self-assessment against named
  external frameworks (NIST AI RMF, ISO/IEC 42001, OWASP, EU AI Act).
- [`RESPONSIBLE_AI.md`](RESPONSIBLE_AI.md) — how design choices reflect
  responsible-AI principles.
- [`RISK_REGISTER.md`](RISK_REGISTER.md) — the living list of known risks,
  their status, and accepted exceptions.
- [`security-changelog.md`](security-changelog.md) — the dated log of
  security-relevant changes this document's change-control section
  requires.
