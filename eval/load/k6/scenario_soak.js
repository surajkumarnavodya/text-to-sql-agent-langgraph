// 60-minute steady load -- watches for error-rate drift and (indirectly,
// via growing p95 over the run) the orphaned-thread accumulation problem
// confirmed in docs/SCALE_OUT_PROMPT.md's bottleneck #1
// (`_run_orchestrated_with_timeout` abandons, never cancels, a timed-out
// request's thread). This is expected to show that problem clearly, on
// purpose, as Phase 0's evidence -- not something this scenario tries to
// avoid or fix.
//
// Shorter default duration via SOAK_DURATION_MINUTES for local/CI smoke
// runs (see the repo-root Makefile's `load-test` target, which overrides
// this to a couple of minutes) -- the full 60-minute run is for an
// explicit, deliberate baseline session, not every `make load-test`.
import http from 'k6/http'
import { check, sleep } from 'k6'
import { authHeaders, BASE_URL } from './lib/auth.js'
import { sloThresholds } from './lib/thresholds.js'

const DURATION_MINUTES = parseInt(__ENV.SOAK_DURATION_MINUTES || '60', 10)

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: 15,
      duration: `${DURATION_MINUTES}m`,
    },
  },
  thresholds: sloThresholds,
}

const QUESTIONS = [
  'How many orders are there in total?',
  'What is the total amount across all orders?',
  'What is the average order amount?',
]

export default function () {
  const headers = authHeaders(__VU)
  const res = http.post(
    `${BASE_URL}/ask`,
    JSON.stringify({ question: QUESTIONS[__ITER % QUESTIONS.length], enable_insight: true }),
    { headers, tags: { name: 'ask' } },
  )
  check(res, { 'ask succeeded': (r) => r.status === 200 })
  sleep(2)
}
