import { create } from 'zustand'
import { ApiError, askQuestion as apiAskQuestion, executeSql, submitGoldenExampleFeedback } from '@/lib/api'
import {
  buildConversationHistory,
  newHistoryEntry,
  nlCacheKey,
  replaceEntry,
  withConfirmedError,
  withConfirmedResult,
  type QueryHistoryEntry,
} from '@/lib/history'
import type { AskResponse } from '@/lib/types'

/** The question currently in flight, rendered as a "Thinking Ns" card
 * directly below the user's message -- see components/chat/PendingTurnCard.tsx
 * and TimingBadge.tsx. `startedAt` is a `performance.now()` timestamp, not
 * wall-clock, so the live counter is immune to system clock changes
 * mid-request. */
export interface PendingQuestion {
  question: string
  startedAt: number
}

interface ChatState {
  queryHistory: QueryHistoryEntry[]
  pendingQuestion: PendingQuestion | null
  confirmingEntryId: string | null
  goldenFeedbackGiven: Set<string>
  enableInsight: boolean
  nlQuestionCache: Map<string, AskResponse>

  setEnableInsight: (value: boolean) => void
  setEditableSql: (entryId: string, sql: string) => void
  askQuestion: (question: string) => Promise<void>
  confirmAndRun: (entryId: string) => Promise<void>
  rerunEntry: (entryId: string) => Promise<void>
  clearHistory: () => void
  giveGoldenFeedback: (entryId: string, thumbsUp: boolean) => Promise<void>
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
  goldenFeedbackGiven: new Set(),
  enableInsight: true,
  nlQuestionCache: new Map(),

  setEnableInsight: (value) => set({ enableInsight: value }),

  setEditableSql: (entryId, sql) =>
    set((state) => ({
      queryHistory: state.queryHistory.map((entry) =>
        entry.entryId === entryId ? { ...entry, editableSql: sql } : entry,
      ),
    })),

  askQuestion: async (question) => {
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
    const entry = newHistoryEntry(question, finalState, answerDurationMs)

    set((state) => ({
      queryHistory: [...state.queryHistory, entry],
      pendingQuestion: null,
      nlQuestionCache: cached ? state.nlQuestionCache : new Map(state.nlQuestionCache).set(cacheKey, finalState),
    }))
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
      set((state) => ({ queryHistory: replaceEntry(state.queryHistory, entryId, updated) }))
    } catch (error) {
      const message = error instanceof ApiError ? error.message : 'Execution failed unexpectedly.'
      const updated = withConfirmedError(entry, message)
      set((state) => ({ queryHistory: replaceEntry(state.queryHistory, entryId, updated) }))
    } finally {
      set({ confirmingEntryId: null })
    }
  },

  rerunEntry: async (entryId) => {
    const entry = get().queryHistory.find((item) => item.entryId === entryId)
    if (!entry) return
    await get().askQuestion(entry.question)
  },

  clearHistory: () => set({ queryHistory: [], pendingQuestion: null, confirmingEntryId: null }),

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
}))
