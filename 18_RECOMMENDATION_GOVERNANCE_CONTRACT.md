# 18 — Recommendation Governance & Feedback

Prompt 18 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`17_RECOMMENDATION_ENGINE_CONTRACT.md`. Two new pure modules
(`recommendation/governance.py`, `recommendation/governance_policy.py`),
two new identity-DB tables (`RecommendationRecord`/
`RecommendationFeedbackEvent`, migration `f3a6b9c2d8e1`), a new
repository (`identity/repositories/recommendation_governance.py`), a new
REST surface (`api/recommendation_governance.py`), and additive
`/ask`-pipeline wiring (`api/recommendation_persistence.py`) that
persists Prompt 17's already-computed recommendations for a locally-
authenticated caller. No existing route, node, or response field changed
shape; `AskResponse` is unchanged, and every existing caller of
`recommendation.models.Recommendation` keeps working unmodified.

## 1. Inspection: what already existed vs. the real gap

Prompt 17's `recommendation.engine.generate_recommendations` is a pure,
per-request function — its output (`AskResponse.recommendations`) is
computed, validated, and returned, then gone; nothing in this codebase
persisted a recommendation anywhere, so there was no way for a human to
give feedback on *a specific recommendation instance* across requests,
nor any way to measure recommendation quality over time. The closest
existing precedents, inspected directly:

- **`semantic/catalog.py` + `catalog_policy.py` + `identity/repositories
  /semantic_catalog.py` + `api/semantic_catalog.py`** — the exact
  "pure typed lifecycle vocabulary + pure RBAC/ABAC policy + repository
  that enforces transitions in defense-in-depth + REST surface that does
  one authorization dance per route" shape this prompt reuses wholesale.
  Its `draft → reviewed → published → superseded` workflow gates whether
  content is *live*; this prompt's own eight-status lifecycle gates
  nothing about visibility (Prompt 17 already decided that) — it exists
  purely to measure quality after the fact, a materially different kind
  of lifecycle layered on the identical mechanics.
- **`identity.models.OnboardingReviewItem`** — a decision/decided_by/
  decided_at/notes shape, but only ever remembers the *most recent*
  decision inline on the row. This prompt needed a true append-only
  event log instead (a recommendation can be judged more than once over
  its life — reviewed, then later resolved), so
  `RecommendationFeedbackEvent` is its own table, not more columns on
  `RecommendationRecord`.
- **`feedback/store.py`** (Chroma like/dislike on any answer) and
  **`embeddings/golden_examples.py`** (thumbs-up on a confirmed SQL
  result, feeding few-shot retrieval) — both inspected and left
  completely untouched. Neither is a governed lifecycle with RBAC/ABAC,
  an audit trail, or a quality rollup; this is purely additive, not a
  duplicate of either.
- **`api/chat_persistence.py`** — the exact "fail-open, locally-
  authenticated-only, built from the already-computed response, never
  re-derived from raw agent state" contract `api/recommendation_
  persistence.py` reuses verbatim for a different kind of write.

**The one real design decision, made explicitly rather than assumed:**
master rule ("feedback must not automatically alter production rules
from a single event") requires that `RecommendationFeedbackEvent` rows
never feed back into `recommendation.engine`'s own rules/thresholds.
Resolved structurally, not by convention: the only read path over that
table is `quality_metrics_for_tenant` (plain counts), and no code
anywhere in this codebase reads a feedback event and writes to
`config.settings.Settings` or a `recommendation.engine` rule constant.
Any future auto-tuning feature would have to be built as its own new,
reviewed decision — nothing here wires it, even implicitly.

## 2. What was built

### `recommendation/governance.py` (new, pure)

`RecommendationStatus` — the exact eight values: `GENERATED`, `REVIEWED`,
`ACCEPTED`, `REJECTED`, `PARTIALLY_USEFUL`, `INCORRECT`, `RESOLVED`,
`EXPIRED`. `VALID_STATUS_TRANSITIONS`:

```
GENERATED        -> {REVIEWED, ACCEPTED, REJECTED, PARTIALLY_USEFUL, INCORRECT, EXPIRED}
REVIEWED         -> {ACCEPTED, REJECTED, PARTIALLY_USEFUL, INCORRECT, EXPIRED}
ACCEPTED         -> {RESOLVED, EXPIRED}
PARTIALLY_USEFUL -> {RESOLVED, EXPIRED}
REJECTED / INCORRECT / RESOLVED / EXPIRED -> {}  (terminal)
```

`FEEDBACK_VERDICT_STATUSES` (the five `POST .../feedback` accepts) and
`TERMINAL_STATUSES`/`is_terminal_status` are derived, named subsets —
`RESOLVED`/`EXPIRED` are deliberately excluded from the general feedback
endpoint and given their own dedicated routes (`.../resolve`,
`.../expire`) instead, since "the fix was actually carried out" and "this
is stale/administratively retired" are different kinds of claims than a
quality verdict, worth their own request shape and permission check.

### `recommendation/governance_policy.py` (new, pure RBAC+ABAC)

`authorize_recommendation_action` — deny-by-default, mirrors
`semantic.catalog_policy.authorize_catalog_action` exactly. Two new
permissions in `identity/rbac.py`:
`Permission.RECOMMENDATION_REVIEW` (analyst+: view, submit feedback, mark
resolved — domain judgment) and `Permission.RECOMMENDATION_MANAGE`
(admin-only: force-expire — an administrative override). Both seed
automatically via the existing idempotent `identity.bootstrap.seed_rbac`
on next app startup (**verified live** — see §3). Every action beyond
none requires `security.tenancy.resolve_actor_tenant_id` to match
`RecommendationRecord.tenant_id`; a cross-tenant or nonexistent record is
denied identically (`"cross_tenant"`/`"record_not_found"` → the route
layer maps both to a 404, anti-enumeration, matching `api/semantic_
catalog.py`'s own precedent).

### `identity/models.py` (two new tables) + migration `f3a6b9c2d8e1`

- **`RecommendationRecord`** — one immutable snapshot of a Prompt-17
  `Recommendation` (category, kind, claim, evidence, affected_entity,
  action, measurable_impact, confidence, limitations, rule_or_model, all
  copied verbatim), plus `tenant_id`/`database_id` (scoped, mirroring
  `OnboardingJob`/`SemanticCatalogEntry`), a mutable `status` column, and
  two independent version snapshots: `engine_version`
  (`Recommendation.engine_version`) and `evidence_version` (a SHA-256
  hex digest over the evidence JSON — a concrete, content-addressable
  identifier distinct from the engine version, since nothing on
  `Recommendation` itself separately records which analytics-engine
  version produced its evidence).
- **`RecommendationFeedbackEvent`** — one immutable row per lifecycle
  transition: `from_status`/`to_status`, `actor_user_id` *or*
  `actor_label` (a real human reviewer sets the former; a future
  automated sweep sets the latter, e.g. `"system:expiry_sweep"` — the
  audit trail always names *something*), `reason`, and both
  `recommendation_version`/`evidence_version` **re-snapshotted onto every
  event**, not just the parent record — the literal "capture ...
  recommendation/evidence versions" requirement, applied per-event so a
  quality rollup can be sliced by exactly which version a piece of
  feedback was about even if the record itself is later re-generated
  under a newer engine version.

### `identity/repositories/recommendation_governance.py` (new)

`create_record` (the only creation path — see §2's API section for why),
`submit_feedback`/`mark_resolved`/`expire_record` (each enforces
`VALID_STATUS_TRANSITIONS` via `_assert_transition_valid`, raising
`InvalidRecommendationStatusTransitionError` on an illegal move — defense
in depth behind the API layer's own pre-flight check, mirroring
`identity/repositories/semantic_catalog.py`'s identical posture),
`list_records`/`get_record_by_id`/`list_feedback_events` (plain, tenant-
scoped reads), and `quality_metrics_for_tenant` — a read-only aggregate
(counts by status, counts by `(category, status)`, an `acceptance_rate`
that is `None`, never `0.0`, when nothing has been judged yet) that is
the one and only consumer of `RecommendationFeedbackEvent` rows in this
codebase, structurally proving the "never alters production rules"
requirement (see §1).

### `api/recommendation_governance.py` + `api/recommendation_governance_schemas.py` (new)

Seven routes under `/recommendations`, each doing exactly one
authorization dance (resolve by id → `authorize_recommendation_action` →
act only on `allowed=True`):

| Route | Permission | Purpose |
|---|---|---|
| `GET /recommendations` | REVIEW or MANAGE | List the caller's tenant's records |
| `GET /recommendations/metrics` | REVIEW or MANAGE | Quality rollup (registered *before* `/{record_id}` so the literal path segment is never swallowed as a malformed UUID) |
| `GET /recommendations/{id}` | REVIEW or MANAGE (ABAC) | One record |
| `GET /recommendations/{id}/events` | REVIEW or MANAGE (ABAC) | Full audit trail |
| `POST /recommendations/{id}/feedback` | REVIEW or MANAGE (ABAC) | A `reviewed`/`accepted`/`rejected`/`partially_useful`/`incorrect` verdict |
| `POST /recommendations/{id}/resolve` | REVIEW or MANAGE (ABAC) | `accepted`/`partially_useful` → `resolved` |
| `POST /recommendations/{id}/expire` | MANAGE only (ABAC) | Any non-terminal → `expired` |

**There is deliberately no create/ingest route** — a `RecommendationRecord`
is only ever created by the live `/ask` pipeline's own already-
`Permission.ASK`-gated call (below), never by a caller hitting this
surface directly; `authorize_recommendation_action` has no `CREATE`
action to recognize at all.

### `api/recommendation_persistence.py` + `/ask` wiring (new, additive)

`persist_ask_recommendations` — called from `POST /ask` immediately after
the existing `persist_ask_turn` (chat-history) call, wrapped in the
identical `try/except`-log-and-continue shape. Reconstructs
`recommendation.models.Recommendation` objects from
`AskResponse.recommendations`'s own dicts and calls `create_record` once
per recommendation, for a **locally-authenticated caller only**
(`AuthIdentity.mode == "local"` and `Settings.local_auth_enabled`) —
identical scoping to `persist_ask_turn`, for the identical reason (no
`identity.users` row, no resolved tenant, for any other caller mode). A
malformed recommendation dict is logged and skipped, never fatal to the
rest. New `Settings.enable_recommendation_persistence` (default `True`)
is the operator escape hatch.

## 3. Testing

`tests/test_recommendation_governance_model.py` (28 tests) — every
required status exists; every allowed/disallowed transition pair;
terminal-status set; feedback-verdict subset.

`tests/test_recommendation_governance_policy.py` (18 tests) — RBAC
(review vs. manage vs. neither, per action), ABAC (missing/mismatched/
`None` tenant, all denied with the correct reason code), unrecognized
action.

`tests/test_identity_repositories_recommendation_governance.py` (25
tests, real in-memory SQLite) — creation + initial audit event + system-
actor labeling, evidence-version determinism, list/get/filter, every
legal transition, illegal-transition/terminal-status rejection,
resolve/expire (including a `None`-user-id system actor), and the
quality-metrics aggregate (counts, acceptance rate, `None` when
unjudged, tenant/database scoping, and a direct proof that reading
metrics never mutates a record).

`tests/test_api_recommendation_governance.py` (20 tests, real `TestClient`
+ in-memory SQLite, the same two-tenant-by-email-domain simulation
`tests/test_api_semantic_catalog.py` already established) — RBAC per
route (admin/analyst/plain-user), tenant isolation (cross-tenant record
and cross-tenant feedback both 404, a random id is also a 404, list is
tenant-scoped), the full feedback→resolve lifecycle end to end via HTTP,
`resolved`/`expired` rejected by the general feedback endpoint's own
schema (422, never reaching the repository), an illegal transition
surfacing as 409, expire being admin-only, the metrics route's RBAC and
its registration-order guard against being swallowed by `/{record_id}`.

`tests/test_recommendation_persistence.py` (9 tests, real in-memory
SQLite) — successful persistence, zero-recommendations no-op, OIDC
identity skipped, `local_auth_enabled=False` skipped, the feature flag
off, a malformed recommendation dict skipped without blocking the rest,
an unreachable identity DB failing open, a non-UUID subject failing
open, and the `database=None` → `"default"` fallback.

**Live-verified against the real, configured PostgreSQL identity
database** (`AUTH_DATABASE_URL`), not only SQLite: `alembic -c identity/
alembic.ini upgrade head` ran the full pending chain — this also
revealed and applied three *prior* prompts' migrations
(`c4d8e1f6a9b3`/onboarding, `d5e9f2a7b4c1`/semantic-catalog,
`e8f1c3a6d9b2`/catalog-metric-fields) that had never actually been
applied to this real database before, in addition to this prompt's own
`f3a6b9c2d8e1` — confirmed at `head` afterward, with both new tables
present in `information_schema.tables`. `identity.bootstrap.seed_rbac`
was then run directly against that same real database and confirmed
both new permission codes (`recommendation.review`/`recommendation
.manage`) now exist in `permissions`.

Full regression suite reran clean after every change: **3272/3272**
backend tests passing (up from 3172 before this prompt — 100 new tests),
0 new mypy errors outside test files, 0 ruff findings, 0 bandit
findings, `black --check` clean.

## 4. Known, disclosed limitations (not oversights)

- **No automated expiry sweep exists yet** — `expire_record`'s
  `actor_label`-without-`actor_user_id` path is built and tested for a
  future scheduled job, but nothing in this codebase currently calls it
  on a timer; every `EXPIRED` transition today is a human-initiated
  admin action through the API.
- **`quality_metrics_for_tenant` is a simple count rollup, not a
  statistical model** — no confidence interval, no time-windowing
  (last 7/30 days), no per-rule breakdown beyond category — a
  deliberately minimal "measure it" foundation for a future dashboard to
  build richer views on top of, not a finished analytics product.
- **Persistence only ever happens from `/ask`'s own live pipeline** —
  there is no backfill path for recommendations the engine produced
  before this prompt existed (none were ever persisted), and no bulk-
  import route; this is a forward-looking log, not a retroactive one.
- **No frontend changes in this pass** — every route here is real,
  authorized, tested API surface; nothing in `frontend/src` reads or
  renders it yet, the same deferred-wiring posture Prompts 14/15/16/17
  already established for their own new surfaces.
