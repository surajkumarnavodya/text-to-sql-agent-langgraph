# Chat History Architecture

How conversations and messages are stored, served, and rendered — server-side,
authenticated, and universal across browsers/devices for a locally-authenticated
user. See `docs/chat-history-authentication-audit.md` for the problem this
replaces and why.

## 1. Model

```
User (identity.users)
  -> many Conversations (identity.conversations)
      -> many Prompt/AiOutput pairs (identity.prompts / identity.ai_outputs)
```

`Prompt` (the user's question) and `AiOutput` (the assistant's answer) are
two separate, pre-existing tables (`identity/models.py`, added in an
earlier session but never previously used) rather than one unified
`messages` table — `identity/repositories/history.py` merges them into one
chronological, role-tagged view (`MessageRow`) for the API to serve. This
was a deliberate choice: `Prompt`/`AiOutput` already carry richer,
asymmetric fields (voice-correction tracking on `Prompt`; model/token/
latency/error metadata on `AiOutput`) that a single generic `role`+
`content` table would either lose or need extra columns for anyway — reusing
what already existed avoided a second migration to add a table that would
just duplicate their shape.

### Conversation fields

| Field | Purpose |
|---|---|
| `id` | UUID primary key, the permanent conversation identifier |
| `user_id` | FK to `users.id`, `ON DELETE CASCADE` — every query is scoped to this |
| `title` | Nullable; auto-derived from the first question if not set explicitly |
| `feature_type` | `"text_to_sql"` for chat-originated conversations |
| `status` | `"active"` (existing column; not yet used to distinguish more states) |
| `created_at` / `updated_at` | Standard timestamps |
| `last_message_at` | **New in this feature.** Set only when a message is actually appended — drives recency ordering without being disturbed by a pure metadata edit (a rename) |
| `archived_at` | **New in this feature.** Set/cleared via `PATCH /conversations/{id}` — an archived conversation is excluded from the default list but still fully readable/searchable |
| `deleted_at` | Soft-delete marker (pre-existing column, now actually used) |

### Message fields (via `Prompt`/`AiOutput`, merged into `MessageRow`)

| Field | Source | Purpose |
|---|---|---|
| `id` | `Prompt.id` / `AiOutput.id` | Row identity |
| `conversation_id` | both | FK |
| `role` | derived (`"user"` for `Prompt`, `"assistant"` for `AiOutput`) | |
| `content` | `Prompt.final_content` / `AiOutput.content` | The rendered text |
| `sequence_number` | **New in this feature**, on both tables | Deterministic ordering — see below |
| `created_at` | both | |
| `status` | `AiOutput.status` (`"completed"`/`"failed"`/...); always `"completed"` for a user row | |
| `model_name` | `AiOutput.model_name` | |
| `error_code` | `AiOutput.error_code` | |
| `metadata` | `AiOutput.metadata_json` — see "Universal conversation history" below | The full, bounded, versioned per-turn snapshot a reload is reconstructed from |

**Deterministic ordering**: `sequence_number` (new column on both
`prompts` and `ai_outputs`) is assigned once, inside
`identity.repositories.history.append_turn`'s single transaction, from
the conversation's own current max — a question always gets `n`, its
answer `n + 1`. This is what makes ordering correct even when two rows'
`created_at` timestamps could theoretically collide (unlikely on
PostgreSQL's microsecond-precision `TIMESTAMPTZ`, but not something
ordering should depend on regardless).

## 2. Indexes

Added by `identity/migrations/versions/a1f3c9d84e21_chat_history_search_and_ordering.py`:

- `ix_conversations_user_last_message` — `(user_id, last_message_at)`, the
  exact shape `list_conversations`'s "this user's conversations, most
  recent first" query filters and sorts on.
- `ix_prompts_conversation_sequence` / `ix_ai_outputs_conversation_sequence`
  — `(conversation_id, sequence_number)`, for `list_messages`'s
  per-conversation ordering.
- `ix_conversations_title_trgm` / `ix_prompts_final_content_trgm` /
  `ix_ai_outputs_content_trgm` — PostgreSQL `pg_trgm` GIN trigram
  indexes on the three searchable text columns, since a leading-wildcard
  `ILIKE '%term%'` (what `search_history` issues) cannot use a plain
  B-tree index at all. Skipped on a non-PostgreSQL bind (this repo's own
  SQLite test engine) — see the migration's own docstring.

## 3. API

All under `api/chat_history.py`, all requiring `Depends(require_local_user)`
(the same dependency `api/identity_auth.py`'s own self-service routes
use), all ownership-scoped inside the repository layer, never via a
separate permission check on top of an unscoped fetch:

| Method | Path | Purpose |
|---|---|---|
| GET | `/conversations` | Paginated list, most-recent-activity-first |
| POST | `/conversations` | Create (optional title) |
| GET | `/conversations/{id}` | Fetch one (404 if not owned/doesn't exist) |
| PATCH | `/conversations/{id}` | Rename and/or archive/unarchive |
| DELETE | `/conversations/{id}` | Soft-delete |
| GET | `/conversations/{id}/messages` | Paginated messages, chronological |
| POST | `/conversations/{id}/messages` | Direct single-message append (API completeness; not the real `/ask` path — see §4) |
| GET | `/chat/search?q=` | Search across this user's own conversations/messages |

A wrong/forged `conversation_id` in any of these routes resolves to
**404**, never a 403 — this never confirms to a caller whether another
user's conversation exists at all (the same account-enumeration-avoidance
principle `identity.exceptions.InvalidCredentialsError` already applies
to login in this codebase).

Pagination: `limit`/`offset`, server-side-clamped to
`Settings.chat_history_max_page_size`/`chat_search_max_page_size`
(a client can request a smaller page but never a larger one than the
configured cap) — a client-supplied `limit` of 0 or a missing value falls
back to `Settings.chat_history_page_size`/`chat_search_page_size`.

## 4. How a turn actually gets persisted

`POST /ask` (`api/main.py`) is unchanged in its core behavior (schema
retrieval, SQL generation/validation/execution all run exactly as
before) — after computing the final agent state, it calls
`api/chat_persistence.py::persist_ask_turn`, which:

1. No-ops immediately unless the caller authenticated via a **local**
   account (`AuthIdentity.mode == "local"`) — see §6's "Known
   limitations."
2. Resolves (or creates, on the first turn) the target `Conversation` —
   `AskRequest.conversation_id`, if supplied and owned by this caller;
   otherwise a fresh one.
3. Calls `identity.repositories.history.append_turn`, which persists the
   question (`Prompt`) and answer (`AiOutput`) together as one
   transaction, with adjacent `sequence_number`s.
4. Returns the conversation id, echoed back as `AskResponse.conversation_id`
   for the frontend to remember for the next turn.

**This can never fail an otherwise-successful `/ask` response** — every
step is wrapped in the route handler's own broad `except Exception`
(`api/chat_persistence.py`'s own module docstring explains why this is
the correct tradeoff: the SQL has already been generated/validated/
executed by the time persistence runs, and a transient identity-DB
hiccup is a completely separate failure class from "was the question
answered").

## 5. Frontend

`frontend/src/store/chatStore.ts` now:

- Calls `hydrateHistoryFromServer()` once after a local-auth sign-in
  succeeds (wired from `AuthGate.tsx`), populating `conversations` from
  `GET /conversations`.
- Lazily fetches a conversation's messages (`GET /conversations/{id}/messages`)
  the first time it's opened, converting them via
  `frontend/src/lib/history.ts::serverMessagesToQueryHistory` into the
  same `QueryHistoryEntry` shape a live, in-session turn produces.
- Migrates `activeConversationId` from a client-generated placeholder to
  the real server id the moment `POST /ask` returns one (the first turn
  of a new chat).
- `clearHistory()` — called on logout and on switching to a different
  local account (both wired from `AuthGate.tsx`) — clears only in-memory
  state, **never** calls a delete API. Server-side history is completely
  unaffected by logout, session expiry, a browser/device change, or an
  application restart (see §6).

`frontend/src/components/layout/HistoryDrawer.tsx`'s search debounces
into a real `GET /chat/search` call (see `docs/chat-history-search.md`)
whenever local-auth history is active; otherwise it falls back to the
original plain client-side title filter over the in-memory list —
unchanged from before this feature, for a deployment where local auth
isn't configured at all.

## 6. Retention behavior

- Logout: server-side history untouched (§5).
- Session expiration (access/refresh token expiry): untouched — proves
  identity only; the database is the source of truth for chat data,
  never the session (see this document's own framing: "sessions prove
  identity, the database stores universal chat history").
- Browser/device change: untouched, by construction (§1 — everything
  lives in PostgreSQL, keyed by `user_id`).
- Application restart: untouched (a durable database, not an in-memory
  cache).
- No silent TTL/automatic purge exists anywhere in this feature.
- Explicit deletion (`DELETE /conversations/{id}`) is soft (`deleted_at`)
  — the row remains in the database (for the same reasons an audit trail
  or accidental-deletion recovery matters), just excluded from every
  read path. **There is currently no user-facing "restore a deleted
  conversation" or "permanently purge" action** — both are named,
  deliberately out-of-scope follow-ups (see §7), not silently missing
  without acknowledgment.
- **[assumption]**: this project has no documented data-retention policy
  or legal/compliance retention limit as of this pass — if a deployment
  needs one, it isn't currently enforced anywhere in this codebase and
  would need to be added explicitly (e.g. a scheduled job checking
  `deleted_at`/`created_at` against a configured TTL) rather than assumed
  to exist.

## 6.5. Universal conversation history (2026-09-27)

Closes the limitation this section used to describe (reproduced below,
struck through, for anyone who read the earlier version): reopening a
saved conversation could show no assistant answer, an empty SQL editor
with "Confirm and Run", or `[object Object]` — traced to `api/
chat_persistence.py` only ever persisting `{"sql": "..."}"` (or nothing)
regardless of which source(s) actually answered, and the frontend
reconstruction (`frontend/src/lib/history.ts::serverMessagesToQueryHistory`)
fabricating a hardcoded `sources_used: []` for every reloaded turn, which
`TurnCard.tsx`'s own "empty means the SQL path" convention then
misinterpreted for every non-SQL saved answer.

`api/chat_persistence.py::_build_history_metadata` now snapshots a bounded,
redacted superset of the already-computed `AskResponse` into the exact same
`AiOutput.metadata_json` column — **no migration was needed**: it was
already an arbitrary-shape JSONB (JSON on SQLite) column with no fixed
schema (see `identity/models.py`'s own docstring), so a richer payload is a
pure application-layer change. A `schema_version` field (currently `2`)
lets the frontend tell a rich record from a pre-this-feature `{"sql": ...}`
-or-nothing legacy one and degrade gracefully rather than guess:

- **Real `sources_used`, `database`, `model`, `query_plan`, and a bounded
  `schema_tables` list** (table name + similarity score; DDL is dropped to
  keep stored size down) are persisted, so a reloaded document/policy/web/
  attachment/generation/media-search/mixed-source turn renders through the
  exact same `SourcesUsedPanel.tsx` a live turn uses — never the SQL panel.
- **`document_result`/`policy_result`/`web_result`/`generation_result`/
  `media_search_result`/`attachment_result`** are persisted verbatim
  (answer text bounded to `Settings.chat_history_max_text_chars`,
  citations kept as-is) — a reloaded multi-source answer shows every
  source that actually contributed, correctly labeled, not just plain text.
- **`persist_execute_result`** (new) updates an already-persisted turn's
  metadata with a bounded `result_snapshot` (rows capped at `Settings
  .chat_history_max_result_rows`, plus column types/chart recommendation/
  the actually-executed SQL/duration) once `POST /execute` succeeds for a
  turn whose `message_id` (a new `AskResponse` field, the persisted
  `AiOutput.id`) is known. Reopening a conversation whose SQL was already
  confirmed-and-run shows those exact rows and chart immediately — **the
  SQL is never re-executed just because a conversation was reopened**;
  `POST /execute` is the only thing that ever runs it, exactly as before.
- **Attachment references** (`attachment_refs`: id/filename/media-type,
  best-effort — see below) let a reloaded turn show which files were sent,
  without ever fabricating a preview for one that's since been evicted
  from the ephemeral, process-lifetime `AttachmentStore`.
- **Legacy records degrade gracefully, never inventing an answer**: a
  pre-this-feature row with `metadata: {"sql": "..."}"` still shows that
  SQL (the one part of the old behavior that already worked); a row with
  no recoverable structure at all falls back to its always-preserved
  `content` text, surfaced via a synthetic `'legacy'` source marker rather
  than showing nothing or an empty SQL editor.
- A separate, unrelated frontend bug (`frontend/src/lib/api.ts`'s
  `request()`) was fixed alongside this: a FastAPI 422 validation error's
  `detail` is an *array* of Pydantic error objects, not a string — naively
  assigning it as an `Error`'s message rendered as the literal text
  `[object Object]` wherever it was later displayed. `normalizeErrorDetail`
  now handles a string, an array of `{msg: ...}` objects, or any other
  shape safely.

~~**A reloaded past turn is not a byte-for-byte reconstruction of a live
one.** Only the answer text and, if the turn produced SQL, that SQL
(`AiOutput.metadata_json`) are persisted — not the full `AskResponse`
(charts, per-source citations for a multi-source answer, the exact
retrieved schema DDL, the query plan).~~ — superseded above. One honest
remaining gap: the retrieved schema's *DDL* itself (not just table names)
and a chart's own free-text customization (title/axis labels, entered
client-side, never sent to the backend) are still not restored on reload —
disclosed, not silently dropped.

## 7. Known limitations

- **Only local accounts get server-side history** — see
  `docs/chat-history-authentication-audit.md` §7.
- **No conversation-restore or permanent-purge UI** — soft-delete only,
  as noted in §6.
- **`POST /conversations/{id}/messages` (direct single-message append)
  has no `sequence_number` pairing guarantee with an assistant reply** —
  it's meant for API completeness/integration testing, not the real
  persistence path (`/ask` always writes a question+answer pair
  together via `append_turn`).
- **No streaming-response persistence** — `POST /ask` in this codebase is
  a synchronous request/response, not a streaming endpoint, so "avoid
  duplicate messages during a streamed retry" doesn't apply; a genuine
  network retry of the same `/ask` call today would create a second
  conversation turn (no idempotency key exists for `/ask` itself, a
  pre-existing characteristic of this endpoint this feature did not
  change).
