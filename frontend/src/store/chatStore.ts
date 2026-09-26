import { create } from 'zustand'
import {
  ApiError,
  askQuestion as apiAskQuestion,
  confirmGeneration as apiConfirmGeneration,
  executeSql,
  submitGoldenExampleFeedback,
  submitMessageFeedback,
  synthesizeSpeechUrl,
} from '@/lib/api'
import {
  buildAnswerMarkdown,
  buildConversationHistory,
  buildSpokenAnswerText,
  deriveConversationTitle,
  newHistoryEntry,
  nlCacheKey,
  replaceEntry,
  serverMessagesToQueryHistory,
  withChartOptions,
  withConfirmedError,
  withConfirmedResult,
  withGenerationResult,
  withSpokenAudio,
  type ConversationSummary,
  type QueryHistoryEntry,
  type SentAttachmentPreview,
} from '@/lib/history'
import {
  CHART_TYPE_LABELS,
  defaultSeriesForType,
  getChartTypeOptions,
  inferColumnRoles,
  type ChartOptions,
} from '@/lib/chartEngine'
import { detectChartTypeSwitchRequest } from '@/lib/chartFollowup'
import {
  deleteConversationOnServer,
  getConversation as apiGetConversation,
  listConversations as apiListConversations,
  listMessages as apiListMessages,
  renameConversationOnServer,
} from '@/lib/identityApi'
import type { AskResponse } from '@/lib/types'
import { useLocalAuthStore } from './localAuthStore'

/** The question currently in flight -- rendered by
 * `components/chat/PendingTurn.tsx` as the next item in the normal
 * scrolling conversation (a user bubble plus `TimingBadge`'s pulsing
 * "Thinking Ns…" text), not a popup/overlay. `startedAt` is a
 * `performance.now()` timestamp, not wall-clock, so the live counter is
 * immune to system clock changes mid-request. */
export interface PendingQuestion {
  question: string
  startedAt: number
}

/** Result of `tryApplyChartTypeFollowup` -- see that action's own doc for
 * what each outcome means to the caller (`ChatInput.tsx`). */
export type ChartFollowupOutcome =
  | { handled: false }
  | { handled: true; applied: true; message: string }
  | { handled: true; applied: false; message: string }

interface ChatState {
  queryHistory: QueryHistoryEntry[]
  pendingQuestion: PendingQuestion | null
  confirmingEntryId: string | null
  confirmingGenerationEntryId: string | null
  goldenFeedbackGiven: Set<string>
  /** Last like/dislike rating submitted per entry (see
   * ResponseFeedbackWidget.tsx) -- a Map, not a Set, since (unlike
   * goldenFeedbackGiven above) a rating can be changed after the fact, and
   * the UI needs to know *which* one is currently selected to highlight
   * it. */
  messageFeedbackGiven: Map<string, 'positive' | 'negative'>
  enableInsight: boolean
  nlQuestionCache: Map<string, AskResponse>

  /** The conversation currently shown on the Chat page. Every conversation
   * this session knows about (including the active one) lives in
   * `conversations`, keyed by id -- the history drawer reads that map,
   * `queryHistory` above is just "whichever one is on screen right now".
   * For a locally-authenticated user this map is hydrated from, and kept
   * in sync with, the server (`identityApi.ts`'s conversation endpoints)
   * -- see `hydrateHistoryFromServer`/`loadConversation` below and
   * `docs/chat-history-architecture.md`. */
  activeConversationId: string
  conversations: Record<string, ConversationSummary>
  /** True once the server conversation list has loaded at least once this
   * session (local auth only) -- lets the history panel show a genuine
   * "loading" state on first mount instead of flashing an empty-state
   * message before the real list arrives. */
  isHistoryHydrated: boolean
  isLoadingConversation: boolean
  historyError: string | null

  setEnableInsight: (value: boolean) => void
  setEditableSql: (entryId: string, sql: string) => void
  /** Sets (generate/customize) or clears (remove, pass `null`) one turn's
   * user-built chart -- see ChartSection.tsx. Purely local/session state,
   * like confirmedColumns/confirmedRows themselves. */
  setChartOptions: (entryId: string, chartOptions: ChartOptions | null) => void
  /** Detects a lightweight NL follow-up asking to switch the *most recent*
   * chartable turn's chart type ("show this as a pie chart", "switch to
   * line", "bar chart instead") and, if the requested type is genuinely
   * valid for that turn's actual result (re-checked via
   * `getChartTypeOptions`, never assumed), applies it directly via
   * `setChartOptions` -- no `/ask` call, no SQL re-run. `{ handled: false }`
   * means `text` didn't read as a chart-followup at all, so the caller
   * should fall through to a normal `askQuestion`. `{ handled: true,
   * applied: false, message }` means it *did* read as one but couldn't be
   * applied (no chartable turn yet, or the type doesn't fit this result's
   * shape) -- the caller should show `message` and stop, since sending
   * that text on to the SQL agent as if it were a real question would be
   * meaningless. */
  tryApplyChartTypeFollowup: (text: string) => ChartFollowupOutcome
  /** Returns the finished entry (including `spokenAudioUrl`, if speech
   * synthesis for a voice-originated turn succeeded) so a caller that
   * needs to react to the *result* -- `useVoiceConversation`'s hands-free
   * loop, specifically -- can `await` it directly instead of subscribing
   * to `queryHistory` and diffing for the new entry itself. */
  askQuestion: (
    question: string,
    options?: {
      originatedFromVoice?: boolean
      attachmentIds?: string[]
      /** Snapshot of the attached files, for the sent question's own chat
       * bubble -- see `SentAttachmentPreview`'s own docstring. */
      sentAttachments?: SentAttachmentPreview[]
    },
  ) => Promise<QueryHistoryEntry>
  confirmAndRun: (entryId: string) => Promise<void>
  confirmGeneration: (entryId: string) => Promise<void>
  rerunEntry: (entryId: string) => Promise<void>
  giveGoldenFeedback: (entryId: string, thumbsUp: boolean) => Promise<void>
  /** General like/dislike + optional comment on any answer (SQL,
   * document/policy RAG, web search, media) -- see
   * ResponseFeedbackWidget.tsx. Independent of giveGoldenFeedback above,
   * which only ever saves a confirmed SQL result as a few-shot example; a
   * positive rating here on a turn that also has a confirmedSql still
   * triggers that same save, additively. */
  giveMessageFeedback: (
    entryId: string,
    rating: 'positive' | 'negative',
    comment: string | null,
  ) => Promise<void>
  /** Aborts the in-flight `/ask` request started by the current
   * `pendingQuestion`, if any -- a no-op if nothing is pending, or if it
   * already settled. There is no real token-by-token stream to interrupt
   * (see docs/frontend-ui-audit.md's "no streaming" finding); this cancels
   * the single outstanding HTTP request via AbortController, which is what
   * "Stop" can honestly mean here. */
  cancelPendingQuestion: () => void

  startNewChat: () => void
  /** Async: for a server-backed conversation whose messages haven't been
   * fetched yet, this loads them from the server first (see
   * `ConversationSummary.messagesLoaded`) -- callers that don't need to
   * wait for that (e.g. a plain click handler) can call it without
   * awaiting, `isLoadingConversation` drives the loading UI either way. */
  loadConversation: (id: string) => Promise<void>
  renameConversation: (id: string, title: string) => Promise<void>
  deleteConversation: (id: string) => Promise<void>
  /** Fetches the authenticated user's conversation list from the server --
   * called once after a local-auth login/session-restore succeeds
   * (`AuthGate.tsx`), and safe to call again any time (e.g. a manual
   * refresh). A no-op, not an error, when local auth isn't the active
   * credential -- see this function's own guard. */
  hydrateHistoryFromServer: () => Promise<void>
  /** Clears every in-memory conversation/message/pending-turn — called on
   * logout and on switching to a different authenticated user. Never
   * calls a delete API: this only clears what THIS browser tab is holding
   * in memory, the server-side history itself is untouched (see
   * `docs/chat-history-architecture.md`'s "chat history must not be
   * deleted on logout" requirement). */
  clearHistory: () => void
}

/** Every mutation to `queryHistory` goes through this so the active entry
 * in `conversations` never drifts out of sync with what's on screen --
 * the history drawer and the Chat page are reading two different fields
 * pointed at the same data, not two independently-updated copies of it. */
function commitQueryHistory(
  state: Pick<ChatState, 'activeConversationId' | 'conversations'>,
  queryHistory: QueryHistoryEntry[],
): Pick<ChatState, 'queryHistory' | 'conversations'> {
  if (queryHistory.length === 0) return { queryHistory, conversations: state.conversations }
  const existing = state.conversations[state.activeConversationId]
  return {
    queryHistory,
    conversations: {
      ...state.conversations,
      [state.activeConversationId]: {
        id: state.activeConversationId,
        title: existing?.title ?? deriveConversationTitle(queryHistory[0].question),
        updatedAt: new Date().toISOString(),
        entries: queryHistory,
        messagesLoaded: true,
      },
    },
  }
}

function freshConversationState(): Pick<
  ChatState,
  'activeConversationId' | 'queryHistory' | 'pendingQuestion' | 'confirmingEntryId' | 'confirmingGenerationEntryId'
> {
  return {
    activeConversationId: crypto.randomUUID(),
    queryHistory: [],
    pendingQuestion: null,
    confirmingEntryId: null,
    confirmingGenerationEntryId: null,
  }
}

function emptyAskResponse(message: string): AskResponse {
  return {
    session_id: '',
    conversation_id: null,
    status: 'failed',
    database: null,
    sql: null,
    result_columns: null,
    result_rows: null,
    row_count: null,
    retry_count: 0,
    attempt_history: [],
    insight: null,
    cost_notice: null,
    low_confidence_notice: null,
    rejection_reason: null,
    rejection_message: null,
    rate_limit_message: null,
    clarification_message: null,
    failure_explanation: message,
    error_history: [message],
    sources_used: [],
    synthesized_answer: null,
    document_result: null,
    policy_result: null,
    web_result: null,
    generation_result: null,
    media_search_result: null,
    attachment_result: null,
    query_plan: null,
    schema_tables: [],
    followup_classification: null,
    followup_resolved_against: null,
    permission_denied_notice: null,
  }
}

/** Whether server-side chat history applies at all right now -- see every
 * exported function/action in this file that checks it before touching
 * the network. False for a plain no-auth/OIDC-only deployment, or before
 * a local-auth sign-in has completed; in that case this store behaves
 * exactly as it did before this feature existed (a purely in-memory,
 * session-only conversation list). */
function isServerHistoryActive(): boolean {
  return useLocalAuthStore.getState().status === 'authenticated'
}

/** The AbortController for whichever `/ask` call is currently in flight --
 * plain module-level plumbing, not reactive Zustand state, since no
 * component needs to re-render when *this specific object's identity*
 * changes; components only care about `pendingQuestion` (already
 * reactive) and call `cancelPendingQuestion()` to act on this. */
let currentAbortController: AbortController | null = null

export const useChatStore = create<ChatState>((set, get) => ({
  queryHistory: [],
  pendingQuestion: null,
  confirmingEntryId: null,
  confirmingGenerationEntryId: null,
  goldenFeedbackGiven: new Set(),
  messageFeedbackGiven: new Map(),
  enableInsight: true,
  nlQuestionCache: new Map(),
  activeConversationId: crypto.randomUUID(),
  conversations: {},
  isHistoryHydrated: false,
  isLoadingConversation: false,
  historyError: null,

  setEnableInsight: (value) => set({ enableInsight: value }),

  setEditableSql: (entryId, sql) =>
    set((state) =>
      commitQueryHistory(
        state,
        state.queryHistory.map((entry) => (entry.entryId === entryId ? { ...entry, editableSql: sql } : entry)),
      ),
    ),

  setChartOptions: (entryId, chartOptions) =>
    set((state) =>
      commitQueryHistory(
        state,
        state.queryHistory.map((entry) =>
          entry.entryId === entryId ? withChartOptions(entry, chartOptions) : entry,
        ),
      ),
    ),

  tryApplyChartTypeFollowup: (text) => {
    const requestedType = detectChartTypeSwitchRequest(text)
    if (!requestedType) return { handled: false }

    const target = [...get().queryHistory]
      .reverse()
      .find((entry) => entry.confirmedColumns && entry.confirmedRows && entry.confirmedRows.length > 0)
    if (!target || !target.confirmedColumns || !target.confirmedRows) {
      return { handled: true, applied: false, message: 'No chart yet -- run a query first.' }
    }

    const columnInfos = inferColumnRoles(
      target.confirmedColumns,
      target.confirmedRows,
      target.confirmedColumnTypes ?? {},
    )
    const match = getChartTypeOptions(columnInfos, target.confirmedRows).find((o) => o.type === requestedType)
    if (!match?.enabled) {
      return {
        handled: true,
        applied: false,
        message: match?.reason ?? `${CHART_TYPE_LABELS[requestedType]} isn't valid for this result.`,
      }
    }

    const base = target.chartOptions
    const nextOptions: ChartOptions = {
      chartType: requestedType,
      series: defaultSeriesForType(requestedType, columnInfos),
      sortOrder: base?.sortOrder ?? 'none',
      topN: base?.topN ?? (target.confirmedRows.length > 20 ? 20 : null),
      dateGrouping: base?.dateGrouping ?? 'none',
      numberFormat: base?.numberFormat ?? 'plain',
      title: base?.title ?? '',
      showLegend: base?.showLegend ?? true,
      stacked: base?.stacked ?? false,
    }
    get().setChartOptions(target.entryId, nextOptions)
    return { handled: true, applied: true, message: `Switched the chart to ${CHART_TYPE_LABELS[requestedType]}.` }
  },

  askQuestion: async (question, options) => {
    const { queryHistory, enableInsight, nlQuestionCache, activeConversationId, conversations } = get()
    const startedAt = performance.now()
    set({ pendingQuestion: { question, startedAt } })

    const attachmentIds = options?.attachmentIds ?? []
    const priorQuestions = queryHistory.map((entry) => entry.question)
    const cacheKey = nlCacheKey(question, priorQuestions, enableInsight)
    // A cached answer was computed without whatever attachment(s) this call
    // carries -- reusing it would silently ignore the attachment, so the
    // cache is skipped entirely whenever one is present rather than trying
    // to fold attachment_ids into the cache key for a case that's cheap to
    // just always re-ask.
    const cached = attachmentIds.length === 0 ? nlQuestionCache.get(cacheKey) : undefined
    // Only pass conversation_id once the server has actually confirmed this
    // id exists (i.e. we've already gotten it back from a prior /ask call,
    // or it came from hydrateHistoryFromServer) -- a brand-new, purely
    // client-generated id must never be sent as if it were a real
    // server-side conversation, since /ask would just silently start a new
    // one anyway (see api/chat_persistence.py) but sending it needlessly
    // muddies the request.
    const knownServerConversationId = conversations[activeConversationId]?.messagesLoaded
      ? activeConversationId
      : undefined

    const controller = new AbortController()
    currentAbortController = controller

    let finalState: AskResponse
    try {
      finalState =
        cached ??
        (await apiAskQuestion(
          {
            question,
            conversation_history: buildConversationHistory(queryHistory),
            enable_insight: enableInsight,
            conversation_id: knownServerConversationId,
            attachment_ids: attachmentIds,
          },
          controller.signal,
        ))
    } catch (error) {
      const message =
        error instanceof DOMException && error.name === 'AbortError'
          ? 'Cancelled.'
          : error instanceof ApiError
            ? error.message
            : 'The agent could not be reached.'
      finalState = emptyAskResponse(message)
    } finally {
      // Only clear the module-level slot if it's still pointing at *this*
      // call's controller -- a second askQuestion() could already have
      // started (and installed its own controller) while this one was
      // still in flight, and clearing unconditionally here would let a
      // later Stop click silently do nothing.
      if (currentAbortController === controller) currentAbortController = null
    }

    const answerDurationMs = performance.now() - startedAt
    const entry = newHistoryEntry(
      question,
      finalState,
      answerDurationMs,
      options?.originatedFromVoice ?? false,
      options?.sentAttachments ?? [],
    )

    // If the server assigned/confirmed a real conversation id (a
    // locally-authenticated caller), and it differs from the client-side
    // placeholder id this chat started under, migrate the active
    // conversation's key so every subsequent action (rename, delete,
    // loadConversation) operates on the real, permanent id.
    const serverConversationId = finalState.conversation_id
    set((state) => {
      const withHistory = commitQueryHistory(state, [...state.queryHistory, entry])
      if (!serverConversationId || serverConversationId === state.activeConversationId) {
        return { ...withHistory, pendingQuestion: null }
      }
      const migrated = { ...withHistory.conversations }
      const current = migrated[state.activeConversationId]
      delete migrated[state.activeConversationId]
      if (current) migrated[serverConversationId] = { ...current, id: serverConversationId, messagesLoaded: true }
      return {
        conversations: migrated,
        queryHistory: withHistory.queryHistory,
        activeConversationId: serverConversationId,
        pendingQuestion: null,
      }
    })
    set((state) => ({
      // Never caches an attachment-informed answer under the bare question
      // text -- see the attachmentIds/cached comment above for why a later
      // identical-looking text-only (or differently-attached) question must
      // not silently reuse it.
      nlQuestionCache:
        cached || attachmentIds.length > 0
          ? state.nlQuestionCache
          : new Map(state.nlQuestionCache).set(cacheKey, finalState),
    }))

    // Spoken answer synthesis: only ever for a voice-originated turn that
    // actually succeeded -- a typed question never reaches this branch,
    // so it can never trigger audio output (see QueryHistoryEntry
    // .originatedFromVoice's docstring). A failure here leaves the
    // (already-shown) text answer as the only output, never blocks or
    // fails the turn itself -- the caller still gets `entry` back either way.
    let finalEntry = entry
    if (entry.originatedFromVoice && finalState.status === 'succeeded') {
      try {
        const audioUrl = await synthesizeSpeechUrl(buildSpokenAnswerText(entry))
        finalEntry = withSpokenAudio(entry, audioUrl)
        set((state) => commitQueryHistory(state, replaceEntry(state.queryHistory, entry.entryId, finalEntry)))
      } catch {
        // No spoken playback for this turn; the text answer already
        // rendered is sufficient, so this is a silent, non-fatal miss.
      }
    }
    return finalEntry
  },

  confirmAndRun: async (entryId) => {
    const entry = get().queryHistory.find((item) => item.entryId === entryId)
    if (!entry) return
    set({ confirmingEntryId: entryId })
    const startedAt = performance.now()
    try {
      const response = await executeSql({ sql: entry.editableSql, database: entry.finalState.database })
      const durationMs = performance.now() - startedAt
      const updated =
        response.status === 'succeeded'
          ? withConfirmedResult(
              { ...entry, editableSql: response.normalized_sql ?? entry.editableSql },
              response.result_columns ?? [],
              response.result_rows ?? [],
              response.normalized_sql ?? entry.editableSql,
              response.column_types,
              response.chart_recommendation,
              response.truncated,
              durationMs,
            )
          : withConfirmedError(entry, response.error ?? 'Execution failed.')
      set((state) => commitQueryHistory(state, replaceEntry(state.queryHistory, entryId, updated)))
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'Execution failed unexpectedly.'
      const updated = withConfirmedError(entry, message)
      set((state) => commitQueryHistory(state, replaceEntry(state.queryHistory, entryId, updated)))
    } finally {
      set({ confirmingEntryId: null })
    }
  },

  confirmGeneration: async (entryId) => {
    const entry = get().queryHistory.find((item) => item.entryId === entryId)
    if (!entry) return
    set({ confirmingGenerationEntryId: entryId })
    try {
      const result = await apiConfirmGeneration(entry.question)
      set((state) => commitQueryHistory(state, replaceEntry(state.queryHistory, entryId, withGenerationResult(entry, result))))
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'Generation failed unexpectedly.'
      const priorType = entry.finalState.generation_result?.media_type ?? null
      const updated = withGenerationResult(entry, {
        answer: message,
        status: 'failed',
        media_id: null,
        media_type: priorType,
        model: null,
      })
      set((state) => commitQueryHistory(state, replaceEntry(state.queryHistory, entryId, updated)))
    } finally {
      set({ confirmingGenerationEntryId: null })
    }
  },

  rerunEntry: async (entryId) => {
    const entry = get().queryHistory.find((item) => item.entryId === entryId)
    if (!entry) return
    await get().askQuestion(entry.question)
  },

  giveGoldenFeedback: async (entryId, thumbsUp) => {
    const entry = get().queryHistory.find((item) => item.entryId === entryId)
    set((state) => ({ goldenFeedbackGiven: new Set(state.goldenFeedbackGiven).add(entryId) }))
    if (!thumbsUp || !entry?.confirmedSql) return
    await submitGoldenExampleFeedback({
      question: entry.question,
      sql: entry.confirmedSql,
      database: entry.finalState.database ?? 'default',
    })
  },

  giveMessageFeedback: async (entryId, rating, comment) => {
    const entry = get().queryHistory.find((item) => item.entryId === entryId)
    if (!entry) return
    set((state) => ({
      messageFeedbackGiven: new Map(state.messageFeedbackGiven).set(entryId, rating),
    }))
    const answerState = entry.finalState
    await submitMessageFeedback({
      question: entry.question,
      answer: buildAnswerMarkdown(entry),
      rating,
      sql: entry.confirmedSql ?? entry.sql,
      database: answerState.database ?? null,
      sources_used: answerState.sources_used,
      comment,
      conversation_id: answerState.conversation_id ?? get().activeConversationId,
    })
    // A positive rating on a confirmed SQL result also feeds the few-shot
    // golden dataset -- the exact same side effect giveGoldenFeedback's own
    // thumbs-up path has, kept additive rather than merged into one
    // function so each store stays independently testable (see
    // feedback/store.py's and embeddings/golden_examples.py's own
    // docstrings for why they're two stores, not one).
    if (rating === 'positive' && entry.confirmedSql) {
      await submitGoldenExampleFeedback({
        question: entry.question,
        sql: entry.confirmedSql,
        database: answerState.database ?? 'default',
      })
    }
  },

  cancelPendingQuestion: () => currentAbortController?.abort(),

  startNewChat: () => set(freshConversationState()),

  loadConversation: async (id) => {
    let existing = get().conversations[id]
    // A search result can name a conversation that hydrateHistoryFromServer's
    // own (capped) initial page didn't include -- fetch its metadata on
    // demand rather than silently no-oping, so opening a search hit always
    // works regardless of how far back it is in the user's full history.
    if (!existing && isServerHistoryActive()) {
      set({ isLoadingConversation: true, historyError: null })
      try {
        const fetched = await apiGetConversation(id)
        existing = {
          id: fetched.id,
          title: fetched.title ?? 'New conversation',
          updatedAt: fetched.last_message_at ?? fetched.created_at,
          entries: [],
          messagesLoaded: false,
        }
        set((state) => ({ conversations: { ...state.conversations, [id]: existing! } }))
      } catch (error) {
        const message = error instanceof ApiError ? error.message : 'Could not load this conversation.'
        set({ historyError: message, isLoadingConversation: false })
        return
      }
    }
    if (!existing) return
    if (existing.messagesLoaded || !isServerHistoryActive()) {
      set({ ...freshConversationState(), activeConversationId: id, queryHistory: existing.entries })
      return
    }

    // Show the conversation immediately with whatever's cached (possibly
    // empty) while its real messages load, rather than a blank screen.
    set({ ...freshConversationState(), activeConversationId: id, queryHistory: existing.entries })
    set({ isLoadingConversation: true, historyError: null })
    try {
      const response = await apiListMessages(id)
      const entries = serverMessagesToQueryHistory(response.messages)
      set((state) => {
        // The user may have navigated away from this conversation while the
        // fetch was in flight -- only apply the result if it's still the
        // active one, so a late response can never clobber what's on screen.
        if (state.activeConversationId !== id) return state
        return {
          queryHistory: entries,
          conversations: {
            ...state.conversations,
            [id]: { ...state.conversations[id], entries, messagesLoaded: true },
          },
        }
      })
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'Could not load this conversation.'
      set({ historyError: message })
    } finally {
      set({ isLoadingConversation: false })
    }
  },

  renameConversation: async (id, title) => {
    const conversation = get().conversations[id]
    if (!conversation) return
    const trimmed = title.trim()
    if (!trimmed) return
    set((state) => ({
      conversations: { ...state.conversations, [id]: { ...conversation, title: trimmed } },
    }))
    if (!isServerHistoryActive() || !conversation.messagesLoaded) return
    try {
      await renameConversationOnServer(id, trimmed)
    } catch {
      // The optimistic local rename already applied; a failed server-side
      // rename is a minor, silently-degraded UX miss (the title reverts on
      // next hydrate), never worth surfacing as a blocking error for a
      // rename action.
    }
  },

  deleteConversation: async (id) => {
    const wasActive = get().activeConversationId === id
    set((state) => {
      const remaining = { ...state.conversations }
      delete remaining[id]
      if (!wasActive) return { conversations: remaining }
      return { ...freshConversationState(), conversations: remaining }
    })
    if (!isServerHistoryActive()) return
    try {
      await deleteConversationOnServer(id)
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'Could not delete this conversation.'
      set({ historyError: message })
    }
  },

  hydrateHistoryFromServer: async () => {
    if (!isServerHistoryActive()) return
    set({ historyError: null })
    try {
      const response = await apiListConversations({ limit: 100 })
      set((state) => {
        const next: Record<string, ConversationSummary> = {}
        for (const conversation of response.conversations) {
          const existing = state.conversations[conversation.id]
          next[conversation.id] = {
            id: conversation.id,
            title: conversation.title ?? 'New conversation',
            updatedAt: conversation.last_message_at ?? conversation.created_at,
            entries: existing?.entries ?? [],
            messagesLoaded: existing?.messagesLoaded ?? false,
          }
        }
        return { conversations: next, isHistoryHydrated: true }
      })
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'Could not load your chat history.'
      set({ historyError: message, isHistoryHydrated: true })
    }
  },

  clearHistory: () =>
    set({
      ...freshConversationState(),
      conversations: {},
      isHistoryHydrated: false,
      historyError: null,
      nlQuestionCache: new Map(),
      goldenFeedbackGiven: new Set(),
      messageFeedbackGiven: new Map(),
    }),
}))
