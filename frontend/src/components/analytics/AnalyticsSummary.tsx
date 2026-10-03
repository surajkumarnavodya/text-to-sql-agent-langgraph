import { useChartPalette } from '@/hooks/useChartPalette'
import { deriveKpis, sanitizeLabel } from '@/lib/analyticsCharts'
import type { AskResponse } from '@/lib/types'
import { AnomalyPanel } from './AnomalyPanel'
import { ComparisonPanel } from './ComparisonPanel'
import { EvidencePanel } from './EvidencePanel'
import { ForecastPanel } from './ForecastPanel'
import { GoverningMetricsPanel } from './GoverningMetricsPanel'
import { KpiStrip } from './KpiStrip'
import { QueryScopePanel } from './QueryScopePanel'
import { RankingPanel } from './RankingPanel'
import { TrendPanel } from './TrendPanel'

/** Prompt 30 -- the business-user analysis view for one answered question.
 * Renders only from already-computed backend state (`analytical_result`,
 * `forecast_result`, `recommendations`, `analytical_intent`, `analytical_plan`,
 * `governing_metrics`) -- it never computes a statistic, a forecast, or a
 * recommendation itself. The classified intent chooses which panel leads
 * (a distribution question gets the share view, a comparison gets the
 * side-by-side), but every panel the data supports is still shown, so
 * nothing is hidden behind the classifier's guess.
 *
 * Honest states: an empty result says so; a skipped calculation is listed,
 * never silently absent; a result with no breakdown at all says that rather
 * than rendering an empty card. `onAsk` is the drill-down/related-question
 * hook -- it submits a new question through the normal `/ask` path. */
export function AnalyticsSummary({
  state,
  cacheStatus,
  onAsk,
}: {
  state: AskResponse
  cacheStatus: 'hit' | 'miss' | null
  onAsk?: (question: string) => void
}) {
  const palette = useChartPalette()
  const color = palette[0]

  const result = state.analytical_result
  const intent = state.analytical_intent?.intent ?? null
  const growth = result?.findings.find((finding) => finding.growth)?.growth ?? null
  const ranking = result?.findings.find((finding) => finding.ranking)?.ranking ?? null
  const anomalies = (result?.findings ?? []).flatMap((finding) =>
    finding.kind === 'anomaly' && finding.anomaly ? [finding.anomaly] : [],
  )
  const kpis = result ? deriveKpis(result) : []
  const forecast = state.forecast_result
  const rowCount = result?.row_count ?? state.row_count ?? 0
  const comparisonDescription =
    state.analytical_plan?.comparison?.description ?? state.analytical_intent?.comparison ?? null

  const hasBreakdown = Boolean(growth || ranking || kpis.length > 0 || forecast || anomalies.length > 0)
  const hasContext = Boolean(
    state.governing_metrics.length > 0 ||
      state.recommendations.length > 0 ||
      state.analytical_plan ||
      state.analytical_intent,
  )

  const freshness = cacheStatus === 'hit' ? 'Served from a recent cached result' : 'Live query result'
  const header = (
    <p className="flex flex-wrap gap-x-3 text-xs text-[var(--muted-foreground)]">
      <span>Source: {sanitizeLabel(state.database ?? 'default', 60)}</span>
      <span>{rowCount} rows</span>
      <span>{freshness}</span>
    </p>
  )

  if (rowCount === 0 || result?.shape === 'empty') {
    return (
      <section aria-label="Analysis" className="flex flex-col gap-2">
        {header}
        <p className="text-sm text-[var(--muted-foreground)]">No rows matched this question, so there is nothing to analyse.</p>
      </section>
    )
  }

  const skipped = result?.insufficient_data_reasons ?? []

  return (
    <section aria-label="Analysis" className="flex flex-col gap-4">
      {header}

      {skipped.length > 0 && (
        <ul className="list-disc pl-5 text-xs text-[var(--muted-foreground)]">
          {skipped.map((reason) => (
            <li key={reason}>Skipped: {sanitizeLabel(reason, 200)}</li>
          ))}
        </ul>
      )}

      {!hasBreakdown && !hasContext && (
        <p className="text-sm text-[var(--muted-foreground)]">
          No statistical breakdown is available for this kind of result.
        </p>
      )}

      <KpiStrip kpis={kpis} />

      {intent === 'comparison' && (
        <ComparisonPanel growth={growth} ranking={ranking} description={comparisonDescription} />
      )}

      {growth && <TrendPanel growth={growth} color={color} />}

      {ranking && (
        <RankingPanel
          ranking={ranking}
          variant={intent === 'distribution' ? 'share' : 'bars'}
          color={color}
          onDrillDown={onAsk}
        />
      )}

      {growth && <AnomalyPanel anomalies={anomalies} />}

      {forecast && <ForecastPanel forecast={forecast} color={color} />}

      <GoverningMetricsPanel metrics={state.governing_metrics} />

      <EvidencePanel recommendations={state.recommendations} onAsk={onAsk} />

      <QueryScopePanel plan={state.analytical_plan} intent={state.analytical_intent} />
    </section>
  )
}
