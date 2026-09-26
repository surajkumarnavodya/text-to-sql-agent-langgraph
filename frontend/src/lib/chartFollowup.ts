/**
 * Lightweight NL detection for a follow-up message that asks to switch the
 * *already-rendered* chart's type ("show this as a pie chart", "switch to
 * line", "bar chart instead") -- deliberately a small set of regexes, not
 * an LLM call: the whole point is answering this kind of follow-up
 * instantly, client-side, without a `/ask` round trip.
 *
 * This module only ever guesses *intent* + a target `ChartTypeId`. It never
 * decides whether that type is actually valid for the current result --
 * `chatStore.tryApplyChartTypeFollowup` re-validates through
 * `getChartTypeOptions` (the same real data-shape check every other chart
 * path in this app goes through) before touching any state, exactly like
 * `recommendChart` never trusts the backend's own hint at face value.
 *
 * Deliberately conservative: a plain data question that happens to contain
 * a chart-type word ("show sales as a percentage of total") must not be
 * mistaken for a chart-switch request. Two independent signals are
 * required together -- a switch-shaped phrase (a "switch/change/convert
 * ... to", a "show/display/... this/it/that/the chart ... as/to/into", or
 * a trailing "instead") AND a recognized chart-type keyword -- so a bare
 * type-name mention alone (e.g. "Pie Shop revenue") never triggers this.
 */

import type { ChartTypeId } from './chartEngine'

const STRONG_SWITCH_RE = /\b(switch|change|convert|turn)\b[\s\S]{0,20}\b(to|into)\b/i
const REFERRING_SHOW_RE =
  /\b(show|display|render|view|visualize|plot)\b[\s\S]{0,15}\b(this|it|that|the\s+chart|the\s+result|the\s+data)\b[\s\S]{0,15}\b(as|to|into)\b/i
// "make" idiomatically drops "as"/"to" ("make this a scatter plot"), unlike
// the show/display/... verbs above -- scoped to its own pattern rather than
// adding a bare "a"/"an" alternative to REFERRING_SHOW_RE, which would
// otherwise match on almost any sentence containing "it"/"this" and the
// word "a" anywhere nearby.
const MAKE_RE = /\bmake\b[\s\S]{0,15}\b(this|it|that|the\s+chart|the\s+result|the\s+data)\b[\s\S]{0,10}\ban?\b/i
const INSTEAD_RE = /\binstead\b/i

function looksLikeChartSwitchRequest(text: string): boolean {
  return STRONG_SWITCH_RE.test(text) || REFERRING_SHOW_RE.test(text) || MAKE_RE.test(text) || INSTEAD_RE.test(text)
}

// Order matters: a more specific phrase ("stacked bar", "horizontal bar",
// "doughnut"/"donut") must be checked before the plain "bar" pattern it
// would otherwise also match.
const TYPE_KEYWORDS: Array<{ type: ChartTypeId; pattern: RegExp }> = [
  { type: 'bar-stacked', pattern: /\bstacked\s+bar(\s+chart)?\b/i },
  { type: 'bar-horizontal', pattern: /\bhorizontal\s+bar(\s+chart)?\b/i },
  { type: 'doughnut', pattern: /\b(doughnut|donut)(\s+chart)?\b/i },
  { type: 'bar', pattern: /\bbar(\s+chart)?\b/i },
  { type: 'pie', pattern: /\bpie(\s+chart)?\b/i },
  { type: 'line', pattern: /\bline(\s+chart)?\b/i },
  { type: 'area', pattern: /\barea(\s+chart)?\b/i },
  { type: 'scatter', pattern: /\bscatter(\s*plot)?(\s+chart)?\b/i },
  { type: 'mixed', pattern: /\b(mixed|combo)(\s+chart)?\b/i },
  { type: 'kpi', pattern: /\b(stat\s*card|kpi|big\s+number|single\s+number)\b/i },
  { type: 'table', pattern: /\b(table\s*(only|view)?|no\s+chart)\b/i },
]

/** Returns the requested chart type if `text` reads as a request to switch
 * the current chart's type, else `null` (meaning: not a chart-followup at
 * all -- treat as an ordinary question). */
export function detectChartTypeSwitchRequest(text: string): ChartTypeId | null {
  const trimmed = text.trim()
  if (!trimmed || !looksLikeChartSwitchRequest(trimmed)) return null
  for (const { type, pattern } of TYPE_KEYWORDS) {
    if (pattern.test(trimmed)) return type
  }
  return null
}
