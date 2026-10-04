import type {
  AgentStatus,
  AskResponse,
  ChartRecommendation,
  Citation,
  ConversationExchange,
  MediaGenerationResult,
  ServerMessage,
  ServerMessageMetadata,
} from './types'
import type { ChartOptions } from './chartEngine'

/** Whether a turn's `sources_used` reads as the plain SQL pipeline (the
 * multi-source router off, or a real SQL-path answer even when the router
 * is on) -- `TurnCard.tsx`'s single gate for showing schema/plan/SQL-editor
 * UI at all. An **empty** array is this app's own established "router off"
 * convention (`AskResponse.sources_used`'s own docstring), not "unknown" --
 * it must never be conflated with a reconstructed history entry that simply
 * failed to record its real sources; see
 * `askResponseFromPersistedMessage` below for why that distinction is what
 * the reload bug (an empty SQL editor showing for a non-SQL saved answer)
 * actually traced back to. Kept in this module (not `TurnCard.tsx`) so it's
 * plain, non-component logic -- both for direct unit testing and so
 * exporting it doesn't trip the "a component file should only export
 * components" Fast Refresh lint rule. */
export function isSqlResult(sourcesUsed: string[]): boolean {
  return sourcesUsed.length === 0 || sourcesUsed.includes('sql')
}

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
  /** Prompt 30 -- ExecuteResponse.cache_status for the confirmed result, so the
   * analytics panel can say "served from a recent cached result" instead of
   * implying a live query. Session-only like every other confirmed* field
   * (never restored on reload). Optional so every existing literal that
   * predates it keeps type-checking unchanged. */
  confirmedCacheStatus?: 'hit' | 'miss' | null
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

/** Reconstructs the reloaded turn's `AskResponse` from a persisted
 * assistant row's metadata (see `api.chat_persistence._build_history_metadata`)
 * -- the one place a saved conversation's fields are mapped back into the
 * exact shape every live-rendering component (`TurnCard.tsx`,
 * `SourcesUsedPanel.tsx`, `ChartSection.tsx`, ...) already knows how to
 * render, so there is no second, reload-only rendering path to keep in
 * sync with the live one.
 *
 * Three cases, most-informative first:
 * 1. **Rich record** (`metadata.schema_version` present, every turn saved
 *    since this reconstruction was fixed): every field below is read
 *    straight from `metadata` -- `sources_used` is the *real* value the
 *    turn actually produced, which is what makes `TurnCard.tsx`'s
 *    `isSqlResult` check behave identically for a reloaded turn as it did
 *    live (a web/document/policy/attachment-only turn correctly never
 *    shows the SQL editor; a genuine SQL-path turn does).
 * 2. **Legacy record with SQL** (`metadata` exists, no `schema_version`,
 *    but `metadata.sql` is set -- every row persisted before this feature
 *    shipped, for a turn that produced SQL): `sources_used` stays `[]`
 *    (this app's own "empty means SQL path" convention), preserving the
 *    one part of the old behavior that already worked -- the SQL text
 *    itself shows, pre-filled, ready to confirm.
 * 3. **Legacy record without recoverable structure** (`metadata` missing
 *    entirely, or present with no `schema_version` and no `sql`): the
 *    per-source shape genuinely cannot be recovered, so this never
 *    guesses one. `assistantRow.content` -- always saved, regardless of
 *    route, even before this fix -- is instead surfaced via a synthetic
 *    `'legacy'` source marker, which `SourcesUsedPanel.tsx` renders like
 *    any other synthesized answer. This is strictly better than the
 *    pre-fix behavior (no answer shown at all) and never worse -- see
 *    `docs/chat-history-architecture.md`'s "Known limitations" for the
 *    honest disclosure that a legacy record's true source/citations/chart
 *    are not recoverable.
 */
function askResponseFromPersistedMessage(
  userRow: ServerMessage,
  assistantRow: ServerMessage | undefined,
): { response: AskResponse; sql: string | null; resultSnapshot: ServerMessageMetadata['result_snapshot'] } {
  const metadata: ServerMessageMetadata | null = assistantRow?.metadata ?? null
  const isRich = metadata?.schema_version != null
  const succeeded = assistantRow ? assistantRow.status !== 'failed' : false
  const status: AgentStatus = assistantRow
    ? (metadata?.status ?? (succeeded ? 'succeeded' : 'failed'))
    : 'pending'

  const legacySql = !isRich ? (metadata?.sql ?? null) : null
  // True for a genuinely legacy assistant row (created before this
  // metadata shape existed) with no SQL to fall back to either -- whether
  // that row has `metadata: null` (no metadata was ever stored, e.g. a
  // plain "chat_answer" output) or a metadata object simply missing
  // `schema_version`/`sql` makes no practical difference here: neither
  // case has a recoverable per-source structure.
  const isLegacyWithoutStructure = assistantRow != null && !isRich && !legacySql

  let sourcesUsed: string[]
  let synthesizedAnswer: string | null
  if (isRich) {
    sourcesUsed = metadata?.sources_used ?? []
    synthesizedAnswer = metadata?.synthesized_answer ?? (succeeded ? (assistantRow?.content ?? null) : null)
  } else if (isLegacyWithoutStructure && succeeded) {
    // See this function's own doc comment, case 3.
    sourcesUsed = ['legacy']
    synthesizedAnswer = assistantRow?.content ?? null
  } else {
    sourcesUsed = []
    synthesizedAnswer = null
  }

  const sql = isRich ? (metadata?.sql ?? null) : legacySql
  const resultSnapshot = isRich ? (metadata?.result_snapshot ?? null) : null

  const response: AskResponse = {
    session_id: '',
    conversation_id: userRow.conversation_id,
    message_id: assistantRow?.id ?? null,
    status,
    database: metadata?.database ?? null,
    model: metadata?.model ?? null,
    sql,
    result_columns: null,
    result_rows: null,
    row_count: metadata?.row_count ?? null,
    retry_count: metadata?.retry_count ?? 0,
    attempt_history: [],
    insight: metadata?.insight ?? null,
    cost_notice: metadata?.cost_notice ?? null,
    low_confidence_notice: metadata?.low_confidence_notice ?? null,
    rejection_reason: metadata?.rejection_reason ?? null,
    rejection_message: metadata?.rejection_message ?? null,
    rate_limit_message: metadata?.rate_limit_message ?? null,
    clarification_message: metadata?.clarification_message ?? null,
    failure_explanation:
      metadata?.failure_explanation ?? (succeeded ? null : (assistantRow?.content ?? null)),
    error_history: [],
    sources_used: sourcesUsed,
    synthesized_answer: synthesizedAnswer,
    document_result: metadata?.document_result ?? null,
    policy_result: metadata?.policy_result ?? null,
    web_result: metadata?.web_result ?? null,
    generation_result: metadata?.generation_result ?? null,
    media_search_result: metadata?.media_search_result ?? null,
    attachment_result: metadata?.attachment_result ?? null,
    query_plan: metadata?.query_plan ?? null,
    schema_tables: (metadata?.schema_tables ?? []).map((table) => ({
      table_name: table.table_name,
      // DDL is deliberately not persisted (see _build_history_metadata's
      // own docstring) -- an empty string here, never fabricated content.
      ddl: '',
      similarity_score: table.similarity_score,
    })),
    followup_classification: null,
    followup_resolved_against: null,
    permission_denied_notice: metadata?.permission_denied_notice ?? null,
    // Prompt 30's analytics fields are not part of the persisted message
    // metadata today, so a reloaded past turn shows no analytics panels --
    // the same disclosed reconstruction gap as the rest of this function's
    // own "not a byte-for-byte reconstruction" note (CLAUDE.md, chat history).
    analytical_result: null,
    forecast_result: null,
    recommendations: [],
    analytical_intent: null,
    analytical_plan: null,
    governing_metrics: [],
    restricted_field_notice: null,
  }
  return { response, sql, resultSnapshot }
}

/** Turns a server-backed conversation's raw message rows
 * (`GET /conversations/{id}/messages`) into the same `QueryHistoryEntry`
 * shape a live, in-session turn produces -- so `TurnCard.tsx` and friends
 * render a reloaded-from-another-device conversation identically to one
 * asked in the current tab. Never executes SQL, calls a model, fetches a
 * URL, or performs OCR -- every field below comes from what was already
 * persisted (see `askResponseFromPersistedMessage` above); a turn whose SQL
 * was confirmed-and-run in its original session shows those exact rows/
 * chart again immediately, via `metadata.result_snapshot`, without the user
 * needing to click "Confirm and Run" a second time.
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
    const { response: finalState, sql, resultSnapshot } = askResponseFromPersistedMessage(
      userRow,
      assistantRow,
    )
    const attachmentRefs = assistantRow?.metadata?.attachment_refs ?? []
    entries.push({
      entryId: userRow.id,
      question: userRow.content,
      sql,
      agentStatus: finalState.status,
      retryCount: finalState.retry_count,
      rowCount: finalState.row_count,
      tables: finalState.schema_tables.map((table) => table.table_name),
      timestamp: userRow.created_at,
      finalState,
      answerDurationMs: 0,
      editableSql: resultSnapshot?.normalized_sql ?? sql ?? '',
      confirmedColumns: resultSnapshot?.columns ?? null,
      confirmedRows: resultSnapshot?.rows ?? null,
      confirmedError: null,
      confirmedSql: resultSnapshot ? (resultSnapshot.normalized_sql ?? sql) : null,
      confirmedColumnTypes: resultSnapshot?.column_types ?? null,
      confirmedChartRecommendation: resultSnapshot?.chart_recommendation ?? null,
      confirmedTruncated: resultSnapshot?.truncated ?? false,
      confirmedDurationMs: resultSnapshot?.duration_ms ?? null,
      chartOptions: null,
      originatedFromVoice: false,
      spokenAudioUrl: null,
      // Restores which files this question was sent with, from the
      // filename/media-type snapshot taken at persist time (see
      // api/chat_persistence.py::_attachment_refs_snapshot) -- never a
      // preview image or document bytes, since the original attachment may
      // since have been evicted from the ephemeral, process-lifetime
      // attachment store; the chip still shows honestly (filename only).
      sentAttachments: attachmentRefs.map((ref) => ({
        filename: ref.filename,
        kind: ref.media_type.startsWith('image/') ? 'image' : 'document',
        previewUrl: null,
      })),
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
  cacheStatus: 'hit' | 'miss' | null = null,
): QueryHistoryEntry {
  return {
    ...entry,
    confirmedColumns: columns,
    confirmedRows: rows,
    confirmedSql,
    confirmedColumnTypes: columnTypes,
    confirmedChartRecommendation: chartRecommendation,
    confirmedTruncated: truncated,
    confirmedCacheStatus: cacheStatus,
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
    confirmedCacheStatus: null,
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
