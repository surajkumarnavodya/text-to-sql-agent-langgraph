# Chat History Search

How `GET /chat/search` and the search box work — see
`docs/chat-history-architecture.md` for the broader chat-history model this
sits on top of. **UI note:** the search box now lives in `Sidebar.tsx`
(the persistent left rail / mobile drawer), not the old combined
"history drawer" this document's examples below still say — that surface
was replaced 2026-09-18 (see `docs/chat-history-ui.md`,
`docs/navigation-and-actions.md`). The backend design below is unchanged
and was re-verified end-to-end, live, on 2026-09-19 —
`docs/functional-ui-audit.md` §2 has the full trace (request→auth→
ownership-scoped query→result→selecting a result loading the *correct*
conversation, not stale frontend state).

## Backend: `identity.repositories.history.search_history`

Searches, for the **authenticated caller only**:

- `conversations.title`
- `prompts.final_content` (the user's own questions)
- `ai_outputs.content` (the assistant's answers)

Every query clause includes `Conversation.user_id == user_id` directly —
never a filter applied after an unscoped fetch. Deleted conversations
(`deleted_at IS NOT NULL`) are always excluded.

### Query approach

Plain, portable `ILIKE '%term%'` via SQLAlchemy's `.ilike()` — deliberately
**not** PostgreSQL's `to_tsvector`/`plainto_tsquery` full-text search
operators. Two reasons:

1. **Portability with this repo's own test convention.** Every test under
   `tests/` runs against SQLite, no real PostgreSQL required
   (`tests/test_identity_repository_users.py`'s own docstring explains
   why). `.ilike()` compiles correctly on both engines; PostgreSQL-specific
   full-text operators would only be exercisable against the real
   database, meaning `search_history`'s actual query logic couldn't be
   unit-tested the way every other repository function in this codebase is.
2. **This app's own documented scale.** Full-text ranking
   (`ts_rank`/`ts_rank_cd`) earns its complexity at a corpus size and
   query-pattern diversity this project's own stated "single-user,
   local-dev-oriented" scale doesn't need yet.

Performance on the real PostgreSQL deployment comes from `pg_trgm` GIN
trigram indexes on the three searchable columns (added by this feature's
own migration, `identity/migrations/versions/a1f3c9d84e21_...py`) — a
leading-wildcard `ILIKE` cannot use a plain B-tree index at all; a trigram
index serves it efficiently. This is "using the database's existing
search features where appropriate" in the sense that matters here:
`pg_trgm` is a standard, bundled PostgreSQL extension (`CREATE EXTENSION
IF NOT EXISTS pg_trgm`), not a new search engine.

**Not evaluated/adopted**: a separate search engine (Elasticsearch,
Meilisearch, ...) — this app's own scale and existing PostgreSQL identity
database made that an unjustified new piece of infrastructure, per the
same "don't add infrastructure this project's stated scale doesn't need"
reasoning `docs/vector-retrieval-design.md` applies to its own
vector-database choice.

### Safety

- **Parameterized, never string-interpolated.** `.ilike(f"%{normalized}%")`
  goes through SQLAlchemy's parameter binding — the pattern string is a
  bound parameter, not concatenated SQL text.
- **Special characters** (`%`, `_`, `[`, `]`, ...) are *not* escaped before
  being placed inside the `LIKE` pattern — a user typing `50%` searches
  loosely (as a wildcard) rather than literally. This is a minor UX quirk
  (a broader-than-literal match), never a SQL-injection risk (still fully
  parameterized). Verified this doesn't error:
  `tests/test_identity_repository_history.py::TestSearchHistory::test_special_characters_do_not_error`.
- **Whitespace normalization**: `" ".join(query.split())` collapses
  runs of whitespace and trims before searching.
- **Case-insensitivity**: `.ilike()` on both engines.
- **Empty query**: returns `([], 0)` immediately, no query issued at all
  — not an error.

### Result shape

```python
@dataclass(frozen=True)
class ConversationSearchHit:
    conversation_id: uuid.UUID
    title: str | None
    matched_in: str          # "title" | "message"
    snippet: str             # a short, safe excerpt around the match
    message_id: uuid.UUID | None
    updated_at: datetime
```

De-duplicated to **one hit per conversation** (a message match's snippet
wins over a bare title match if a conversation has both), sorted by the
conversation's own recency (`last_message_at`/`created_at`) — the same
"most-recently-active first" ordering `list_conversations` already uses,
so search results and the plain conversation list feel consistent. This
is a recency proxy for relevance, not a ranking model — see the "no
full-text ranking" note above.

## API: `GET /chat/search`

```
GET /chat/search?q=<query>&limit=<n>&offset=<n>
```

Requires `Depends(require_local_user)`. `limit` is clamped server-side to
`Settings.chat_search_max_page_size` (default 50); an unset/zero `limit`
falls back to `Settings.chat_search_page_size` (default 20).

## Frontend: `HistoryDrawer.tsx`

- **Debounced** (300ms, `SEARCH_DEBOUNCE_MS`) — a search request fires
  once the user pauses typing, not on every keystroke.
- **Stale-response protection**: each search increments a request
  counter (`searchRequestId`); a response is only applied if its request
  id still matches the latest one issued — a fast typist's earlier,
  slower request can never overwrite a later, faster one's results.
- **Loading indicator**: a spinner replaces the search icon while a
  request is in flight.
- **Empty-result state**: a "no conversations match your search" message,
  distinct from the loading state.
- **Error state**: a request failure shows an inline message, not a
  silently empty list.
- **Clear-search control**: an "×" button inside the search field once
  there's text in it; pressing it resets to the unfiltered conversation
  list.
- **Keyboard**: `Escape` inside the search field clears it (without
  closing the whole drawer, unlike `Escape` everywhere else in the
  drawer); the search field auto-focuses when the drawer opens.
- **Selecting a result**: `loadConversation(hit.conversation_id)` —
  which, per `chatStore.ts`'s own contract, fetches the conversation's
  metadata from the server first if it isn't already in the locally
  hydrated list (e.g. an older conversation beyond `hydrateHistoryFromServer`'s
  own page-100 initial load), then its messages, then renders them. This
  is what makes "open a search result" work correctly even for a
  conversation that predates the current session's own history hydration.

### Not implemented (named, not silently skipped)

- **Matching-message highlighting inside the opened conversation** — the
  search result's own snippet shows the match; once a conversation is
  opened, the matching message isn't scrolled-to or visually highlighted
  within it. A reasonable follow-up, not attempted in this pass.
- **Infinite scroll for search results** — pagination exists at the API
  level (`limit`/`offset`), but the drawer's UI doesn't yet page through
  additional results; the first page (`Settings.chat_search_page_size`,
  default 20) is what's shown.
