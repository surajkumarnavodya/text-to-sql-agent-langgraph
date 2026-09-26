// Phase 0 baseline scenario -- see docs/SCALE_OUT_PROMPT.md's Phase 0
// section and docs/SCALE_BASELINE.md for the numbers this produces.
//
// login -> ask -> confirm-and-run (execute) -> list history, per
// iteration, using today's real endpoints (no /ask/stream yet -- that's
// Phase 2; this scenario gets a streamed variant added when that phase
// lands, not invented speculatively now).
//
// Run: k6 run -e BASE_URL=http://localhost:8001 scenario_ask_flow.js
// (or against the mapped compose-network address -- see
// docker-compose.loadtest.yml's own usage comment).
import http from 'k6/http'
import { check, sleep } from 'k6'
import { authHeaders, BASE_URL } from './lib/auth.js'
import { sloThresholds } from './lib/thresholds.js'

export const options = {
  scenarios: {
    ask_flow: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 10 },
        { duration: '1m', target: 25 },
        { duration: '1m', target: 25 },
        { duration: '20s', target: 0 },
      ],
    },
  },
  thresholds: sloThresholds,
}

// Deliberately simple, single-metric questions -- see
// mock_ollama_server.py's own docstring for why a "complex" question
// (top-N-per-group, year-over-year, several metrics) would trigger the
// extra plan/review LLM calls agent/complexity.py gates on, which this
// baseline is not trying to measure yet.
const QUESTIONS = [
  'How many orders are there in total?',
  'What is the total amount across all orders?',
  'List the 10 most recent orders.',
  'What is the average order amount?',
]

export default function () {
  const headers = authHeaders(__VU)

  const askRes = http.post(
    `${BASE_URL}/ask`,
    JSON.stringify({ question: QUESTIONS[__ITER % QUESTIONS.length], enable_insight: true }),
    { headers, tags: { name: 'ask' } },
  )
  check(askRes, {
    'ask succeeded': (r) => r.status === 200,
    'ask returned a status field': (r) => r.json('status') !== undefined,
  })

  const sql = askRes.json('sql')
  const database = askRes.json('database')
  if (askRes.status === 200 && sql) {
    const executeRes = http.post(
      `${BASE_URL}/execute`,
      JSON.stringify({ sql, database }),
      { headers, tags: { name: 'execute' } },
    )
    check(executeRes, { 'execute succeeded': (r) => r.status === 200 })
  }

  const historyRes = http.get(`${BASE_URL}/conversations?limit=10`, {
    headers,
    tags: { name: 'conversations_list' },
  })
  check(historyRes, { 'history list succeeded': (r) => r.status === 200 })

  sleep(1)
}
