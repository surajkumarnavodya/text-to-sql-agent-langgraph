import type { AskResponse, AnalyticsResult, ForecastResult, Recommendation } from '@/lib/types'

/** A complete, successful SQL-path `AskResponse` with every analytics field
 * at its "nothing to report" value -- tests override only what they exercise. */
export function askState(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    session_id: 's1',
    conversation_id: 'c1',
    message_id: 'm1',
    status: 'succeeded',
    database: 'default',
    model: 'llama3.1:8b',
    sql: 'SELECT year, sales FROM sales_by_year',
    result_columns: null,
    result_rows: null,
    row_count: 4,
    retry_count: 0,
    attempt_history: [],
    insight: null,
    cost_notice: null,
    low_confidence_notice: null,
    rejection_reason: null,
    rejection_message: null,
    rate_limit_message: null,
    clarification_message: null,
    failure_explanation: null,
    error_history: [],
    sources_used: [],
    synthesized_answer: null,
    document_result: null,
    policy_result: null,
    web_result: null,
    generation_result: null,
    media_search_result: null,
    attachment_result: null,
    query_plan: null,
    schema_tables: [],
    followup_classification: null,
    followup_resolved_against: null,
    permission_denied_notice: null,
    analytical_result: null,
    forecast_result: null,
    recommendations: [],
    analytical_intent: null,
    analytical_plan: null,
    governing_metrics: [],
    restricted_field_notice: null,
    ...overrides,
  }
}

/** Backend-shaped analytics fixtures (Prompt 30). Each mirrors the JSON the
 * real `analytics.engine`/`analytics.anomaly`/`analytics.forecasting`/
 * `recommendation.engine` modules produce -- field names and nesting match
 * `api/schemas.py`'s own mirrors, so a test built on these exercises the same
 * shapes a live `/ask` response carries. */

const DATABASE_FACT_CLAIM = {
  value: 'Computed from the result',
  level: 'database_fact' as const,
  grounded_in: [],
  source: 'analytics.engine',
}

/** A four-year series with one dip (2023) -- a real anomaly, a missing 2020
 * period, and an insufficient-data note for seasonality (no lag-12 history). */
export const timeSeriesResult: AnalyticsResult = {
  row_count: 4,
  shape: 'time_series',
  engine_version: '1.0.0',
  insufficient_data_reasons: ['seasonal: no prior occurrence at lag 12'],
  findings: [
    {
      kind: 'growth',
      claim: DATABASE_FACT_CLAIM,
      growth: {
        label_column: 'year',
        value_column: 'sales',
        points: [
          { period: '2021', value: 100, change_percent_from_previous: null },
          { period: '2022', value: 120, change_percent_from_previous: 20 },
          { period: '2023', value: 90, change_percent_from_previous: -25 },
          { period: '2024', value: 130, change_percent_from_previous: 44.4 },
        ],
        overall_change_percent: 30,
        direction: 'up',
        missing_periods: ['2020'],
        formula: 'change_percent = 100 * (current - previous) / abs(previous)',
      },
      ranking: null,
      anomaly: null,
      column_summary: null,
    },
    {
      kind: 'anomaly',
      claim: DATABASE_FACT_CLAIM,
      growth: null,
      ranking: null,
      anomaly: {
        period: '2023',
        value: 90,
        signals: [
          {
            method: 'percent_change',
            baseline_value: 120,
            actual_value: 90,
            deviation: -25,
            threshold_used: 20,
            formula: 'percent change from previous period',
          },
        ],
      },
      column_summary: null,
    },
  ],
}

/** Three product categories -- a categorical breakdown whose shares sum to 100. */
export const rankingResult: AnalyticsResult = {
  row_count: 3,
  shape: 'categorical_aggregate',
  engine_version: '1.0.0',
  insufficient_data_reasons: [],
  findings: [
    {
      kind: 'ranking',
      claim: DATABASE_FACT_CLAIM,
      growth: null,
      anomaly: null,
      column_summary: null,
      ranking: {
        label_column: 'category',
        value_column: 'revenue',
        entries: [
          { label: 'Bikes', value: 500, rank: 1, share_percent: 50 },
          { label: 'Clothing', value: 300, rank: 2, share_percent: 30 },
          { label: 'Accessories', value: 200, rank: 3, share_percent: 20 },
        ],
        truncated: false,
        formula: 'share_percent = 100 * value / sum(all values)',
      },
    },
  ],
}

export const forecastOk: ForecastResult = {
  status: 'ok',
  rejection_reasons: [],
  horizon: 2,
  model: {
    model: 'linear_trend',
    version: '1.0.0',
    training_window_start: '2021',
    training_window_end: '2024',
    training_point_count: 4,
    period_kind: 'year',
    supports_interval: true,
    confidence_level: 0.8,
  },
  points: [
    { period: '2025', forecast: 140, lower_bound: 120, upper_bound: 160, horizon_step: 1 },
    { period: '2026', forecast: 150, lower_bound: 125, upper_bound: 175, horizon_step: 2 },
  ],
  evaluation: { holdout_size: 2, mae: 5, rmse: 6, mape: 4.2, formula: 'MAE = mean(|actual - forecast|)' },
  limitations: ['assumes the historical pattern continues unchanged'],
  truth_level: 'ai_inference',
  summary: 'Estimated sales for the next 2 years.',
  engine_version: '1.0.0',
}

export const forecastRejected: ForecastResult = {
  status: 'rejected',
  rejection_reasons: ['fewer than 4 historical points'],
  horizon: 2,
  model: null,
  points: [],
  evaluation: null,
  limitations: [],
  truth_level: 'ai_inference',
  summary: 'Not enough history to forecast.',
  engine_version: '1.0.0',
}

export const nextQuestionRec: Recommendation = {
  kind: 'next_question',
  claim: {
    value: 'What drove the 2023 dip in sales?',
    level: 'ai_inference',
    grounded_in: [],
    source: 'recommendation.engine',
  },
  rationale: null,
  category: 'anomaly',
  evidence: [],
  affected_entity: '2023',
  action: null,
  measurable_impact: null,
  confidence: 0.7,
  rule_or_model: 'recommendation.engine.AnomalyRule',
  limitations: [],
  generated_at: '2026-10-03T00:00:00Z',
  engine_version: '1.0.0',
}

export const actionRec: Recommendation = {
  kind: 'action',
  claim: {
    value: 'Bikes account for half of revenue; review concentration risk.',
    level: 'ai_inference',
    grounded_in: [],
    source: 'recommendation.engine',
  },
  rationale: null,
  category: 'revenue',
  evidence: [
    { value: 'Bikes share is 50.0% of total revenue', level: 'database_fact', grounded_in: [], source: null },
  ],
  affected_entity: 'Bikes',
  action: 'Review dependence on a single category',
  measurable_impact: 'Affects 50.0% of revenue',
  confidence: 0.82,
  rule_or_model: 'recommendation.engine.RevenueConcentrationRule',
  limitations: ['based on one period of data'],
  generated_at: '2026-10-03T00:00:00Z',
  engine_version: '1.0.0',
}
