import { describe, expect, it } from 'vitest'
import { detectChartTypeSwitchRequest } from './chartFollowup'

describe('detectChartTypeSwitchRequest', () => {
  it.each([
    ['show this as a pie chart', 'pie'],
    ['show it as a doughnut', 'doughnut'],
    ['can you display that as a donut chart', 'doughnut'],
    ['switch to line', 'line'],
    ['switch to a line chart', 'line'],
    ['change the chart to a stacked bar', 'bar-stacked'],
    ['convert this to a horizontal bar chart', 'bar-horizontal'],
    ['bar chart instead', 'bar'],
    ['pie chart instead please', 'pie'],
    ['make this a scatter plot', 'scatter'],
    ['view the data as an area chart', 'area'],
    ['render it as a kpi', 'kpi'],
    ['turn it into a stat card', 'kpi'],
    ['show the result as a table instead', 'table'],
  ] as const)('detects %s -> %s', (text, expected) => {
    expect(detectChartTypeSwitchRequest(text)).toBe(expected)
  })

  it.each([
    'how many orders were placed last month',
    'show sales as a percentage of total revenue',
    'what is the pie shop revenue by region',
    'show me the orders table',
    'compare this quarter to last quarter',
  ])('does not misfire on an ordinary question: %s', (text) => {
    expect(detectChartTypeSwitchRequest(text)).toBeNull()
  })

  it('returns null for an empty or whitespace-only string', () => {
    expect(detectChartTypeSwitchRequest('')).toBeNull()
    expect(detectChartTypeSwitchRequest('   ')).toBeNull()
  })

  it('prefers the more specific bar variant over the plain "bar" match', () => {
    expect(detectChartTypeSwitchRequest('switch to a stacked bar chart')).toBe('bar-stacked')
    expect(detectChartTypeSwitchRequest('switch to a horizontal bar chart')).toBe('bar-horizontal')
    expect(detectChartTypeSwitchRequest('switch to a bar chart')).toBe('bar')
  })
})
