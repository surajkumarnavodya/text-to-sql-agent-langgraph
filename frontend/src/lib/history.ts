import type { AgentStatus, AskResponse, ConversationExchange, PlotlyFigure } from './types'

/** One full chat turn -- question + everything about its answer. Holds its
 * own editable-SQL/confirmed-result state (rather than a single global
 * "current turn" the app used to keep) so every previously-asked question
 * stays fully rendered, adjacent to its own answer, for the life of the
 * session -- not just the latest one. Mirrors ui/session_history.py's
 * QueryHistoryEntry, extended with the fields that per-entry rendering and
 * timing display need. */
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
  confirmedChart: PlotlyFigure | null
  confirmedDurationMs: number | null
}

export const MAX_FOLLOWUP_EXCHANGES = 3

export function newHistoryEntry(
  question: string,
  finalState: AskResponse,
  answerDurationMs: number,
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
    confirmedChart: null,
    confirmedDurationMs: null,
  }
}

export function withConfirmedResult(
  entry: QueryHistoryEntry,
  columns: string[],
  rows: unknown[][],
  confirmedSql: string,
  chart: PlotlyFigure | null,
  durationMs: number,
): QueryHistoryEntry {
  return {
    ...entry,
    confirmedColumns: columns,
    confirmedRows: rows,
    confirmedSql,
    confirmedChart: chart,
    confirmedDurationMs: durationMs,
    confirmedError: null,
  }
}

export function withConfirmedError(entry: QueryHistoryEntry, error: string): QueryHistoryEntry {
  return {
    ...entry,
    confirmedError: error,
    confirmedColumns: null,
    confirmedRows: null,
    confirmedSql: null,
    confirmedChart: null,
    confirmedDurationMs: null,
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

/** Assembles a plain-markdown export of everything textual about one turn
 * (synthesized/per-source answer, insight, the SQL that ran) -- what the
 * "Download answer" button saves to a .md file. */
export function buildAnswerMarkdown(entry: QueryHistoryEntry): string {
  const parts: string[] = []
  const state = entry.finalState
  if (state.synthesized_answer) {
    parts.push(state.synthesized_answer)
  } else {
    if (state.document_result) parts.push(`**Documents**: ${state.document_result.answer}`)
    if (state.policy_result) parts.push(`**Policy**: ${state.policy_result.answer}`)
    if (state.web_result) parts.push(`**Web**: ${state.web_result.answer}`)
  }
  if (state.insight) parts.push(`**Insight**: ${state.insight}`)
  const sql = entry.confirmedSql ?? entry.sql
  if (sql) parts.push(`\`\`\`sql\n${sql}\n\`\`\``)
  return parts.join('\n\n')
}

/** Same cache-key shape as ui/app.py's nl_question_cache. */
export function nlCacheKey(
  question: string,
  priorQuestions: string[],
  enableInsight: boolean,
): string {
  return JSON.stringify([question.trim().toLowerCase(), priorQuestions, enableInsight])
}
