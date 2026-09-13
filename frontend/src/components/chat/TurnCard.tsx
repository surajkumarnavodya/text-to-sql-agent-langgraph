import { AlertTriangle, Loader2, Play, Sparkles } from 'lucide-react'
import { lazy, Suspense } from 'react'
import { useTranslation } from 'react-i18next'
import { GoldenFeedbackWidget } from '@/components/sql/GoldenFeedbackWidget'
import { ResultsTable } from '@/components/sql/ResultsTable'
import { SqlEditor } from '@/components/sql/SqlEditor'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Expander } from '@/components/ui/expander'
import { Markdown } from '@/components/ui/markdown'
import { buildAnswerMarkdown, type QueryHistoryEntry } from '@/lib/history'
import { useChatStore } from '@/store/chatStore'
import { ChatMessage } from './ChatMessage'
import { CopyAnswerButton } from './CopyAnswerButton'
import { DownloadAnswerButton } from './DownloadAnswerButton'
import { QueryPlanPanel } from './QueryPlanPanel'
import { RetryTimeline } from './RetryTimeline'
import { SchemaContextPanel } from './SchemaContextPanel'
import { SourcesUsedPanel } from './SourcesUsedPanel'
import { TimingBadge } from './TimingBadge'

// Chart.js is the largest remaining chunk in this app -- lazy-loaded so a
// turn that never shows a chart never pays for it, matching
// agent/result_charting.py's own "no chart is a valid, common outcome"
// philosophy.
const ResultChart = lazy(() => import('@/components/sql/ResultChart').then((m) => ({ default: m.ResultChart })))

function isSqlResult(sourcesUsed: string[]): boolean {
  return sourcesUsed.length === 0 || sourcesUsed.includes('sql')
}

const BLOCKED_STATUSES = new Set(['failed', 'needs_clarification', 'rejected', 'rate_limited'])

/** One full question+answer turn -- always fully rendered (never replaced
 * by a later turn), so the whole conversation stays visible and each
 * answer stays right next to the question that produced it. Every piece
 * of interactive state below (the SQL editor, Confirm-and-Run, golden
 * feedback) is scoped to *this* entry via its entryId, so editing one
 * turn's SQL box can never affect another's. */
export function TurnCard({ entry, isMultiDb }: { entry: QueryHistoryEntry; isMultiDb: boolean }) {
  const { t } = useTranslation()
  const setEditableSql = useChatStore((state) => state.setEditableSql)
  const confirmAndRun = useChatStore((state) => state.confirmAndRun)
  const confirmingEntryId = useChatStore((state) => state.confirmingEntryId)

  const state = entry.finalState
  const showSqlPanel = isSqlResult(state.sources_used) && !BLOCKED_STATUSES.has(entry.agentStatus)
  const isConfirming = confirmingEntryId === entry.entryId
  const answerMarkdown = buildAnswerMarkdown(entry)

  return (
    <div id={`turn-${entry.entryId}`} className="flex scroll-mt-4 flex-col gap-3">
      <div className="flex justify-end">
        <ChatMessage message={{ role: 'user', content: entry.question }} />
      </div>

      <div className="flex flex-col gap-3 pl-10">
        <div className="flex flex-wrap items-center gap-2">
          <TimingBadge mode="done" durationMs={entry.answerDurationMs} />
          {answerMarkdown && (
            <>
              <CopyAnswerButton answer={answerMarkdown} />
              <DownloadAnswerButton question={entry.question} answer={answerMarkdown} />
            </>
          )}
          {entry.spokenAudioUrl && (
            // Playback itself already happened once, automatically, via
            // `useVoiceConversation`'s own `<audio>` element as part of the
            // hands-free conversation loop -- this is a manual-replay
            // control only (no `autoPlay`, or the answer would be spoken
            // twice: once here, once by the hook). Kept small and visible
            // so the user can replay/pause/mute it. Deliberately not
            // revoked on unmount: switching conversations and back must
            // not break replay, and one blob URL per voice turn is a
            // bounded, accepted cost for the life of the tab.
            <audio src={entry.spokenAudioUrl} controls className="h-8 max-w-[200px]" />
          )}
        </div>

        {state.followup_classification === 'followup' && state.followup_resolved_against && (
          <p className="text-xs text-[var(--muted-foreground)]">
            ↪ {t('chat.followingUpOn')}: "{state.followup_resolved_against.question}"
          </p>
        )}

        {entry.agentStatus === 'rate_limited' && (
          <p className="text-sm text-[var(--warning)]">
            {state.rate_limit_message ?? 'Too many questions -- please wait a moment.'}
          </p>
        )}
        {entry.agentStatus === 'rejected' && (
          <p className="text-sm text-[var(--warning)]">
            {state.rejection_message ?? 'That question could not be processed.'}
          </p>
        )}
        {entry.agentStatus === 'needs_clarification' && (
          <p className="text-sm text-[var(--warning)]">
            Needs clarification: {state.clarification_message}
          </p>
        )}
        {entry.agentStatus === 'failed' && (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-[var(--danger)]">
              Agent could not produce a working query:{' '}
              {state.failure_explanation ?? state.error_history.at(-1) ?? 'Unknown error.'}
            </p>
            {state.sql && (
              <pre className="overflow-x-auto rounded bg-[var(--muted)] p-2 font-mono text-xs">{state.sql}</pre>
            )}
          </div>
        )}

        {state.attempt_history.length > 0 && <RetryTimeline attempts={state.attempt_history} />}
        <SourcesUsedPanel state={state} entryId={entry.entryId} />
        {isMultiDb && showSqlPanel && state.database && (
          <Expander title={t('details.title')}>
            <p className="text-xs text-[var(--muted-foreground)]">
              {t('details.database')}: <strong>{state.database}</strong>
            </p>
          </Expander>
        )}

        {showSqlPanel && (
          <>
            <SchemaContextPanel tables={state.schema_tables} />
            <QueryPlanPanel plan={state.query_plan} />

            <Expander title={t('sql.title')} defaultOpen>
              <div className="flex flex-col gap-2">
                <p className="text-xs text-[var(--muted-foreground)]">{t('sql.hint')}</p>
                <SqlEditor value={entry.editableSql} onChange={(sql) => setEditableSql(entry.entryId, sql)} />
                {state.cost_notice && entry.editableSql === state.sql && (
                  <p className="text-xs text-[var(--warning)]">{state.cost_notice}</p>
                )}
                <div>
                  <Button
                    variant="primary"
                    onClick={() => void confirmAndRun(entry.entryId)}
                    disabled={isConfirming}
                  >
                    {isConfirming ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Play className="h-4 w-4" />
                    )}
                    {t('sql.confirmAndRun')}
                  </Button>
                </div>
              </div>
            </Expander>

            {entry.confirmedError && (
              <p className="rounded-md border border-[var(--danger)]/30 bg-[var(--danger)]/10 p-3 text-sm text-[var(--danger)]">
                {entry.confirmedError}
              </p>
            )}

            {entry.confirmedColumns && entry.confirmedRows && (
              <div className="flex flex-col gap-4">
                {entry.confirmedDurationMs !== null && (
                  <Badge tone="neutral" className="w-fit">
                    {(entry.confirmedDurationMs / 1000).toFixed(2)}s
                  </Badge>
                )}
                <GoldenFeedbackWidget entryId={entry.entryId} />
                {state.low_confidence_notice && entry.editableSql === state.sql && (
                  <p className="flex items-center gap-1.5 text-xs text-[var(--warning)]">
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                    {state.low_confidence_notice}
                  </p>
                )}
                {state.insight && entry.editableSql === state.sql && (
                  <div className="flex gap-2 rounded-lg border-l-4 border-[var(--accent)] bg-[var(--accent-soft)] p-3 text-sm">
                    <Sparkles className="mt-0.5 h-4 w-4 shrink-0" />
                    <Markdown className="inline">{state.insight}</Markdown>
                  </div>
                )}
                <ResultsTable columns={entry.confirmedColumns} rows={entry.confirmedRows} />
                {entry.confirmedChart && (
                  <Suspense
                    fallback={<p className="text-xs text-[var(--muted-foreground)]">{t('common.loading')}</p>}
                  >
                    <ResultChart figure={entry.confirmedChart} />
                  </Suspense>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
