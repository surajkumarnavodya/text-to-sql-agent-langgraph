import type {
  AgentStatus,
  AskResponse,
  ChartRecommendation,
  Citation,
  ConversationExchange,
  MediaGenerationResult,
  ServerMessage,
} from './types'
import type { ChartOptions } from './chartEngine'

/** A read-only snapshot of one file attached to a sent question, taken at
 * submit time (`ChatInput.tsx`'s `submit()`) so the question's own chat
 * bubble can show what was actually attached to it. Deliberately
 * independent of the composer's own live `ChatAttachment.previewUrl` --
 * the composer's pending chips (and their `previewUrl`s) are cleared once
 * the backend has accepted the request (see `ChatInput.tsx`'s `submit()`),
 * so without this separate snapshot a sent turn would have no record of
 * its attachment left anywhere at all. `previewUrl` here is a fresh,
 * separate object/data URL, created before that clear happens, so it stays
 * valid for the life of this already-sent turn regardless of what happens
 * to the composer afterward. */
export interface SentAttachmentPreview {
  filename: string
  kind: 'image' | 'document'
  /** Null for a document -- no visual preview to show. */
  previewUrl: string | null
}

/** One full chat turn -- question + everything about its answer. Holds its
 * own editable-SQL/confirmed-result state (rather than a single global
 * "current turn" the app used to keep) so every previously-asked question
 * stays fully rendered, adjacent to its own answer, for the life of the
 * session -- not just the latest one. */
export interface QueryHistoryEntry {
  entryId: string
  question: string
  sql: string | null
  agentStatus: AgentStatus
  retryCount: number
  rowCount: number | null
  tables: string[]
  timestamp: string
  finalState: AskResponse
  /** Wall-clock time the /ask call itself took, for the "Answered in Ns"
   * badge (mirrors Perplexity/ChatGPT's "Researched Ns" pattern). */
  answerDurationMs: number
  /** Current contents of this turn's own SQL editor -- starts as
   * `finalState.sql`, but the user may edit it before confirming. */
  editableSql: string
  confirmedColumns: string[] | null
  confirmedRows: unknown[][] | null
  confirmedError: string | null
  confirmedSql: string | null
  /** Per-column inferred type ('numeric' | 'date' | 'text'), from
   * ExecuteResponse.column_types -- feeds frontend/src/lib/chartEngine.ts's
   * chart-type validity checks without re-guessing types client-side. */
  confirmedColumnTypes: Record<string, string> | null
  /** A starting-point suggestion only -- see ChartRecommendation's own
   * docstring for why chartEngine.ts never trusts this outright. */
  confirmedChartRecommendation: ChartRecommendation | null
  /** Whether confirmedRows hit Settings.max_result_rows -- shown as a
   * visible notice both in the results table area and (if a chart exists)
   * alongside the chart, per this feature's own "never chart a subset as
   * though it's the full data" requirement. */
  confirmedTruncated: boolean
  confirmedDurationMs: number | null
  /** The user's own chart selection for this confirmed result, or `null`
   * if no chart has been generated (the default -- see ChartSection.tsx).
   * Session-only, same lifetime as confirmedColumns/confirmedRows
   * themselves (neither survives a reload today -- see CLAUDE.md's chat-
   * history "Known limitations"), so there is nothing server-side this
   * could stay out of sync with. */
  chartOptions: ChartOptions | null
  /** True only when this question came from a voice turn
   * (`useVoiceConversation`'s transcript, landed in the composer and then
   * submitted like any typed question) -- the sole signal
   * `chatStore.askQuestion` uses to decide whether to synthesize and play
   * back the answer. A typed question always leaves this false, so it can
   * never trigger audio output. */
  originatedFromVoice: boolean
  /** Blob object URL for this turn's spoken-answer audio, set once
   * synthesis completes for a voice-originated turn. `TurnCard` plays it
   * and must revoke it on unmount/replacement. */
  spokenAudioUrl: string | null
  /** Files attached to this specific question, as sent -- see
   * `SentAttachmentPreview`'s own docstring. Empty for the overwhelming
   * majority of turns (no attachment). */
  sentAttachments: SentAttachmentPreview[]
}

/** One conversation as shown in the history drawer.
 *
 * For a locally-authenticated user (`useLocalAuthStore`), this is now a
 * *server-backed* record: `id` is the real, permanent
 * `identity.models.Conversation.id`, and `entries` is lazily populated
 * from `GET /conversations/{id}/messages` the first time the conversation
 * is opened (`messagesLoaded` distinguishes "not fetched yet" from
 * "genuinely has zero messages") -- see `chatStore.ts`'s
 * `hydrateHistoryFromServer`/`loadConversation`. For everyone else (local
 * auth not configured/not signed in), this remains a purely in-memory,
 * session-only snapshot, exactly as before this feature existed -- see
 * `docs/chat-history-architecture.md`'s "Known limitations" for why this
 * isn't extended to OIDC/unauthenticated sessions in this pass. */
export interface ConversationSummary {
  id: string
  title: string
  updatedAt: string
  entries: QueryHistoryEntry[]
  /** True once `entries` reflects the server's own message list (even if
   * that list is empty) -- undefined/false means "not fetched yet," not
   * "empty." Always true for a purely local (non-server-backed) conversation. */
  messagesLoaded?: boolean
}

/** Turns a server-backed conversation's raw message rows
 * (`GET /conversations/{id}/messages`) into the same `QueryHistoryEntry`
 * shape a live, in-session turn produces -- so `TurnCard.tsx` and friends
 * render a reloaded-from-another-device conversation identically to one
 * asked in the current tab, with one honest exception: a reloaded turn
 * only has whatever this app chose to persist (the answer text, and the
 * SQL if the turn produced one, via `metadata.sql`) -- not the full
 * `AskResponse` (charts, per-source citations, schema DDL shown, ...),
 * since that was never durably stored. See `docs/chat-history-architecture.md`'s
 * "Known limitations" for the full disclosure.
 *
 * Pairs consecutive `user`/`assistant` rows by `sequence_number` (assigned
 * server-side by `identity.repositories.history.append_turn`, always
 * adjacent for one turn) -- a user message with no matching assistant
 * reply yet (a truncated/failed persistence) still renders as its own
 * entry, with a "pending"-shaped empty answer, rather than being dropped.
 */
export function serverMessagesToQueryHistory(rows: ServerMessage[]): QueryHistoryEntry[] {
  const sorted = [...rows].sort((a, b) => a.sequence_number - b.sequence_number)
  const entries: QueryHistoryEntry[] = []
  let index = 0
  while (index < sorted.length) {
    const userRow = sorted[index]
    if (userRow.role !== 'user') {
      index += 1
      continue
    }
    const assistantRow = sorted[index + 1]?.role === 'assistant' ? sorted[index + 1] : undefined
    const sql = assistantRow?.metadata?.sql ?? null
    const succeeded = assistantRow ? assistantRow.status !== 'failed' : false
    const finalState: AskResponse = {
      session_id: '',
      conversation_id: userRow.conversation_id,
      status: assistantRow ? (succeeded ? 'succeeded' : 'failed') : 'pending',
      database: null,
      sql,
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
      failure_explanation: succeeded ? null : (assistantRow?.content ?? null),
      error_history: [],
      sources_used: [],
      synthesized_answer: succeeded ? (assistantRow?.content ?? null) : null,
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
    entries.push({
      entryId: userRow.id,
      question: userRow.content,
      sql,
      agentStatus: finalState.status,
      retryCount: 0,
      rowCount: null,
      tables: [],
      timestamp: userRow.created_at,
      finalState,
      answerDurationMs: 0,
      editableSql: sql ?? '',
      confirmedColumns: null,
      confirmedRows: null,
      confirmedError: null,
      confirmedSql: null,
      confirmedColumnTypes: null,
      confirmedChartRecommendation: null,
      confirmedTruncated: false,
      confirmedDurationMs: null,
      chartOptions: null,
      originatedFromVoice: false,
      spokenAudioUrl: null,
      // A reloaded past turn isn't a byte-for-byte reconstruction of a live
      // one -- charts/citations/schema DDL aren't restored either (see
      // docs/chat-history-architecture.md's own "Known limitations").
      // Nothing server-side stores which files a past question was sent
      // with, so this stays empty rather than guessing.
      sentAttachments: [],
    })
    index += assistantRow ? 2 : 1
  }
  return entries
}

/** First question, trimmed and capped, so a conversation reads like a real
 * title instead of a raw id -- mirrors how most chat products derive a
 * thread name from its opening message. */
export function deriveConversationTitle(question: string): string {
  const trimmed = question.trim().replace(/\s+/g, ' ')
  return trimmed.length > 60 ? `${trimmed.slice(0, 57)}…` : trimmed
}

export type ConversationGroupKey = 'today' | 'yesterday' | 'previous7Days' | 'older'

/** Buckets conversations by recency (today / yesterday / previous 7 days /
 * older), newest-first within each bucket -- the grouping every modern
 * chat history panel (ChatGPT, Claude, Perplexity) uses. */
export function groupConversationsByRecency(
  conversations: ConversationSummary[],
): { key: ConversationGroupKey; items: ConversationSummary[] }[] {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(startOfToday)
  startOfYesterday.setDate(startOfYesterday.getDate() - 1)
  const sevenDaysAgo = new Date(startOfToday)
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7)

  const buckets: Record<ConversationGroupKey, ConversationSummary[]> = {
    today: [],
    yesterday: [],
    previous7Days: [],
    older: [],
  }

  const sorted = [...conversations].sort(
    (a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
  )
  for (const conversation of sorted) {
    const updated = new Date(conversation.updatedAt)
    if (updated >= startOfToday) buckets.today.push(conversation)
    else if (updated >= startOfYesterday) buckets.yesterday.push(conversation)
    else if (updated >= sevenDaysAgo) buckets.previous7Days.push(conversation)
    else buckets.older.push(conversation)
  }

  return (['today', 'yesterday', 'previous7Days', 'older'] as const)
    .map((key) => ({ key, items: buckets[key] }))
    .filter((group) => group.items.length > 0)
}

/** Compact "3m ago"/"Yesterday"-style timestamp for a history item --
 * deliberately never the raw ISO string or a full date+time, which would
 * read as a technical/internal detail rather than a normal chat product
 * timestamp. */
export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  const diffSeconds = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (diffSeconds < 60) return 'Just now'
  const diffMinutes = Math.round(diffSeconds / 60)
  if (diffMinutes < 60) return `${diffMinutes}m ago`
  const diffHours = Math.round(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  const diffDays = Math.round(diffHours / 24)
  if (diffDays === 1) return 'Yesterday'
  if (diffDays < 7) return `${diffDays}d ago`
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export const MAX_FOLLOWUP_EXCHANGES = 3

export function newHistoryEntry(
  question: string,
  finalState: AskResponse,
  answerDurationMs: number,
  originatedFromVoice = false,
  sentAttachments: SentAttachmentPreview[] = [],
): QueryHistoryEntry {
  return {
    entryId: crypto.randomUUID(),
    question,
    sql: finalState.sql,
    agentStatus: finalState.status,
    retryCount: finalState.retry_count,
    rowCount: finalState.row_count,
    tables: finalState.schema_tables.map((table) => table.table_name),
    timestamp: new Date().toISOString(),
    finalState,
    answerDurationMs,
    editableSql: finalState.sql ?? '',
    confirmedColumns: null,
    confirmedRows: null,
    confirmedError: null,
    confirmedSql: null,
    confirmedColumnTypes: null,
    confirmedChartRecommendation: null,
    confirmedTruncated: false,
    confirmedDurationMs: null,
    chartOptions: null,
    originatedFromVoice,
    spokenAudioUrl: null,
    sentAttachments,
  }
}

/** Attaches the synthesized-speech blob URL for a voice-originated turn's
 * answer -- see `chatStore.askQuestion`'s post-success TTS call. */
export function withSpokenAudio(entry: QueryHistoryEntry, audioUrl: string): QueryHistoryEntry {
  return { ...entry, spokenAudioUrl: audioUrl }
}

export function withConfirmedResult(
  entry: QueryHistoryEntry,
  columns: string[],
  rows: unknown[][],
  confirmedSql: string,
  columnTypes: Record<string, string>,
  chartRecommendation: ChartRecommendation | null,
  truncated: boolean,
  durationMs: number,
): QueryHistoryEntry {
  return {
    ...entry,
    confirmedColumns: columns,
    confirmedRows: rows,
    confirmedSql,
    confirmedColumnTypes: columnTypes,
    confirmedChartRecommendation: chartRecommendation,
    confirmedTruncated: truncated,
    confirmedDurationMs: durationMs,
    confirmedError: null,
    // A fresh confirmed result may have a different shape than whatever
    // chart the user built for a prior run of this same turn's SQL box --
    // never carry a stale chart config over onto new columns/rows it was
    // never validated against.
    chartOptions: null,
  }
}

/** Sets or clears (pass `null`) this turn's user-generated chart -- the
 * one piece of chart state that changes independently of a SQL
 * confirm/execute cycle (generate/customize/remove, see ChartSection.tsx). */
export function withChartOptions(
  entry: QueryHistoryEntry,
  chartOptions: ChartOptions | null,
): QueryHistoryEntry {
  return { ...entry, chartOptions }
}

/** Replaces this turn's `generation_result` after `POST /generate/confirm`
 * completes -- the "generation" source equivalent of `withConfirmedResult`
 * above, applied to `finalState` directly (media generation has no
 * separate `confirmed*` field set, unlike SQL's editable-box pattern,
 * since there's nothing to hand-edit before confirming). */
export function withGenerationResult(
  entry: QueryHistoryEntry,
  result: MediaGenerationResult,
): QueryHistoryEntry {
  return {
    ...entry,
    finalState: { ...entry.finalState, generation_result: result },
  }
}

export function withConfirmedError(entry: QueryHistoryEntry, error: string): QueryHistoryEntry {
  return {
    ...entry,
    confirmedError: error,
    confirmedColumns: null,
    confirmedRows: null,
    confirmedSql: null,
    confirmedColumnTypes: null,
    confirmedChartRecommendation: null,
    confirmedTruncated: false,
    confirmedDurationMs: null,
    chartOptions: null,
  }
}

export function replaceEntry(
  history: QueryHistoryEntry[],
  entryId: string,
  updated: QueryHistoryEntry,
): QueryHistoryEntry[] {
  return history.map((entry) => (entry.entryId === entryId ? updated : entry))
}

/** Only succeeded turns qualify as follow-up context, most recent first N,
 * returned oldest-first -- exactly ui/session_history.py's own contract. */
export function buildConversationHistory(
  history: QueryHistoryEntry[],
  maxExchanges = MAX_FOLLOWUP_EXCHANGES,
): ConversationExchange[] {
  return history
    .filter((entry) => entry.agentStatus === 'succeeded')
    .slice(-maxExchanges)
    .map((entry) => ({
      question: entry.question,
      sql: entry.sql,
      tables: entry.tables,
      status: entry.agentStatus,
    }))
}

const STATUS_DISPLAY: Record<AgentStatus, { icon: string; label: string }> = {
  succeeded: { icon: '✅', label: 'succeeded' },
  failed: { icon: '❌', label: 'failed' },
  needs_clarification: { icon: '❓', label: 'needs clarification' },
  rejected: { icon: '🚫', label: 'rejected' },
  rate_limited: { icon: '🐌', label: 'rate limited' },
  pending: { icon: '⏳', label: 'pending' },
  sanitizing_input: { icon: '⏳', label: 'pending' },
  classifying_followup: { icon: '⏳', label: 'pending' },
  retrieving_schema: { icon: '⏳', label: 'pending' },
  generating: { icon: '⏳', label: 'pending' },
  reviewing: { icon: '⏳', label: 'pending' },
  validating: { icon: '⏳', label: 'pending' },
  estimating_cost: { icon: '⏳', label: 'pending' },
  executing: { icon: '⏳', label: 'pending' },
}

export function statusLabel(entry: QueryHistoryEntry): { icon: string; label: string } {
  if (entry.agentStatus === 'succeeded' && entry.retryCount > 0) {
    return { icon: '🔁', label: 'retried' }
  }
  return STATUS_DISPLAY[entry.agentStatus] ?? { icon: '❓', label: entry.agentStatus }
}

/** Renders one source's citations as a "Sources" markdown bullet list --
 * same dedup (by document_id, falling back to filename) and same
 * URL-vs-plain-text distinction as SourceAnswerCard.tsx's own rendering, so
 * the exported markdown (PDF/copy) matches what's shown on screen. A web
 * citation's `filename` is the page URL itself (see
 * agent/orchestrator/nodes.py::web_search_node), so it renders as a real
 * markdown link; a document/policy filename has nothing to link to in a
 * static export (no click handler survives into a PDF or clipboard paste),
 * so it's listed as plain text. Returns '' when there's nothing to show. */
function citationsMarkdown(citations: Citation[]): string {
  const seen = new Set<string>()
  const unique = citations.filter((citation) => {
    const key = citation.document_id || citation.filename
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  if (unique.length === 0) return ''
  const lines = unique.map((citation) =>
    /^https?:\/\//i.test(citation.filename)
      ? `- [${citation.filename}](${citation.filename})`
      : `- ${citation.filename}`,
  )
  return ['**Sources:**', ...lines].join('\n')
}

/** Assembles a plain-markdown export of everything textual about one turn
 * (synthesized/per-source answer, its cited sources, insight, the SQL that
 * ran) -- what the "Download answer" (PDF) and "Copy answer" buttons both
 * build from. Sources are appended right after the answer text they belong
 * to, mirroring SourceAnswerCard.tsx's on-screen answer-then-sources
 * layout, so a combined (synthesized) answer gets one combined sources
 * list pooled from every source that actually fired, and a single-source
 * answer gets just that source's own list. */
export function buildAnswerMarkdown(entry: QueryHistoryEntry): string {
  const parts: string[] = []
  const state = entry.finalState
  if (state.synthesized_answer) {
    parts.push(state.synthesized_answer)
    const pooledCitations = [
      ...(state.document_result?.citations ?? []),
      ...(state.policy_result?.citations ?? []),
      ...(state.web_result?.citations ?? []),
    ]
    const sources = citationsMarkdown(pooledCitations)
    if (sources) parts.push(sources)
  } else {
    if (state.document_result) {
      parts.push(`**Documents**: ${state.document_result.answer}`)
      const sources = citationsMarkdown(state.document_result.citations)
      if (sources) parts.push(sources)
    }
    if (state.policy_result) {
      parts.push(`**Policy**: ${state.policy_result.answer}`)
      const sources = citationsMarkdown(state.policy_result.citations)
      if (sources) parts.push(sources)
    }
    if (state.web_result) {
      parts.push(`**Web**: ${state.web_result.answer}`)
      const sources = citationsMarkdown(state.web_result.citations)
      if (sources) parts.push(sources)
    }
  }
  if (state.insight) parts.push(`**Insight**: ${state.insight}`)
  const sql = entry.confirmedSql ?? entry.sql
  if (sql) parts.push(`\`\`\`sql\n${sql}\n\`\`\``)
  return parts.join('\n\n')
}

/** Picks the one thing worth reading aloud for a voice-originated turn --
 * plain, spoken-style text, not `buildAnswerMarkdown`'s markdown-formatted
 * export. Priority: a grounded insight (SQL path) > a synthesized/
 * per-source answer (multi-source path) > a row-count fallback -- never
 * silent on a successful turn, since the user asked out loud and should
 * hear *something* back. */
export function buildSpokenAnswerText(entry: QueryHistoryEntry): string {
  const state = entry.finalState
  if (state.insight) return state.insight
  if (state.synthesized_answer) return state.synthesized_answer
  if (state.document_result?.answer) return state.document_result.answer
  if (state.policy_result?.answer) return state.policy_result.answer
  if (state.web_result?.answer) return state.web_result.answer
  if (entry.rowCount !== null) {
    return entry.rowCount === 1 ? 'Found 1 row.' : `Found ${entry.rowCount} rows.`
  }
  return 'Done.'
}

/** Same cache-key shape this app's own `/ask` question-deduplication uses. */
export function nlCacheKey(
  question: string,
  priorQuestions: string[],
  enableInsight: boolean,
): string {
  return JSON.stringify([question.trim().toLowerCase(), priorQuestions, enableInsight])
}
