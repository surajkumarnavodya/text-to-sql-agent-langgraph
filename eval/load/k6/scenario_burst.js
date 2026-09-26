// A short, sharp spike to find this baseline's current saturation point --
// see docs/SCALE_OUT_PROMPT.md's Phase 1 gate ("load test shows no
// thread-pool exhaustion at 10x baseline concurrency") and bottleneck #1
// (api/main.py::ask is sync, and `_run_orchestrated_with_timeout` spawns
// a raw thread per request). Phase 0's job is to show *where* this
// breaks, not fix it -- expect (and document, not hide) errors/timeouts
// climbing past some VU count in docs/SCALE_BASELINE.md.
import http from 'k6/http'
import { check, sleep } from 'k6'
import { authHeaders, BASE_URL } from './lib/auth.js'

export const options = {
  scenarios: {
    burst: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '10s', target: 100 },
        { duration: '30s', target: 100 },
        { duration: '10s', target: 0 },
      ],
    },
  },
  // No thresholds that abort the run -- this scenario's whole purpose is
  // to observe where things break, not to fail fast the moment they do.
}

export default function () {
  const headers = authHeaders(__VU)
  const res = http.post(
    `${BASE_URL}/ask`,
    JSON.stringify({ question: 'How many orders are there in total?', enable_insight: false }),
    { headers, tags: { name: 'ask' }, timeout: '30s' },
  )
  check(res, { 'ask did not 5xx': (r) => r.status < 500 })
  sleep(0.2)
}
