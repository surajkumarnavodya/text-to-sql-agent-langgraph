// The SLO table from docs/SCALE_OUT_PROMPT.md's "THE HONEST TARGET"
// section, encoded as k6 thresholds so a run's pass/fail is explicit in
// its own exit code, not eyeballed from a numbers dump. Split by request
// tag (`name: ...`, set on every http.* call in the scenarios) so a slow
// `/ask` doesn't silently mask a slow `/conversations` in one aggregate
// number.
//
// Phase 0 note: `/ask`'s own target here is deliberately NOT the
// streamed "time to first token" row from that table (streaming doesn't
// exist yet -- see SCALE_OUT_PROMPT.md Phase 2) -- it's a full-response
// latency threshold against *this* mock-LLM-backed baseline, generous
// enough to not fail on the mock's own configured ~800ms latency plus
// this app's real schema-retrieval/validation/execution overhead. Later
// phases replace this with the real streamed-first-event threshold once
// Phase 2 ships.
export const sloThresholds = {
  'http_req_duration{name:auth_login}': ['p(95)<150'],
  'http_req_duration{name:conversations_list}': ['p(95)<150'],
  'http_req_duration{name:ask}': ['p(95)<5000'],
  'http_req_duration{name:execute}': ['p(95)<2500'],
  http_req_failed: ['rate<0.01'],
}
