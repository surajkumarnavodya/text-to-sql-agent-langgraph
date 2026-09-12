import type { PlotlyFigure } from './types'

/** Translates the Plotly-shaped `{data, layout}` JSON `agent/result_charting
 * .py::build_chart` returns into the minimal shape our Chart.js component
 * needs. The backend still owns the actual chart-type decision (bar vs.
 * line, which column is the label vs. the value, category capping) --
 * this is a pure data-format adapter, not a re-implementation of that
 * auto-pick heuristic, so switching rendering libraries client-side can
 * never drift from what the backend decided. */
export interface AdaptedChart {
  kind: 'bar' | 'line'
  labels: (string | number)[]
  values: number[]
  xTitle: string
  yTitle: string
}

function axisTitle(layout: Record<string, unknown>, axis: 'xaxis' | 'yaxis'): string {
  const axisObj = layout[axis]
  if (axisObj && typeof axisObj === 'object' && 'title' in axisObj) {
    const title = (axisObj as { title?: unknown }).title
    if (title && typeof title === 'object' && 'text' in title) {
      const text = (title as { text?: unknown }).text
      if (typeof text === 'string') return text
    }
    if (typeof title === 'string') return title
  }
  return ''
}

export function adaptPlotlyFigure(figure: PlotlyFigure): AdaptedChart | null {
  const trace = figure.data?.[0]
  if (!trace) return null

  const x = trace.x
  const y = trace.y
  if (!Array.isArray(x) || !Array.isArray(y)) return null

  const kind: AdaptedChart['kind'] = trace.type === 'bar' ? 'bar' : 'line'
  return {
    kind,
    labels: x as (string | number)[],
    values: (y as number[]).map(Number),
    xTitle: axisTitle(figure.layout ?? {}, 'xaxis'),
    yTitle: axisTitle(figure.layout ?? {}, 'yaxis'),
  }
}
