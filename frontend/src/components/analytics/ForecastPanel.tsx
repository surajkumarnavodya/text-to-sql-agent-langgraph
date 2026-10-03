import { ResultChart } from '@/components/sql/ResultChart'
import { forecastToPrepared, sanitizeLabel } from '@/lib/analyticsCharts'
import { formatNumber } from '@/lib/chartEngine'
import type { ForecastResult } from '@/lib/types'
import { AnalyticsPanel } from './AnalyticsPanel'

/** Prompt 16's deterministic forecast, always labelled as an AI estimate. A
 * rejected forecast shows its own rejection reasons as the insufficient-
 * evidence state -- never an empty chart or a silent omission. Every
 * successful forecast lists its limitations next to the numbers it qualifies. */
export function ForecastPanel({ forecast, color }: { forecast: ForecastResult; color: string }) {
  if (forecast.status === 'rejected') {
    return (
      <AnalyticsPanel id="analytics-forecast" title="Forecast" truthLevel="ai_inference">
        <p className="text-sm text-[var(--warning)]">A forecast could not be produced for this series.</p>
        <ul className="list-disc pl-5 text-sm text-[var(--muted-foreground)]">
          {forecast.rejection_reasons.map((reason) => (
            <li key={reason}>{sanitizeLabel(reason, 200)}</li>
          ))}
        </ul>
      </AnalyticsPanel>
    )
  }

  const evaluation = forecast.evaluation
  return (
    <AnalyticsPanel id="analytics-forecast" title={`Forecast · next ${forecast.horizon} periods`} truthLevel="ai_inference">
      <ResultChart
        prepared={forecastToPrepared(forecast, color)}
        chartType="line"
        title=""
        showLegend={false}
        numberFormat="plain"
        stacked={false}
      />
      <p className="text-xs text-[var(--warning)]">Estimate, not a confirmed fact. Values are projections from past data.</p>
      <table className="w-full text-sm">
        <caption className="sr-only">Forecast values by period</caption>
        <thead>
          <tr className="text-left text-xs text-[var(--muted-foreground)]">
            <th scope="col" className="py-1 font-medium">Period</th>
            <th scope="col" className="py-1 font-medium">Estimate</th>
            <th scope="col" className="py-1 font-medium">Range</th>
          </tr>
        </thead>
        <tbody>
          {forecast.points.map((point) => (
            <tr key={`${point.horizon_step}-${point.period}`} className="border-t border-[var(--border)]">
              <td className="py-1">{sanitizeLabel(point.period)}</td>
              <td className="py-1 tabular-nums">{formatNumber(point.forecast, 'plain')}</td>
              <td className="py-1 tabular-nums text-[var(--muted-foreground)]">
                {point.lower_bound !== null && point.upper_bound !== null
                  ? `${formatNumber(point.lower_bound, 'plain')} – ${formatNumber(point.upper_bound, 'plain')}`
                  : 'Not estimated'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {forecast.model && (
        <p className="text-xs text-[var(--muted-foreground)]">
          Model {sanitizeLabel(forecast.model.model)} trained on {forecast.model.training_point_count} periods (
          {sanitizeLabel(forecast.model.training_window_start)} to {sanitizeLabel(forecast.model.training_window_end)}).
        </p>
      )}
      {evaluation && (
        <p className="text-xs text-[var(--muted-foreground)]">
          Backtest over the last {evaluation.holdout_size} periods: MAE {formatNumber(evaluation.mae, 'plain')}, RMSE{' '}
          {formatNumber(evaluation.rmse, 'plain')}
          {evaluation.mape !== null ? `, MAPE ${evaluation.mape.toFixed(1)}%` : ''}.
        </p>
      )}
      {forecast.limitations.length > 0 && (
        <div>
          <p className="text-xs font-medium text-[var(--muted-foreground)]">Limitations</p>
          <ul className="list-disc pl-5 text-xs text-[var(--muted-foreground)]">
            {forecast.limitations.map((item) => (
              <li key={item}>{sanitizeLabel(item, 200)}</li>
            ))}
          </ul>
        </div>
      )}
    </AnalyticsPanel>
  )
}
