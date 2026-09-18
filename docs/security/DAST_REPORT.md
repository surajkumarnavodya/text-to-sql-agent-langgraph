# DAST Report

**Status: NOT VERIFIED.** No staging environment and no OWASP ZAP (or
equivalent) were available in this sandboxed session. DAST has never been
run against this application at any point across this repository's
documented history — `SECURITY_FINAL_REPORT.md` §18 already disclosed
this same gap; it remains true today, re-confirmed rather than assumed.

**Why a local attempt wasn't forced this session:** `api/main.py`'s
`lifespan` context manager warms real connections at startup (every
configured database's engine, the Ollama client, the compiled LangGraph
graph — per `CLAUDE.md`'s own "Process-lifetime singletons" section,
independently confirmed by reading `api/main.py:194` this session). This
sandboxed environment has no live Ollama instance and no application
database configured. Standing up a server that fails its own startup
checks (or that runs against fabricated, meaningless data) would produce
DAST-*shaped* output without DAST's actual value — a false signal is
worse than an honest `NOT VERIFIED` here, per this gate's own explicit
"do not fabricate evidence" rule.

## What exists today that is dynamic-adjacent, but is explicitly NOT DAST

FastAPI's `TestClient` drives real ASGI request/response cycles (not a
pure unit test of a function in isolation) for a meaningful slice of this
app's HTTP surface:

- `tests/test_security_headers.py` — asserts real HTTP response headers
  (HSTS, CSP, `X-Content-Type-Options`, etc.) on actual responses
- `tests/test_api_authz.py`, `tests/test_api_auth.py`,
  `tests/test_api_identity_auth.py` — real HTTP 401/403/429 status codes
  against real (mocked-backend) requests, including malformed/expired/
  forged-signature tokens
- `tests/test_api_chat_history.py`, `tests/test_api_documents.py` — IDOR
  probes via real HTTP requests with altered path parameters

**This is not a substitute for DAST.** None of it does what ZAP's active
scanner does: fuzzing parameters for injection, spidering for undocumented
endpoints, testing HTTP methods this app's own route table doesn't
anticipate, or probing for reflected/stored XSS in ways a hand-written
test wouldn't think to check. It's real evidence for the specific things
it tests, and honestly scoped as narrower than DAST everywhere else in
this document set.

## Procedure to actually close this gap

1. Stand up a real staging deployment (`docker-compose.yml`, or the
   `Dockerfile` image directly) against a real, disposable Ollama instance
   and a disposable/seeded database — not production data.
2. `docker run --rm -v $(pwd):/zap/wrk owasp/zap2docker-stable zap-baseline.py -t http://<staging-host>:8000 -r zap-report.html`
   (baseline/passive scan first — safe against a real, disposable staging
   target) followed by a full active scan
   (`zap-full-scan.py`) once the baseline is clean, since active scanning
   sends real attack payloads and should never be pointed at anything
   that isn't disposable.
3. Specifically target, per this gate's own Phase 17 list: authentication,
   authorization, IDOR, SQL injection, XSS, SSRF, CORS, security headers,
   file upload, HTTP methods, error handling, path traversal, parameter
   tampering, rate limiting.
4. For every finding: `FIX`, `DOCUMENTED ACCEPTED RISK`, or
   `FALSE POSITIVE WITH EVIDENCE` — never silently suppressed.
5. Re-run after any fix to confirm closure, and add the specific finding
   as a named regression test under `tests/security/` so it can't silently
   regress (matching this repository's own established convention from
   the CI-gate-hardening pass earlier in this engagement).

Until this procedure actually runs, `docs/security/FINAL_PRODUCTION_GATE.md`'s
DAST row stays `NOT VERIFIED` — this is one of the explicit, named
triggers for this gate's own `NOT READY` verdict, not a minor footnote.
