# New Chat Flow

See `docs/functional-ui-audit.md` §3 for the full investigation. This
document states the chosen, implemented behavior plainly.

## Chosen model: lazy persistence (Option B)

```
1. User clicks New Chat.
2. chatStore.startNewChat() resets local state to a fresh, unsaved
   conversation (a new client-side placeholder id via crypto.randomUUID()).
   No server call is made.
3. Composer is empty; previous conversation's turns are gone from the
   view but untouched in `conversations` (the sidebar's own data).
4. User sends the first message. askQuestion() omits `conversation_id`
   from the POST /ask payload (the placeholder id has never been
   confirmed real by the server).
5. api/chat_persistence.py creates exactly one server-side conversation
   and returns its real id in AskResponse.conversation_id.
6. chatStore migrates the local placeholder id to the real server id
   (conversations[placeholder] deleted, conversations[realId] created)
   so every subsequent action (rename, delete, search, reload) operates
   on the real, permanent id.
7. Sidebar reflects the new conversation (via the same `conversations`
   map, keyed under its now-real id).
```

**Why lazy, not immediate persistence**: clicking New Chat never creates a
database row for a conversation the user might abandon without typing
anything — a real, if minor, cleanliness property immediate persistence
wouldn't have. It also makes New Chat itself trivially safe to click any
number of times (see below), since it has zero server side effect.

## Duplicate-creation safety

- **New Chat itself**: no API call → clicking it any number of times,
  including a genuine double-click, cannot create a duplicate. Locked in
  by `Sidebar.test.tsx`'s *"New Chat never calls a server API..."* test.
- **The first message send** (the one point that actually creates a
  server conversation): guarded by `ChatInput`'s existing `pendingQuestion`
  state, set synchronously the instant `askQuestion` starts — the Send
  button's real DOM `disabled` attribute flips before any await, so a
  rapid second click/Enter cannot fire a second request. Verified live
  (button disabled 50ms after the first click; exactly one `POST /ask`
  observed).

## What's preserved across the flow

- Old conversations remain in the sidebar and are unaffected by New Chat.
- New Chat works identically after search, after a failed prior response,
  after login, and from the collapsed icon-rail sidebar.
- The composer is usable immediately (no async gate before typing).
- The new conversation survives a page refresh once it's actually been
  persisted (i.e., after the first message succeeds) — `hydrateHistoryFromServer`
  re-fetches the full conversation list on load, same as any other
  conversation.

## Not implemented (disclosed, not attempted)

- Explicit loading/error UI *for New Chat specifically* — since it has no
  network call, there's nothing to show a loading state for. The
  first-message send already has its own loading (`pendingQuestion` →
  `PendingTurn.tsx`) and error handling, reused rather than duplicated.
