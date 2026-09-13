import { create } from 'zustand'
import {
  ApiError,
  askQuestion as apiAskQuestion,
  confirmGeneration as apiConfirmGeneration,
  executeSql,
  submitGoldenExampleFeedback,
  synthesizeSpeechUrl,
} from '@/lib/api'
import {
  buildConversationHistory,
  buildSpokenAnswerText,
  deriveConversationTitle,
  newHistoryEntry,
  nlCacheKey,
  replaceEntry,
  withConfirmedError,
  withConfirmedResult,
  withGenerationResult,
  withSpokenAudio,
  type ConversationSummary,
  type QueryHistoryEntry,
} from '@/lib/history'
import type { AskResponse } from '@/lib/types'

/** The question currently in flight -- drives the full-screen centered
 * "Thinking Ns" takeover (see components/chat/ThinkingOverlay.tsx and
 * TimingBadge.tsx), not an inline chat bubble. `startedAt` is a
 * `performance.now()` timestamp, not wall-clock, so the live counter is
 * immune to system clock changes mid-request. */
export interface PendingQuestion {
  question: string
  startedAt: number
}

interface ChatState {
  queryHistory: QueryHistoryEntry[]
  pendingQuestion: PendingQuestion | null
  confirmingEntryId: string | null
  confirmingGenerationEntryId: string | null
  goldenFeedbackGiven: Set<string>
  enableInsight: boolean
  nlQuestionCache: Map<string, AskResponse>

  /** The conversation currently shown on the Chat page. Every conversation
   * ever started this session (including the active one) lives in
   * `conversations`, keyed by id -- the history drawer reads that map,
   * `queryHistory` above is just "whichever one is on screen right now". */
  activeConversationId: string
  conversations: Record<string, ConversationSummary>

  setEnableInsight: (value: boolean) => void
  setEditableSql: (entryId: string, sql: string) => void
  /** Returns the finished entry (including `spokenAudioUrl`, if speech
   * synthesis for a voice-originated turn succeeded) so a caller that
   * needs to react to the *result* -- `useVoiceConversation`'s hands-free
   * loop, specifically -- can `await` it directly instead of subscribing
   * to `queryHistory` and diffing for the new entry itself. */
  askQuestion: (
    question: string,
    options?: { originatedFromVoice?: boolean },
  ) => Promise<QueryHistoryEntry>
  confirmAndRun: (entryId: string) => Promise<void>
  confirmGeneration: (entryId: string) => Promise<void>
  rerunEntry: (entryId: string) => Promise<void>
  giveGoldenFeedback: (entryId: string, thumbsUp: boolean) => Promise<void>

  startNewChat: () => void
  loadConversation: (id: string) => void
  renameConversation: (id: string, title: string) => void
  deleteConversation: (id: string) => void
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
    query_plan: null,
    schema_tables: [],
    followup_classification: null,
    followup_resolved_against: null,
  }
}

export const useChatStore = create<ChatState>((set, get) => ({
  queryHistory: [],
  pendingQuestion: null,
  confirmingEntryId: null,
  confirmingGenerationEntryId: null,
  goldenFeedbackGiven: new Set(),
  enableInsight: true,
  nlQuestionCache: new Map(),
  activeConversationId: crypto.randomUUID(),
  conversations: {},

  setEnableInsight: (value) => set({ enableInsight: value }),

  setEditableSql: (entryId, sql) =>
    set((state) =>
      commitQueryHistory(
        state,
        state.queryHistory.map((entry) => (entry.entryId === entryId ? { ...entry, editableSql: sql } : entry)),
      ),
    ),

  askQuestion: async (question, options) => {
    const { queryHistory, enableInsight, nlQuestionCache } = get()
    const startedAt = performance.now()
    set({ pendingQuestion: { question, startedAt } })

    const priorQuestions = queryHistory.map((entry) => entry.question)
    const cacheKey = nlCacheKey(question, priorQuestions, enableInsight)
    const cached = nlQuestionCache.get(cacheKey)

    let finalState: AskResponse
    try {
      finalState =
        cached ??
        (await apiAskQuestion({
          question,
          conversation_history: buildConversationHistory(queryHistory),
          enable_insight: enableInsight,
        }))
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'The agent could not be reached.'
      finalState = emptyAskResponse(message)
    }

    const answerDurationMs = performance.now() - startedAt
    const entry = newHistoryEntry(
      question,
      finalState,
      answerDurationMs,
      options?.originatedFromVoice ?? false,
    )

    set((state) => ({
      ...commitQueryHistory(state, [...state.queryHistory, entry]),
      pendingQuestion: null,
      nlQuestionCache: cached ? state.nlQuestionCache : new Map(state.nlQuestionCache).set(cacheKey, finalState),
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
              response.chart,
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

  startNewChat: () => set(freshConversationState()),

  loadConversation: (id) =>
    set((state) => {
      const conversation = state.conversations[id]
      if (!conversation) return state
      return { ...freshConversationState(), activeConversationId: id, queryHistory: conversation.entries }
    }),

  renameConversation: (id, title) =>
    set((state) => {
      const conversation = state.conversations[id]
      if (!conversation) return state
      const trimmed = title.trim()
      if (!trimmed) return state
      return { conversations: { ...state.conversations, [id]: { ...conversation, title: trimmed } } }
    }),

  deleteConversation: (id) =>
    set((state) => {
      const remaining = { ...state.conversations }
      delete remaining[id]
      if (state.activeConversationId !== id) return { conversations: remaining }
      return { ...freshConversationState(), conversations: remaining }
    }),
}))
