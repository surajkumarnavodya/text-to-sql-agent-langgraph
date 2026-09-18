# Chat History & Authentication Audit

This document records what was actually found in the repository before any
code was changed for the "universal server-side chat history" feature, the
root cause of the cross-browser/cross-device history problem, and what was
changed as a result. Written from direct inspection of the code (not
assumed) — anywhere a fact could not be verified is marked **[assumption]**.

---

## 1. Current architecture (before this feature)

- **Backend**: FastAPI (`api/main.py`), LangGraph SQL agent (`agent/`),
  optional self-hosted authentication (`identity/`, added in a prior
  session on this same repository — see `docs/AUTHENTICATION.md`) backed
  by a dedicated PostgreSQL database, separate from the business database(s)
  the Text-to-SQL agent queries.
- **Frontend**: React + Vite + TypeScript + Zustand (`frontend/`).
- **Authentication provider**: this app's own (`identity/` — Argon2id
  password hashing, locally-issued JWT access tokens, opaque hashed
  refresh tokens in an `HttpOnly` cookie), plus optional OIDC
  (`security/oidc.py`) and a legacy static bearer token — see
  `docs/AUTHENTICATION.md` for the full picture. This audit focuses on the
  local-account path, since it's the only one with a real, stable,
  per-user database identity to attach chat history to (see §4).

## 2. Current authentication flow (as found)

1. `POST /auth/register` (`api/identity_auth.py`) creates a row in
   `identity.users` (UUID primary key) and, unless email verification is
   required, immediately issues an access token + refresh-token cookie.
2. `POST /auth/login` verifies the password and issues the same token pair.
3. The access token (`identity/security.py::create_access_token`) carries
   `sub` (the user's UUID, as a string) and `roles` — nothing else
   sensitive.
4. `api/auth.py::verify_api_key` validates the token and attaches
   `security.oidc.AuthIdentity(subject=<user id>, roles=..., mode="local")`
   to the request.
5. **Frontend**: `frontend/src/store/localAuthStore.ts` calls
   `POST /auth/refresh` on load to silently restore a session from the
   refresh cookie (in-memory-only access token, never `localStorage`).

This part was already correct and needed no changes — the authenticated
user's identity (`AuthIdentity.subject`, a stable UUID) was already
available to every route; it just wasn't being used for chat history.

## 3. Current chat-history flow (before this feature) — root cause

**Directly inspected**: `frontend/src/store/chatStore.ts` and
`frontend/src/lib/history.ts`.

```ts
// chatStore.ts, before this feature
interface ChatState {
  queryHistory: QueryHistoryEntry[]
  activeConversationId: string           // crypto.randomUUID(), client-only
  conversations: Record<string, ConversationSummary>  // in-memory only
  ...
}
```

- **Every conversation and message lived only in a Zustand store, in
  browser memory.** `frontend/src/lib/history.ts`'s own docstring (before
  this feature) said so explicitly: *"kept in the same in-memory-only
  store as everything else in chatStore (no backend/localStorage
  persistence exists for chat state today, so a full page reload still
  clears it)."*
- **No `localStorage`/`sessionStorage` was used for chat data either** —
  it wasn't a browser-storage bug, it was a *complete absence of any
  persistence layer* for conversations/messages. (`localStorage` *is*
  used elsewhere in the app, e.g. `frontend/src/store/settingsStore.ts`
  for theme/language preferences — never for chat content.)
- `activeConversationId` was `crypto.randomUUID()` — a purely
  browser-generated, session-local identifier, never sent to or
  recognized by the server in any way.
- `POST /ask` (`api/main.py`) accepted an optional `session_id`, but its
  own docstring already disclosed the limitation: *"a correlation token
  today (the API is still stateless); it does not yet gate or scope
  anything server-side."* No database write for a conversation or message
  happened anywhere in the request lifecycle.

**Root cause of "same user, different browser, no history": there was no
server-side storage of chat data at all.** This is not a bug in an
existing sync mechanism — the mechanism didn't exist. Confirmed directly:
`identity/models.py` already defined `Conversation`/`Prompt`/`AiOutput`
tables (added in the same prior session that built `identity/`), but
**no code path anywhere in the repository ever read or wrote them** —
verified by a repo-wide search for `identity.repositories.history` (the
module that would need to exist to use them) before this feature, which
returned nothing.

### Was anything linked to a stable user ID, a browser ID, or a session ID?

Nothing was linked to any persistent identifier at all, because nothing
was persisted. `activeConversationId` (browser-generated) was the closest
thing to an identifier, and it was discarded on every page reload.

### Current search behavior (before this feature)

```ts
// HistoryDrawer.tsx, before this feature
const filtered = useMemo(() => {
  const query = search.trim().toLowerCase()
  if (!query) return allConversations
  return allConversations.filter((c) => c.title.toLowerCase().includes(query))
}, [allConversations, search])
```

- Searched **only conversation titles**, never message content.
- Searched **only whatever was in the in-memory `conversations` map** —
  i.e., only conversations from the current browser tab's current
  session. Nothing server-side was ever queried.

### Current authorization behavior (before this feature)

N/A for chat data specifically — there was no server-side chat data to
authorize access to. Every other authenticated route (`/execute`,
`/documents`, etc.) already enforced ownership/permissions correctly
(`agent/authz.py`, `api/authz.py`) and was unaffected by this gap.

### Current vector-retrieval behavior (before this feature)

Unrelated to, and unaffected by, the chat-history gap — the business-context
vector retrieval layer (`retrieval/`, built in an earlier session, see
`docs/vector-retrieval-design.md`) operates on schema/glossary/metric/
documentation chunks, never on chat history. Preserved as-is by this
feature; re-verified working after every change in this pass (see the
final validation section of this feature's implementation report).

---

## 4. Required changes (what this audit led to)

- **Database**: extend `identity.models.Conversation`/`Prompt`/`AiOutput`
  (already-existing but unused tables) with the fields needed for
  deterministic ordering and search — see `docs/chat-history-architecture.md`
  §2 for the full schema. New Alembic migration
  (`identity/migrations/versions/a1f3c9d84e21_...py`).
- **Backend**: a new repository layer
  (`identity/repositories/history.py`) implementing ownership-scoped
  conversation/message CRUD + search; a new router
  (`api/chat_history.py`) exposing it; a new glue module
  (`api/chat_persistence.py`) wiring `POST /ask` to actually persist a
  turn for a locally-authenticated caller.
- **Frontend**: `chatStore.ts` rewritten to hydrate its conversation list
  from the server (`GET /conversations`) and lazily fetch a conversation's
  messages (`GET /conversations/{id}/messages`) on open, instead of being
  the sole source of truth; `HistoryDrawer.tsx`'s search rewired to call
  `GET /chat/search`.
- **Migration strategy**: see §5 below — no data migration was needed,
  because there was no prior persisted data to migrate.

## 5. Migration strategy

**No migration of existing conversation/message data was performed or is
needed, and this is not an oversight — it follows directly from §3
above: there was no existing server-side chat data to migrate.** The
`identity.conversations`/`identity.prompts`/`identity.ai_outputs` tables
existed in the schema (from a prior session) but no code path anywhere in
the repository ever wrote to them before this feature — verified directly
by a repo-wide search for `identity.repositories.history` (the module
that would need to exist to use them), which returned nothing prior to
this pass. Given that, the real deployment's tables could only have
contained rows if something inserted them by hand outside the
application entirely, which this audit has no evidence of and treats as
not applicable. The only database change needed was therefore additive
(new columns + indexes on tables no application code had ever populated),
not a data-migration problem (no "identify records that can be safely
mapped to a user" step applies, since there are no ambiguous/unmapped/
anonymous records to sort through in the first place). Directly queried
after this feature's own work (and its own test data cleaned up):
`conversations`/`prompts`/`ai_outputs` hold only the rows this feature's
own live end-to-end testing created, all removed again afterward.

**[assumption]**: A different deployment of this codebase that had
already been running with an older, in-memory-only frontend for a long
time would have *nothing* to migrate either, by construction — chat data
never left the browser tab it was created in, so there is no
server-side row of any kind, anywhere, that this feature's migration could
have found and mapped. If a deployment separately exported/backed up
browser `localStorage` chat data through some out-of-band mechanism this
audit has no visibility into, importing it is out of scope for this pass
and not attempted.

## 6. Security risks identified and addressed

- **Client-supplied `user_id` trust** — checked directly: no endpoint in
  `api/chat_history.py` accepts a `user_id` field at all (Pydantic request
  models don't declare one, and `extra="forbid"` rejects an unexpected one
  outright — verified by a dedicated test,
  `tests/test_api_chat_history.py::TestOwnershipIsolation::test_client_supplied_user_id_is_ignored`).
- **Cross-user data leakage via a guessed/enumerated conversation id** —
  every read/write in `identity/repositories/history.py` folds `user_id`
  directly into its `WHERE` clause (never a separate permission check
  layered on top of an unscoped fetch); a non-owned conversation resolves
  to a 404, not a 403, so its existence is never confirmed to a caller who
  doesn't own it. Verified directly with real HTTP requests in
  `tests/test_api_chat_history.py::TestOwnershipIsolation` (4 tests) and,
  separately, live in a browser (a second registered account could not see
  the first account's conversation at all).
- **Search leaking another user's content** — `search_history`'s every
  query clause includes `Conversation.user_id == user_id`; verified with
  both a unit test (`tests/test_identity_repository_history.py
  ::TestSearchHistory::test_never_returns_another_users_results`) and a
  live cross-account browser check.

## 7. Assumptions and limitations

- **Only a locally-authenticated caller gets server-side chat history.**
  An OIDC-authenticated or fully-unauthenticated caller's questions are
  still answered exactly as before (`/ask` remains fully functional either
  way), but nothing is persisted for them — there is no corresponding row
  in `identity.users` to attach a conversation to. Extending this to OIDC
  callers would require a "shadow user" record keyed by the OIDC
  `subject` claim, deliberately not attempted in this pass (a real,
  separate design decision, not an oversight).
- **The reconstructed view of a reloaded past turn is not
  byte-for-byte identical to a live one.** Only the answer text and the
  generated SQL (if any) are persisted — not the full `AskResponse`
  (charts, per-source citations, the exact schema DDL shown at generation
  time). See `docs/chat-history-architecture.md`'s own "Known
  limitations" for the full disclosure and the reasoning behind this
  tradeoff.
- **This audit's "before" facts are current as of the state of the
  repository at the start of this feature's own work** — a prior session
  on this same repository had already added `identity/`, its
  authentication endpoints, and the (until-now-unused)
  `Conversation`/`Prompt`/`AiOutput` tables; this document does not
  re-audit decisions made in that earlier pass beyond what's directly
  relevant to chat history.
