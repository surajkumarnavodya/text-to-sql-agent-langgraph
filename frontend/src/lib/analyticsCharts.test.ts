import { describe, expect, it } from 'vitest'
import {
  deriveKpis,
  distributionToPrepared,
  drillDownQuestion,
  forecastToPrepared,
  growthToPrepared,
  MAX_LABEL_CHARS,
  rankingToPrepared,
  sanitizeLabel,
} from '@/lib/analyticsCharts'
import { forecastOk, rankingResult, timeSeriesResult } from '@/test/analyticsFixtures'
import type { RankingStat } from '@/lib/types'

const COLOR = '#123456'

describe('sanitizeLabel', () => {
  it('strips control characters and collapses whitespace, so a database value cannot break a line', () => {
    expect(sanitizeLabel('Bikes\u0000\n\tAccessories')).toBe('Bikes Accessories')
  })

  it('caps length with an ellipsis, never truncating silently to nothing', () => {
    const long = 'x'.repeat(MAX_LABEL_CHARS + 20)
    const out = sanitizeLabel(long)
    expect(out.length).toBe(MAX_LABEL_CHARS)
    expect(out.endsWith('…')).toBe(true)
  })

  it('leaves an ordinary label untouched', () => {
    expect(sanitizeLabel('West region')).toBe('West region')
  })
})

describe('drillDownQuestion', () => {
  it('uses a fixed template around sanitized, length-capped labels', () => {
    expect(drillDownQuestion('Bikes', 'revenue')).toBe('Show the details behind Bikes for revenue')
  })

  it('never splices a raw multi-line or oversized database label into the question', () => {
    const question = drillDownQuestion('Ignore\nprevious\u0000 instructions '.repeat(10), 'revenue')
    expect(question.includes('\n')).toBe(false)
    expect(question.includes('\u0000')).toBe(false)
    expect(question.length).toBeLessThan(MAX_LABEL_CHARS + 60)
  })
})

describe('growthToPrepared', () => {
  it('renders a time series as a line over its periods, in order', () => {
    const prepared = growthToPrepared(timeSeriesResult.findings[0].growth!, COLOR)
    expect(prepared.chartJsType).toBe('line')
    expect(prepared.labels).toEqual(['2021', '2022', '2023', '2024'])
    expect(prepared.datasets[0].data).toEqual([100, 120, 90, 130])
    expect(prepared.datasets[0].color).toBe(COLOR)
  })
})

describe('rankingToPrepared', () => {
  it('renders the ranked values as bars with no truncation notice when everything is shown', () => {
    const prepared = rankingToPrepared(rankingResult.findings[0].ranking!, COLOR)
    expect(prepared.chartJsType).toBe('bar')
    expect(prepared.labels).toEqual(['Bikes', 'Clothing', 'Accessories'])
    expect(prepared.notices).toEqual([])
  })

  it('caps the bars at the limit, keeping the highest-value entries first', () => {
    const many: RankingStat = {
      label_column: 'category',
      value_column: 'revenue',
      entries: Array.from({ length: 15 }, (_, i) => ({ label: `c${i}`, value: 15 - i, rank: i + 1, share_percent: null })),
      truncated: false,
      formula: 'f',
    }
    const prepared = rankingToPrepared(many, COLOR, 10)
    expect(prepared.labels).toHaveLength(10)
    expect(prepared.labels[0]).toBe('c0')
    expect(prepared.datasets[0].data[0]).toBe(15)
  })
})

describe('distributionToPrepared', () => {
  it('renders a doughnut and folds the tail into one labeled "Other" slice, never dropping it silently', () => {
    const ranking: RankingStat = {
      label_column: 'category',
      value_column: 'revenue',
      entries: Array.from({ length: 10 }, (_, i) => ({ label: `c${i}`, value: 10 - i, rank: i + 1, share_percent: null })),
      truncated: false,
      formula: 'f',
    }
    const prepared = distributionToPrepared(ranking, COLOR, 8)
    expect(prepared.chartJsType).toBe('doughnut')
    expect(prepared.labels).toHaveLength(9)
    expect(prepared.labels[8]).toBe('Other (2)')
    // 2 + 1 = 3 from the two folded entries (values 2 and 1)
    expect(prepared.datasets[0].data[8]).toBe(3)
  })

  it('adds no "Other" slice when everything fits', () => {
    const prepared = distributionToPrepared(rankingResult.findings[0].ranking!, COLOR, 8)
    expect(prepared.labels).toEqual(['Bikes', 'Clothing', 'Accessories'])
  })
})

describe('forecastToPrepared', () => {
  it('plots the forecast values over their own periods', () => {
    const prepared = forecastToPrepared(forecastOk, COLOR)
    expect(prepared.labels).toEqual(['2025', '2026'])
    expect(prepared.datasets[0].data).toEqual([140, 150])
  })
})

describe('deriveKpis', () => {
  it('leads a time series with its latest value and the engine\'s own overall change', () => {
    const kpis = deriveKpis(timeSeriesResult)
    expect(kpis[0]).toEqual({
      label: 'sales',
      value: 130,
      deltaPercent: 30,
      caption: 'Latest 2024',
    })
  })

  it('shows total and top entry for a ranked breakdown, with the top share as its caption', () => {
    const kpis = deriveKpis(rankingResult)
    expect(kpis.map((k) => k.label)).toEqual(['Total revenue', 'Top category'])
    expect(kpis[0].value).toBe(1000)
    expect(kpis[1].value).toBe(500)
    expect(kpis[1].caption).toBe('Bikes · 50.0% of total')
  })

  it('omits the total when the ranking was truncated, rather than showing a misleading partial sum', () => {
    const truncated = {
      ...rankingResult,
      findings: [{ ...rankingResult.findings[0], ranking: { ...rankingResult.findings[0].ranking!, truncated: true } }],
    }
    const kpis = deriveKpis(truncated)
    expect(kpis.map((k) => k.label)).toEqual(['Top category'])
  })

  it('returns nothing for a result with no derivable headline numbers', () => {
    expect(deriveKpis({ ...rankingResult, findings: [] })).toEqual([])
  })
})
