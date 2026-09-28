# Secure conversation sharing

**Status: implemented and live-verified against a real local PostgreSQL
identity database.** This document is the canonical design record for
conversation sharing — `SECURITY.md` and `CLAUDE.md` both point here rather
than duplicating it.

A shared conversation is a **controlled, read-only snapshot/projection** —
never an authentication session, cookie, OAuth credential, database
credential, provider key, private agent prompt, or a live query channel.
Every server boundary is deny-by-default; no route trusts an object ID or
frontend filtering as authorization.

## Tenant scope — a deliberate, scoped decision

This application is **single-tenant by design** (`identity/models.py`'s own
docstring, `agent/rate_limit.py`'s existing "no-tenant-isolation"
disclosure) — there is no `Tenant` model, and no `tenant_id` column on
`users`/`conversations`. Retrofitting real multi-tenancy across the whole
application was explicitly out of scope for this feature (a much larger,
separate project). Instead, **only the new sharing tables carry a scoped
`tenant_id`** (`security/tenancy.py::resolve_actor_tenant_id`, one function,
returning a single constant for every real user today), and
`identity.share_policy.authorize_share_action` genuinely enforces a
tenant-match condition against it — this is what makes "cross-tenant
member cannot view even with a valid-looking ID" a real, tested code path
(`tests/test_share_policy.py::TestCrossTenantIsolation`,
`tests/test_api_shares.py::TestCrossTenantIsolation`, the latter simulating
two tenants deterministically by email domain) rather than a check that
only starts existing once real multi-tenancy is eventually built. A future
multi-tenant retrofit only needs to change `resolve_actor_tenant_id`.

## Data model (`identity/models.py`, migration `b21f6a3c9d47`)

```
ConversationShare      -- one row per shared conversation (unique on conversation_id)
  id, conversation_id, owner_user_id, tenant_id
  access_mode ("invite_only" | "anyone_with_link"), default_permission ("viewer")
  status ("active" | "disabled"), snapshot_message_sequence, snapshot_captured_at
  expires_at, revoked_at, version, created_at, updated_at

ShareMember             -- an invited/accepted viewer (never the owner)
  id, share_id, user_id (nullable until matched/accepted), invited_email
  role ("viewer"), status ("pending"|"active"|"revoked"|"expired")
  invitation_token_hash, invitation_expires_at, expires_at, accepted_at, revoked_at

ShareLink               -- one version of an "anyone with the link" bearer token
  id, share_id, token_hash (SHA-256, never the raw token), token_version
  expires_at, revoked_at, last_used_at

ShareAuditEvent         -- privacy-safe audit trail, share_id/actor_user_id ON DELETE SET NULL
  id, share_id, conversation_id, actor_user_id, event_type, result, reason,
  request_id, safe_metadata, created_at
```

Foreign keys, live-verified against real PostgreSQL:
`conversation_shares.conversation_id`/`owner_user_id`, `share_members.share_id`/
`user_id`, `share_links.share_id` → `ON DELETE CASCADE`;
`share_audit_events.share_id`/`actor_user_id` → `ON DELETE SET NULL` (the
audit trail must outlive what it describes).

**Snapshot boundary**: `ConversationShare.snapshot_message_sequence` is
captured at share-creation/update time from the conversation's current max
`Prompt`/`AiOutput.sequence_number`. A message sent afterward is invisible
to any viewer until the owner explicitly calls `PATCH .../share` with
`refresh_snapshot=true` — verified directly
(`tests/test_identity_repository_shares.py::TestCreateShare`,
`tests/test_api_shares.py::test_create_captures_snapshot_and_excludes_later_messages`).

**Optimistic concurrency**: every `PATCH`/revoke supplies the `version` it
last read; a stale value raises `ShareVersionConflictError` → HTTP 409,
never a silent overwrite (`identity/repositories/shares.py::update_share`/
`revoke_share`).

**Revoke vs. disable, and re-sharing**: `status` toggling (owner's "Turn
off"/"Turn on") is reversible and never touches `revoked_at`. `revoke_share`
sets `revoked_at` **permanently** — `authorize_share_action` denies every
viewer unconditionally once `revoked_at` is set, regardless of `status`. A
real design gap found by this project's own test-writing (not by the user):
`ConversationShare.conversation_id` is unique, so a revoked share can never
simply be re-created — `identity.repositories.shares.reactivate_share` is
the one function that clears `revoked_at`, called only from `POST
.../share`'s own idempotent-create path when an existing, revoked row is
found, treating re-sharing as a fresh grant (new snapshot, new expiry).

## RBAC + ABAC (`identity/share_policy.py`)

`authorize_share_action(actor, actor_tenant_id, share, action,
conversation_deleted, member=None, link=None, now)` is the **single**
decision point every route/resolver calls — pure policy, no FastAPI/DB
import, unit-testable with plain duck-typed objects
(`tests/test_share_policy.py`, 35 cases).

### RBAC roles
| Role | Determined by | May do |
|---|---|---|
| Owner | `share.owner_user_id == actor.id` | create/update/invite/remove/regenerate/revoke, plus view/download as a viewer would |
| Viewer | `ShareMember.role == "viewer"`, `status == "active"` | view snapshot + approved attachments only |
| Anonymous link viewer | `access_mode == "anyone_with_link"` + a valid `ShareLink` | view snapshot + approved attachments only, no tenant check (no identity to check) |
| Editor | **never modeled** | — deliberately out of scope per the feature's own requirement |

### ABAC conditions, in order (first failing condition wins)
1. Share exists (`share_not_found` otherwise — see "never confirm existence" below).
2. Parent conversation not soft-deleted (`conversation_deleted`) — independent of `share.revoked_at`.
3. **Owner-only actions**: authenticated, is the recorded owner, tenant matches.
4. **Viewer actions**: share not revoked/disabled/expired; if a link is presented, that link not revoked/expired; public-link mode requires a valid link; invite-only mode requires authenticated + tenant match + an active, non-expired, `"viewer"`-role membership (unknown role → `unknown_role`, denied).

Deny-by-default: an unrecognized `ShareAction`, an unrecognized member role,
a `None` tenant — every one denies, never allows.

## Token security (`identity/security.py`, reused directly)

- **Generation**: `generate_refresh_token()` — 48 CSPRNG-random bytes,
  URL-safe base64 (≈384 bits, well over the 128-bit minimum) — the exact
  same function already used for refresh tokens/password-reset/
  email-verification tokens, per this codebase's own "identical
  requirement, don't reinvent" precedent.
- **Storage**: only `hash_refresh_token()`'s SHA-256 hex digest is ever
  persisted (`ShareLink.token_hash`, `ShareMember.invitation_token_hash`).
  The raw token is returned **exactly once**, in the synchronous API
  response of the one action that minted it (`POST .../share`, `POST
  .../share/link/regenerate`, the invite response's `invitation_path`) —
  never logged, never in `ShareAuditEvent.safe_metadata`.
- **Expiry/revocation**: checked by `authorize_share_action`, normalizing
  SQLite's naive datetimes to UTC first (`identity/share_policy.py::_is_expired`
  — a real bug found and fixed during this build: a raw `now >=
  share.expires_at` comparison crashed with `TypeError: can't compare
  offset-naive and offset-aware datetimes` the moment a real SQLite-backed
  test exercised it, the identical class of bug this codebase's own
  `identity.repositories.users._aware_utc` already exists to prevent
  elsewhere).
- **Regeneration**: `regenerate_link` revokes every existing active link
  for the share and inserts a new row with an incremented `token_version` —
  the old token's own row is independently `revoked_at`-marked, so it fails
  immediately even if not yet expired (verified:
  `tests/test_identity_repository_shares.py::test_regenerating_invalidates_the_previous_token`,
  `tests/test_api_shares.py::test_regenerated_link_invalidates_the_previous_token`).
- **Never accepted as owner authentication** — a link only ever grants
  `ShareAction.VIEW`/`DOWNLOAD_ATTACHMENT`, resolved through the exact same
  `authorize_share_action` every other caller goes through; there is no
  code path where holding a link elevates a caller to "owner."
- **Brute force**: the raw token space (2^384) makes offline guessing
  infeasible regardless of rate limiting; `GET /share-view/{ref}` is still
  rate-limited per-IP (`Settings.share_link_access_rate_limit_per_minute`,
  default 30/min) as defense-in-depth against scraping/automated probing.

## Cache and Referrer protections

- **`Cache-Control: private, no-store`** on every share route response —
  owner-management and the anonymous viewer alike.
- **`Referrer-Policy: no-referrer`** and **`X-Content-Type-Options: nosniff`**
  on the anonymous-reachable surface (`GET /share-view/{ref}` and its
  attachment sibling), overriding this app's global default
  (`strict-origin-when-cross-origin`) specifically for bearer-link pages.
- **A real bug, found only by live-testing the running app (not by
  `TestClient` alone)**: `response.headers[...] = ...` set on the
  dependency-injected `Response` object is silently discarded whenever the
  route raises `HTTPException` instead of returning normally — FastAPI
  builds a *separate* response object for exceptions. This meant the
  **denial path** — the security-critical one for an anonymous endpoint —
  served the default global headers, not the intended private/no-referrer
  ones, and reproduced live via `curl` against a real running instance
  before being caught by any automated test. Fixed by moving every safe
  header onto `HTTPException(..., headers=_SAFE_ERROR_HEADERS)` explicitly,
  and closed with a named regression test
  (`tests/test_api_shares.py::test_denied_shared_view_also_carries_the_safe_headers_not_just_the_success_path`)
  and a second live re-verification via `curl` confirming the fix.
- **CSP `frame-ancestors 'none'` / `X-Frame-Options: DENY`** already apply
  globally (`api/main.py::_add_security_headers`) — no separate wiring
  needed for share pages.
- **Service worker**: `/share-view` and `/share-invitations` are explicit
  `NetworkOnly` entries in `frontend/vite.config.ts`'s `BACKEND_ROUTES` list
  (the same list also drives dev-server proxying) — confirmed present in
  the built `dist/sw.js` after a production build. `/conversations`/`/chat`
  were added to the same list in this pass too, closing a related,
  pre-existing gap those routes had (present in this same list's
  proxy/cache-exclusion role, despite CLAUDE.md's own prior note claiming
  they didn't need one).
- **No third-party requests carry the share token**: the frontend never
  constructs an absolute URL server-side (avoiding any `Host`-header-trust
  issue); `robots: noindex, nofollow` is set app-wide in `index.html`.
  **Known, disclosed limitation**: the shared-conversation page still loads
  Google Fonts via the same static `index.html` every other page uses —
  the token itself is not leaked via `Referer` (modern browsers only send
  the origin cross-origin under `strict-origin-when-cross-origin`, and this
  page additionally sets `no-referrer`), but a truly zero-third-party
  bearer-link page would need a dedicated minimal HTML entry point, out of
  scope for this pass.

## Source projection (`identity/repositories/shares.py::build_share_projection`)

A hard **allowlist**, not a blocklist: `_PROJECTED_METADATA_FIELDS` names
exactly which keys of `AiOutput.metadata_json` (see
`api/chat_persistence.py::_build_history_metadata`) ever reach a viewer —
`sources_used`, `database`, `model`, `sql`, `row_count`, `insight`,
`synthesized_answer`, the four per-source result dicts, `attachment_refs`,
`result_snapshot`. Everything else — `query_plan`, `schema_tables`,
`retry_count`, `failure_explanation`, `rejection_message`, and every other
internal/operational field — is categorically absent, verified directly
(`tests/test_identity_repository_shares.py::test_projection_only_exposes_allowlisted_metadata_fields`).
A newly-added metadata field defaults to **not** appearing in a share until
someone deliberately adds it to the allowlist.

`ProjectedTurn.content` is always this app's own pre-existing safe display
text (the same string already shown in the owner's own reloaded-conversation
view) — never raw agent state, a tool trace, or a stack trace.

**Attachments require a second, independent check beyond share-level
access**: `ShareProjection.approved_attachment_ids` is the exact set of
attachment IDs referenced by turns within the snapshot boundary — `GET
/share-view/{ref}/attachments/{id}` checks membership in this set *after*
confirming the caller may view the share at all, and records a distinct
`idor_attempt` audit event (not just `download_denied`) when an
otherwise-authorized viewer requests an attachment id outside that set
(verified: `tests/test_api_shares.py::test_guessed_unrelated_attachment_id_is_denied_even_for_the_owner`).
Attachment bytes are read from `attachments.store`'s process-lifetime,
FIFO-evicted cache — a share's attachment can become genuinely unavailable
(evicted) independent of the share's own validity; this is surfaced as a
distinct `attachment_unavailable` denial reason, not conflated with an
authorization failure.

**Never invokes the SQL/orchestrator pipeline**: `api/shares.py` has no
`import` of `agent.graph`/`agent.orchestrator` at all — a structural
guarantee, verified via `ast` parsing of the module's own source
(`tests/test_api_shares.py::TestAgentAndSqlIsolation`), not just a
behavioral claim. There is no "Confirm and Run," no chart regeneration, no
composer, no way to trigger SQL/web/document/vector/model/image work from
opening a share.

## API surface (`api/shares.py`)

| Route | Auth | Purpose |
|---|---|---|
| `POST /conversations/{id}/share` | Owner (`SHARES_CREATE_OWN`) | Create (idempotent) or re-share after revocation |
| `GET /conversations/{id}/share` | Owner | Current settings, never a raw link |
| `PATCH /conversations/{id}/share` | Owner (`SHARES_MANAGE_OWN`) | Update access mode/expiry/status/snapshot, versioned |
| `POST /conversations/{id}/share/revoke` | Owner | Permanent revoke, versioned |
| `POST /conversations/{id}/share/link/regenerate` | Owner | New link, invalidates the old one |
| `POST /conversations/{id}/share/members/invite` | Owner | Invite by email, no account-existence disclosure |
| `DELETE /conversations/{id}/share/members/{id}` | Owner | Remove a member |
| `POST /share-invitations/{token}/accept` | Any authenticated local user | Redeem an invitation for the caller's own account |
| `GET /share-view/{ref}` | Anonymous or authenticated | The read-only projection |
| `GET /share-view/{ref}/attachments/{id}` | Anonymous or authenticated | Re-authorized attachment download |

**Deliberately a separate path prefix (`/share-view`) from the frontend's
own `/shared/:ref` client-side page route** — a real routing collision was
caught before it shipped: both would otherwise resolve to the same URL in
the browser, meaning either the backend JSON API would intercept a genuine
page load, or the SPA would swallow the API call, depending on which layer
matched first. `POST /share-invitations/{token}/accept` is similarly
distinct from its own frontend page, `/accept-invitation/:token`.

**Never confirms whether a share exists** on the anonymous surface — every
denial reason (not-found, expired, revoked, wrong tenant, not a member)
collapses to the identical generic 404
(`tests/test_api_shares.py::test_unknown_token_and_a_never_shared_conversation_return_the_identical_generic_denial`);
only a 429 (rate-limited) is ever distinguishable, since that alone reveals
nothing about the target.

**No CSRF token needed**: every mutating route here requires a Bearer
`Authorization` header (never a browser-attached cookie) for the caller to
act as anything but an anonymous link viewer — the same reasoning this
app's existing mutating routes (`/conversations`, `/documents`) already
rely on; a cookie is never sufficient to authenticate a state-changing
request here.

## Observability (`identity/share_audit.py`)

Every lifecycle event writes both a queryable `ShareAuditEvent` row and a
mirrored structured log line
(`security.audit_log.log_security_event`): `share_created`, `share_updated`,
`share_revoked`, `link_regenerated`, `member_invited`, `member_removed`,
`invitation_accepted`, `invitation_rejected`, `view_allowed`, `view_denied`,
`download_allowed`, `download_denied`, `idor_attempt`, `rate_limited`.
`safe_metadata` only ever carries small, pre-approved scalars (counts,
booleans, role names) — never a raw token, cookie, or message body.
`ShareAuditEvent.share_id`/`actor_user_id` are `ON DELETE SET NULL` so the
trail outlives what it describes.

## External configuration / staging steps for an operator

1. **Apply the migration**: `alembic -c identity/alembic.ini upgrade head`
   (live-verified in this pass against a real PostgreSQL identity database,
   both directions).
2. **Re-seed RBAC after deploying this feature**:
   `python scripts/bootstrap_admin.py` (idempotent — safe to re-run). **A
   real, disclosed gap found during this pass's own live verification**:
   this codebase has no automatic "re-seed RBAC on startup" hook for *any*
   feature, not just this one — a brand-new permission added to
   `identity/rbac.py`'s source is invisible to already-provisioned
   deployments until an operator explicitly re-runs this script. Every
   fresh local user correctly got denied `shares.create_own` until this
   was run, which is the *correct*, fail-closed behavior of the new
   permission check — the gap is the missing automatic reseed, not the
   deny-by-default check itself. A `identity.bootstrap.seed_rbac()` call
   inside `api/main.py`'s own `lifespan` startup would close this
   generally, for this and any future permission addition; deliberately
   not added in this pass to avoid touching startup behavior for every
   other existing feature as a side effect of this one.
3. **`ENABLE_CONVERSATION_SHARING`** (default `true`) and
   **`SHARE_PUBLIC_LINKS_ENABLED`** (default `false`) — an "anyone with the
   link" share can only ever be created once an operator explicitly opts
   in; invite-only sharing works with no configuration change.
4. **`SHARE_DEFAULT_EXPIRY_DAYS`** (30), **`SHARE_INVITATION_EXPIRY_DAYS`**
   (14), **`SHARE_MAX_MEMBERS_PER_CONVERSATION`** (50),
   **`SHARE_LINK_ACCESS_RATE_LIMIT_PER_MINUTE`** (30),
   **`SHARE_INVITE_RATE_LIMIT_PER_HOUR`** (20) — all tunable via `.env`.
5. **No outbound email integration exists** for invitations — the owner
   copies the invitation path (`/accept-invitation/<token>`) and sends it
   out-of-band; this is a disclosed, deliberate limitation, not an
   oversight.

## What's verified vs. not

**Live-verified in this pass** (a real local PostgreSQL identity database
was available and used extensively): the migration (both directions), the
real Postgres FK/cascade/set-null behavior (via `pg_constraint`), the full
create → invite → accept → view → revoke → reshare flow via `curl` against
a genuinely running `uvicorn` instance (not `TestClient`), and — critically
— the cache/referrer header bug on the denial path, found only because a
real HTTP response was inspected rather than relying on unit tests alone.

**Not verified in this environment**: a real reverse proxy/CDN sitting in
front of this app (this is a direct, single-instance deployment in this
environment, per this project's own documented "local-first" scale) — the
header-based cache guidance here (`Cache-Control: private, no-store`) is
the correct, standard signal any compliant proxy/CDN should honor, but no
actual proxy/CDN configuration was available to confirm it's honored in
practice. An operator deploying behind a real CDN should independently
confirm it does not override or ignore these headers for this path prefix.
