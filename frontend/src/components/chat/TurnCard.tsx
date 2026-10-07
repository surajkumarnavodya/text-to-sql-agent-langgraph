import { AlertTriangle, Loader2, Play, ShieldAlert, Sparkles } from 'lucide-react'
import { lazy, Suspense } from 'react'
import { useTranslation } from 'react-i18next'
import { GoldenFeedbackWidget } from '@/components/sql/GoldenFeedbackWidget'
import { ResultsTable } from '@/components/sql/ResultsTable'
import { SqlEditor } from '@/components/sql/SqlEditor'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Expander } from '@/components/ui/expander'
import { Markdown } from '@/components/ui/markdown'
import { buildAnswerMarkdown, isSqlResult, type QueryHistoryEntry } from '@/lib/history'
import { useChatStore } from '@/store/chatStore'
import { ChatMessage } from './ChatMessage'
import { CopyAnswerButton } from './CopyAnswerButton'
import { DownloadAnswerButton } from './DownloadAnswerButton'
import { QueryPlanPanel } from './QueryPlanPanel'
import { ReadAloudButton } from './ReadAloudButton'
import { ResponseFeedbackWidget } from './ResponseFeedbackWidget'
import { RetryTimeline } from './RetryTimeline'
import { SchemaContextPanel } from './SchemaContextPanel'
import { SentAttachmentsPreview } from './SentAttachmentsPreview'
import { SourcesUsedPanel } from './SourcesUsedPanel'
import { TimingBadge } from './TimingBadge'

// Chart.js is the largest remaining chunk in this app -- lazy-loaded so a
// turn never pays for it unless the user actually clicks "Visualize"
// (ChartSection.tsx itself imports ResultChart.tsx, so this one lazy
// boundary covers the whole opt-in chart flow: the picker, the customize
// panel, and the chart.js renderer). Chart generation is optional by
// design (see ChartSection.tsx's own docstring) -- this lazy boundary is
// what makes "optional" also mean "not even downloaded" for the common
// case of a turn nobody ever visualizes.
const ChartSection = lazy(() =>
  import('@/components/sql/ChartSection').then((m) => ({ default: m.ChartSection })),
)

// Prompt 30's analysis panels render through the same chart renderer
// (ResultChart -> Chart.js), so they get the identical lazy boundary --
// reading a turn must not pull Chart.js into the main bundle.
const AnalyticsSummary = lazy(() =>
  import('@/components/analytics/AnalyticsSummary').then((m) => ({ default: m.AnalyticsSummary })),
)

const BLOCKED_STATUSES = new Set([
  'failed',
  'needs_clarification',
  'rejected',
  'rate_limited',
  // A reconstructed history entry with no matching assistant row at all
  // (see history.ts::serverMessagesToQueryHistory's own doc comment on a
  // truncated/failed persistence) -- there is genuinely nothing to show,
  // and showing the SQL editor + "Confirm and Run" here would offer to run
  // a query that was never actually generated.
  'pending',
])

/** One full question+answer turn -- always fully rendered (never replaced
 * by a later turn), so the whole conversation stays visible and each
 * answer stays right next to the question that produced it. Every piece
 * of interactive state below (the SQL editor, Confirm-and-Run, golden
 * feedback) is scoped to *this* entry via its entryId, so editing one
 * turn's SQL box can never affect another's. */
/** When the question was asked, in the browser's locale -- e.g. "Oct 4, 2026, 10:32 AM".
 * Unparseable timestamps render as nothing rather than "Invalid Date". */
function formatTurnTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function TurnCard({ entry, isMultiDb }: { entry: QueryHistoryEntry; isMultiDb: boolean }) {
  const { t } = useTranslation()
  const setEditableSql = useChatStore((state) => state.setEditableSql)
  const confirmAndRun = useChatStore((state) => state.confirmAndRun)
  const confirmingEntryId = useChatStore((state) => state.confirmingEntryId)
  const setChartOptions = useChatStore((state) => state.setChartOptions)
  const askQuestion = useChatStore((state) => state.askQuestion)

  const state = entry.finalState
  const showSqlPanel = isSqlResult(state.sources_used) && !BLOCKED_STATUSES.has(entry.agentStatus)
  // The editor + "Confirm and Run" specifically (as opposed to the
  // read-only schema/plan context above it) require actual SQL text to
  // edit or a result already confirmed -- `showSqlPanel` alone used to be
  // treated as "safe to show an editor," but a reconstructed history entry
  // (or, in principle, any SQL-path turn that produced no candidate SQL at
  // all) has neither, and showing an empty box with a working "Confirm and
  // Run" button next to it offered to run nothing. See CLAUDE.md's chat-
  // history section for the reload-specific case this was found from.
  const hasEditableSql = entry.editableSql.trim().length > 0
  const showSqlEditor = showSqlPanel && (hasEditableSql || entry.confirmedColumns !== null)
  const isConfirming = confirmingEntryId === entry.entryId
  const answerMarkdown = buildAnswerMarkdown(entry)

  return (
    <div id={`turn-${entry.entryId}`} className="flex scroll-mt-4 flex-col gap-3">
      <SentAttachmentsPreview attachments={entry.sentAttachments} />
      <div className="flex justify-end">
        <ChatMessage message={{ role: 'user', content: entry.question }} />
      </div>

      <div className="flex flex-col gap-3 pl-10">
        <div className="flex flex-wrap items-center gap-2">
          <TimingBadge mode="done" durationMs={entry.answerDurationMs} />
          <time dateTime={entry.timestamp} className="text-xs text-[var(--muted-foreground)]">
            {formatTurnTime(entry.timestamp)}
          </time>
          {answerMarkdown && (
            <>
              <CopyAnswerButton answer={answerMarkdown} />
              <DownloadAnswerButton question={entry.question} answer={answerMarkdown} />
              <ReadAloudButton entryId={entry.entryId} answer={answerMarkdown} />
              <ResponseFeedbackWidget entryId={entry.entryId} />
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
        {state.permission_denied_notice && (
          <p className="text-sm text-[var(--warning)]">{state.permission_denied_notice}</p>
        )}
        {entry.agentStatus === 'pending' && (
          <p className="text-sm text-[var(--muted-foreground)]">
            {t('chat.noRecordedAnswer')}
          </p>
        )}
        {entry.agentStatus === 'failed' && (
          <div className="flex flex-col gap-2">
            {state.restricted_field_notice && (
              <p className="flex items-center gap-1.5 text-sm text-[var(--warning)]">
                <ShieldAlert className="h-4 w-4 shrink-0" aria-hidden="true" />
                {state.restricted_field_notice}
              </p>
            )}
            <p className="text-sm text-[var(--danger)]">
              {isSqlResult(state.sources_used) ? (
                <>
                  Agent could not produce a working query:{' '}
                  {state.failure_explanation ?? state.error_history.at(-1) ?? 'Unknown error.'}
                </>
              ) : (
                (state.failure_explanation ?? state.error_history.at(-1) ?? 'The request failed.')
              )}
            </p>
            {state.sql && (
              <pre className="whitespace-pre-wrap break-words rounded bg-[var(--muted)] p-2 font-mono text-xs">
                {state.sql}
              </pre>
            )}
          </div>
        )}

        {state.attempt_history.length > 0 && <RetryTimeline attempts={state.attempt_history} />}
        <SourcesUsedPanel state={state} entryId={entry.entryId} />
        {showSqlPanel && ((isMultiDb && state.database) || state.model) && (
          <Expander title={t('details.title')}>
            {isMultiDb && state.database && (
              <p className="text-xs text-[var(--muted-foreground)]">
                {t('details.database')}: <strong>{state.database}</strong>
              </p>
            )}
            {state.model && (
              <p className="text-xs text-[var(--muted-foreground)]">
                {t('details.model')}: <strong>{state.model}</strong>
              </p>
            )}
          </Expander>
        )}

        {showSqlPanel && (
          <>
            <SchemaContextPanel tables={state.schema_tables} />
            <QueryPlanPanel plan={state.query_plan} />

            {showSqlEditor && (
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
            )}

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
                {entry.editableSql === state.sql && (
                  <Suspense
                    fallback={<p className="text-xs text-[var(--muted-foreground)]">{t('common.loading')}</p>}
                  >
                    <AnalyticsSummary
                      state={state}
                      cacheStatus={entry.confirmedCacheStatus ?? null}
                      onAsk={(question) => void askQuestion(question)}
                    />
                  </Suspense>
                )}
                <ResultsTable columns={entry.confirmedColumns} rows={entry.confirmedRows} />
                <Suspense
                  fallback={<p className="text-xs text-[var(--muted-foreground)]">{t('common.loading')}</p>}
                >
                  <ChartSection
                    columns={entry.confirmedColumns}
                    rows={entry.confirmedRows}
                    columnTypes={entry.confirmedColumnTypes ?? {}}
                    chartRecommendation={entry.confirmedChartRecommendation}
                    truncated={entry.confirmedTruncated}
                    chartOptions={entry.chartOptions}
                    onChartOptionsChange={(options) => setChartOptions(entry.entryId, options)}
                  />
                </Suspense>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
