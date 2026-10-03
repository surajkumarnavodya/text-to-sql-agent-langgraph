# Prompt 31 — Recommendation & Action Dashboard

Mandatory first step: `00_MASTER_IMPLEMENTATION_CONTRACT.md` (read and applied).

## Objective

Build the recommendation-focused experience on top of the governed
recommendation APIs (Prompts 17 and 18): a reviewer can see what the platform
suggested, why, on what evidence, and act on it — accept, reject, partially
useful, resolve, assign an owner, add notes — with every action governed,
tenant-scoped, rate-limited, logged, and recorded in an append-only trail.

## Areas inspected

- `recommendation/governance.py` (lifecycle states, transition table), `governance_policy.py` (RBAC + ABAC), `models.py` (`Recommendation`, `ProvenancedClaim`-shaped evidence)
- `identity/models.py` (`RecommendationRecord`, `RecommendationFeedbackEvent`), migration `f3a6b9c2d8e1` and head `a4c7e2b9d6f5`
- `identity/repositories/recommendation_governance.py`, `identity/repositories/semantic_catalog.py::_apply_transition` (Prompt 27's race-safe transition helper)
- `api/recommendation_governance.py`, its schemas, `api/recommendation_persistence.py` (the only creation path), `api/rate_limit.py`
- `security/audit_log.py` (`log_security_event`), `identity/repositories/audit.py` (persisted audit table)
- Existing tests: `tests/test_api_recommendation_governance.py`, the repository and policy suites
- Frontend: `SemanticReview.tsx` (review-dashboard pattern), `TruthLevelBadge` (Prompt 30), `tenantAdminApi.ts`/`queries.ts` (the only prior consumers of the recommendation list and metrics), `AppShell.tsx` review-tab gating, the i18n locale files

## Gaps found by inspection

1. **No owner concept.** A record could not be assigned to anyone.
2. **No note/comment action.** The audit trail only held status changes.
3. **Race in status transitions.** `submit_feedback` validated the in-memory status and then committed an unconditioned `UPDATE`. Two reviewers acting on the same record could both succeed. The same bug class Prompt 27 fixed for the semantic catalog.
4. **Mutations not on the structured security log**, and the three transition routes had no rate limit.
5. **No frontend consumer** for the governed lifecycle. Only the tenant-admin summary read the list and metrics.

## Existing functionality reused

- Lifecycle table and transition validation (`recommendation/governance.py`) — unchanged.
- RBAC + ABAC decision engine (`authorize_recommendation_action`) — extended with two actions only.
- `RecommendationRecord` / `RecommendationFeedbackEvent` tables and the append-only audit trail — extended, not replaced.
- Prompt 27's conditional-`UPDATE` race fix, applied to this table (the same pattern, written for `recommendation_records`).
- `enforce_api_action_rate_limit`, `log_security_event`, `resolve_actor_tenant_id`, `require_local_user`.
- `TruthLevelBadge` from the analytics panels (Prompt 30) — shared, not duplicated.
- `useTenantUsers` for the admin owner picker.
- `tenantAdminApi`'s two read calls now re-export from the new `recommendationApi.ts` (single home for the endpoint wrappers).

## Files

**Created**
- `identity/migrations/versions/b7e2d4a1c9f0_recommendation_actions.py`
- `tests/test_api_recommendation_actions.py`, `tests/test_recommendation_actions_repository.py`
- `frontend/src/lib/recommendationApi.ts`, `frontend/src/lib/recommendationDisplay.ts` (+ test)
- `frontend/src/components/recommendations/RecommendationList.tsx`, `RecommendationDetail.tsx`
- `frontend/src/pages/Recommendations.tsx` (+ test)
- `31_RECOMMENDATION_ACTION_DASHBOARD_CONTRACT.md`

**Modified**
- `identity/models.py` — `RecommendationRecord.owner_user_id`; `RecommendationFeedbackEvent.event_type`, `detail`
- `identity/repositories/recommendation_governance.py` — conditional transitions; `add_note`, `assign_owner`, `owner_display_names`; list filters
- `recommendation/governance_policy.py` — `ADD_NOTE`, `ASSIGN_OWNER` (analyst+, tenant-gated)
- `api/recommendation_governance.py`, `api/recommendation_governance_schemas.py` — new routes, filters, owner display names, logging, rate limits
- `frontend/src/lib/types.ts`, `frontend/src/lib/tenantAdminApi.ts`, `frontend/src/hooks/queries.ts`
- `frontend/src/App.tsx`, `frontend/src/components/layout/AppShell.tsx` (nav tab gated on the existing admin/analyst check), five `i18n/locales/*/translation.json` (nav label)
- `docs/navigation-and-actions.md`, `README.md`, `CLAUDE.md`

## API changes (backward compatible)

| Route | Change |
|---|---|
| `GET /recommendations` | New optional `owner_user_id` and `unassigned` query params. Response gains `owner_user_id`, `owner_display_name` |
| `GET /recommendations/{id}` | Response gains the same two owner fields |
| `GET /recommendations/{id}/events` | Each event gains `event_type` (default `status_change`) and `detail` |
| `POST /recommendations/{id}/notes` | **New.** Body `{note}` (1–4000 chars). Never changes status |
| `POST /recommendations/{id}/owner` | **New.** Body `{owner_user_id}` (`null` clears). Missing, inactive, cross-tenant, or non-reviewer owner → the same 404 |
| `POST /recommendations/{id}/feedback`, `/resolve`, `/expire` | Unchanged contract. Now rate-limited and logged |

No existing field was removed or renamed. Existing clients ignore the new fields.

## Database changes

Migration `b7e2d4a1c9f0` (revises `a4c7e2b9d6f5`), purely additive:
- `recommendation_records.owner_user_id` (nullable FK to `users`, `ON DELETE SET NULL`, indexed)
- `recommendation_feedback_events.event_type` (`NOT NULL`, server default `status_change`, so every existing row is valid)
- `recommendation_feedback_events.detail` (nullable JSON)

Apply with `alembic -c identity/alembic.ini upgrade head`. `downgrade()` removes only what `upgrade()` added.

## Configuration

No new settings. Relevant existing ones:
- `API_ACTION_RATE_LIMIT_PER_MINUTE` (default `20`) — per-IP, per-action budget. Applies independently to `recommendation_feedback`, `recommendation_resolve`, `recommendation_expire`, `recommendation_note`, `recommendation_owner`.
- `ENABLE_RECOMMENDATION_PERSISTENCE` — unchanged; still the only way a record is created.

## Security review

- **Authorization order.** Every route resolves the record, runs `authorize_recommendation_action`, and only then consumes rate-limit budget. A denied caller cannot exhaust a reviewer's budget (tested).
- **Owner validation is server-side, in the repository.** The assignee must be an active account in the record's own tenant *and* hold a recommendation-review permission. The API cannot skip this.
- **Anti-enumeration.** Cross-tenant, nonexistent, inactive, or non-reviewer owner ids all produce byte-identical 404 bodies (tested by comparing the two responses).
- **No LLM in any decision path.** Notes, verdicts, and ownership are human inputs. Nothing here calls a model.
- **Log hygiene.** Log lines carry ids, the action, the category, and the status transition. They never carry claim text, evidence, rationale, or note bodies (tested: a note's text is absent from every log line).
- **Untrusted text is rendered as plain text.** Evidence values can contain database content. The UI renders them as React text, never markup.
- **Refused owner assignments are logged at `warning`** (`event=recommendation_owner_rejected`), including the requested user id, so operators can see probing attempts.

## Tenant-isolation review

- The tenant comes only from the caller's own row (`resolve_actor_tenant_id`), never from the request. The frontend sends no tenant id (asserted in a test).
- Owner assignment compares the **stored** `users.tenant_id` with the record's `tenant_id`, so the check does not depend on the session's view of the caller.
- Owner display names are looked up in one batched query for the list and are returned only for the caller's own tenant's records.
- The admin owner picker lists only the tenant's own users (`/tenant-admin/users`, already tenant-scoped).

## Performance review

- List endpoint: one extra batched `IN` query for owner names, regardless of list size. Unchanged `limit=200` cap.
- Status transitions: still one `UPDATE` plus one `INSERT` plus one `COMMIT`. The conditional `WHERE` uses the primary key and the status column (indexed).
- Note and owner writes add one `refresh` read each (to capture the current status for the audit row).
- Frontend: the history fetch is per selected record and cached by React Query; mutations invalidate only the recommendation keys.

## Testing

Backend (`tests/test_api_recommendation_actions.py`, `tests/test_recommendation_actions_repository.py`) — 46 tests:
- Notes: append-only, status unchanged, ordering in the trail, empty note rejected, plain user 403, cross-tenant 404.
- Owner assignment: self-assign with display name, no display name → `null` (never an email), clear with `null`, previous-owner recorded, non-reviewer refused, cross-tenant and nonexistent indistinguishable, extra fields rejected.
- Ownership filters (including `unassigned` precedence and malformed UUID → 422), batched owner names.
- Lifecycle still works end to end; notes allowed on terminal records.
- Structured logging: one line per mutation, no note/claim text in any log line, refused owner logged as warning.
- Rate limiting: second call in window → 429; an unauthorized call does not spend the budget.
- **Race safety (repository):** two sessions load the same `accepted` record; one resolves; the other's stale attempt is refused by the conditional `UPDATE`, and exactly one `resolved` event exists.
- **Mutation check:** removing the `WHERE status = expected` guard makes the race test fail. Restored afterward.
- Refused transitions write no event. Notes and owner changes never change status.
- Owner validation at the repository layer: cross-tenant, inactive, non-reviewer, unknown.
- New policy actions: allowed for reviewers in tenant, denied for plain users, denied cross-tenant before any role check.

Frontend (`lib/recommendationDisplay.test.ts`, `pages/Recommendations.test.tsx`):
- Lifecycle visibility rules (verdicts, resolve, expire, terminal set).
- Honest labelling: every item shows "AI estimate"; evidence shows its own truth level; no-evidence warning; unmeasured impact shown as unmeasured.
- Actions call the real API with the typed reason; a 409 message is shown; notes; assign-to-me sends only the signed-in user id.
- Role gating: analyst cannot see Expire; admin can; a plain user sees a role message; terminal records show no decision controls.
- Audit trail: notes and status changes labelled distinctly; another reviewer's note is attributed generically.

## Known limitations

- **Local accounts only.** Like every governance route, these require `require_local_user`. OIDC-authenticated reviewers get 401 — the same disclosed limitation as the rest of the governance API.
- **No pagination.** The list is capped at 200 (existing behaviour); a larger tenant needs a cursor.
- **Owner changes are last-writer-wins.** Two admins reassigning the same record at once both succeed, each recorded in the trail. Only status transitions are race-guarded.
- **Owner names come from display names only.** An unnamed owner shows as "Assigned to a reviewer", by design; the admin picker falls back to the email, which the admin already sees on the Users tab.
- **History does not name owners.** An `owner_assigned` event records the owner's id, not their name, because the event row stores ids only.
- **Rate limit is per IP**, not per user (`api/rate_limit.py`'s existing keying for these routes). Users behind one NAT share a budget of 20 actions a minute per action type.
- **The engine's `operations` category never fires live** (inherited from Prompt 14/17). Its recommendations are shown if they exist, but this dashboard does not manufacture them.
- **No automatic learning.** A verdict is recorded and counted; it never adjusts an engine rule (master-contract rule, held structurally).
- **Frontend copy is English-only** on this page, except the nav label, which is translated across all five locales. The same minimum-bar precedent as Prompts 26 and 27.

## Remaining risks

- The race fix covers the *status* path only. A future action that changes another column conditionally must follow the same pattern.
- The mutation test was run once by hand. It is not a CI gate.
- Owner display names are cached by React Query for the list lifetime; a renamed reviewer shows the old name until the next fetch.

## Recommended next prompt

Prompt 32 (final in the series) should close the remaining operational gaps across the platform: a cursor-paginated recommendations list, OIDC support for the governance routes, and an end-to-end browser test of the lifecycle with a real database.
