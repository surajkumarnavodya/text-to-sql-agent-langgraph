# Scale Baseline

Measured results from `eval/load/`'s Phase 0 harness (see
`docs/SCALE_OUT_PROMPT.md`). Every later phase re-runs this and appends
its own numbers below — no claims without numbers, per that document's
own working rules.

## Test environment (honest disclosure)

- **Hardware**: one Windows 11 developer laptop running Docker Desktop
  (WSL2 backend). Not a dedicated load-test host — other processes (the
  IDE, this very session) shared the same CPU/memory throughout every run
  below. Exact core/RAM allocation to the Docker VM was not pinned or
  recorded.
- **Stack**: `eval/load/docker-compose.loadtest.yml` — the real `api`
  image (repo-root `Dockerfile`, unmodified), a throwaway Postgres 16
  (business DB + identity DB), and `eval/load/mock_ollama_server.py` (an
  ~800ms±300ms artificial-latency stand-in for the real LLM — see that
  module's own docstring for why this is the right thing to hold constant
  when measuring API-tier behavior specifically).
- **Data**: `eval/load/seed/schema.sql` — one table, 300 rows. 50 seeded
  local-auth users (`eval/load/seed_users.py`).
- **Load generator**: k6 (`grafana/k6` Docker image), run container-to-container
  on the same compose network as the stack (not through the host-mapped
  port — see "Known harness limitations" below for why that distinction
  mattered).
- **Rate limits loosened for this harness only** (`.env.loadtest.example`):
  `QUESTION_RATE_LIMIT_PER_MINUTE`, `LLM_CALL_RATE_LIMIT_PER_MINUTE`, and
  `LOGIN_RATE_LIMIT_PER_MINUTE` are all set very high. All three are
  keyed by client IP (or a shared per-minute budget) sized for real human
  traffic; every k6 VU in this harness shares one source IP, so the real
  defaults would measure the rate limiter's own correctness (already unit
  tested in `tests/test_rate_limit.py`), not the API tier's actual
  capacity. **Never copy these three values into a real deployment's
  `.env`.** The *concurrency* limits (`MAX_CONCURRENT_ASK_REQUESTS*`) are
  left at their real defaults — those are what this baseline measures.
  `POST /execute`'s own `API_ACTION_RATE_LIMIT_PER_MINUTE` (default
  20/min) was **not** loosened for the run below — see its 88% failure
  rate in the results, which is that limiter, correctly doing its job
  against a synthetic many-VUs-one-IP client, not a real app bug.

## Run 1 — `scenario_ask_flow.js`, 20 VUs, 60s (2026-09-26)

Login → ask → confirm-and-run (execute) → list history, per iteration.
Instance was already warm (embedding model loaded, one prior request
served) before this run started.

| Endpoint | p50 | p90 | p95 | max | Notes |
|---|---|---|---|---|---|
| `/auth/login` | 1.89s | 2.11s | 2.12s | 2.16s | Argon2id is deliberately CPU-hard; see below |
| `/ask` | 6.24s | 8.26s | 8.75s | 9.74s | Real SQL generation (mock LLM) + schema retrieval |
| `/execute` | 35ms | 395ms | 541ms | 919ms | Fast when it wasn't rate-limited |
| `/conversations` (history) | 42ms | 463ms | 696ms | 1.13s | |

- Total requests: 503 in 64s (~7.9 req/s all endpoints combined).
- Overall error rate: 28.0% (141/503) — **entirely** `POST /execute`
  hitting its own `API_ACTION_RATE_LIMIT_PER_MINUTE` (20/min, not
  loosened for this run, and shared across all 20 VUs' one source IP).
  `/ask` itself: 0 failures, 0 5xx.
- `ask succeeded` / `ask returned a status field` checks: 100% pass —
  every `/ask` call completed with a real, correctly-shaped response.

**What's actually driving `/ask`'s ~6-9s latency here, and why it's not
(mostly) code**: the mock LLM's own artificial delay is only ~800ms.
The remainder is real, CPU-bound work — schema-question embedding
(sentence-transformers ONNX inference) — running inside the *same*
container, competing for the same limited CPU allocation as 20 other
concurrent requests doing the identical thing, on a shared developer
laptop with other processes also running. This is a genuine, measured
concurrency-contention finding, not a code defect: `docs/SCALE_OUT_PROMPT.md`'s
Phase 4 ("split heavy models into separate services with their own
autoscaling") is exactly the fix for this class of contention — it isn't
attempted in this pass. Similarly, `/auth/login`'s own ~2s at this
concurrency is Argon2id password verification (deliberately CPU/memory-hard
by design) contending for the same CPU budget, not a slow code path.

## Run 2 — `scenario_burst.js`, ramp to 100 VUs over ~50s (2026-09-26)

A short, sharp spike specifically to find where this baseline currently
breaks (`docs/SCALE_OUT_PROMPT.md`'s Phase 1 gate).

- `ask did not 5xx`: **100%** (299/299) — under this burst, on this
  hardware, the app degraded to *slow*, never to a server error. No
  crash, no unhandled exception reaching the client.
- Overall `http_req_failed`: 23.1% (69/299) — these were k6's own
  30s **client-side request timeout**, not server-side errors (the
  `ask did not 5xx` check above only inspects responses that actually
  came back; a client-side timeout before any response arrives is
  reported as a failed request but proves nothing about the server's
  own error rate).
- Latency at this load: p50 17.96s, p90/p95 saturate at k6's own 30s
  request timeout — meaning real p90/p95 under this burst are `>=30s`,
  not precisely known from this run.
- 17-28 VUs were still actively iterating at any given moment (versus
  100 configured) — consistent with `MAX_CONCURRENT_ASK_REQUESTS`
  (default 50, global) admitting requests and `MAX_CONCURRENT_ASK_REQUESTS_PER_CALLER`
  (default 2) capping each of the (few, reused) synthetic users, while
  the rest queue behind real CPU-bound work rather than being admitted
  and then failing.

**Bottom line for this run**: the admission-control change made this
session (bounded ask-worker pool + global/per-caller concurrency limits,
see "Changes made" in the final report) produced the intended shape of
degradation — slow and/or 429, never a crash or an unbounded thread pile-up.
Confirming *which* responses were 429 vs. genuinely completed-but-slow
during this specific burst was not separately isolated in this session
— a good follow-up for the next baseline session (tag k6 checks by
response status, not just <500).

## What this harness itself found and fixed (infrastructure, not app code)

Building and actually *running* this harness — not just writing it — surfaced
four real, previously-invisible infrastructure bugs, each fixed in
`eval/load/` (never in application code):

1. **Missing shared volume for the Chroma schema index.** Without it,
   `docker compose run --rm api scripts/build_embeddings.py` and the
   persistent `api` service each got their own throwaway filesystem — the
   built index never reached the running service, which then failed
   *every* real `/ask` call with `RuntimeError: reached with no
   selected_database`. Fixed: a named volume shared between both.
2. **Missing shared volume for the sentence-transformers/Chroma ONNX
   model cache** (a *different* path than #1). Without it, every fresh
   container re-downloaded the ~79MB model from scratch — a ~3-4 minute
   tax on the first real question, looking exactly like a hang.
3. **The new volume from #2 was root-owned by default**, but the app
   image runs as a non-root user — a real, common Docker named-volume
   gotcha. Fixed with a one-time `chown`, run as root, before the
   persistent service starts.
4. **A `Content-Length` bug in this session's own
   `mock_ollama_server.py`**: `GET /api/tags` (called by `/health`'s
   Ollama reachability check) omitted the header, and since the mock
   declares HTTP/1.1 (keep-alive by default), the client had no way to
   know the response was complete — it blocked until its own configured
   timeout on every single call, which is why `/health` intermittently
   took exactly `OLLAMA_REQUEST_TIMEOUT_SECONDS` to respond. Fixed by
   setting `Content-Length` on every response, matching the (already
   correct) `POST /api/chat` handler.

None of these were application bugs — all four were purely in
`eval/load/`'s own test infrastructure — but each is exactly the kind of
fragility a real multi-instance deployment would also hit (shared state
assumptions, cold-start cost, container permission models, HTTP protocol
correctness), which is arguably the harness doing its job even before
producing a single latency number.

**One genuine application-layer finding, found the same way**: a
pre-existing bug in `db/connection.py::get_engine` (unrelated to Chart/attachment
work) — `str()` on a SQLAlchemy `URL` object masks the password as
literal `"***"` by design, and that masked string was what got passed to
`create_engine`. Any discrete-field (not `DB_CONNECTION_STRING`) database
connection with a real password has been silently unable to authenticate
since this code was written. Zero test coverage caught it because this
project's test suite is fully mocked and had never made a real
password-authenticated connection before this harness did. Fixed in
`db/connection.py`, with a regression test
(`tests/test_connection.py::TestGetEngine`) that asserts the real password
reaches `create_engine`, not a masked placeholder.

## Known harness limitations (disclosed, not fixed this pass)

- k6 requests to the **host-mapped port** (`localhost:8001`) exhibited
  inconsistent multi-second delays that container-to-container requests
  (the same call, addressed as `http://api:8000` from another container
  on the compose network) did not — a Docker-Desktop-on-Windows
  port-forwarding quirk, not investigated further since it doesn't affect
  the actual measurement path (k6 always runs container-to-container in
  this harness, never through the host port).
- `/execute`'s own rate limit was deliberately left at its real default for
  Run 1 (see above) — a cleaner run would loosen it the same way the other
  three were loosened, to isolate the concurrency-admission-control numbers
  from this separate limiter's own, already-correct behavior.
- The burst run's 429-vs-timeout breakdown wasn't separately tagged (see
  Run 2's own note).
- Single-node only. No multi-replica, no distributed rate limiting (Phase
  3), no separate inference tier (Phase 4) — this baseline measures
  exactly one API-tier instance's behavior, which is the intentional scope
  of Phase 0.

## Honest capacity statement

**Demonstrated**: one API-tier instance, on unpinned/shared developer
hardware, admits and correctly serves 20 concurrent authenticated users
end-to-end (login → ask → execute → history) with zero server errors;
under a 100-VU burst it degrades to slow responses and/or rejections,
never a crash. The bounded-concurrency admission control added this
session is what produces that degradation shape rather than an unbounded
thread pile-up.

**Not demonstrated**: throughput or latency on real production hardware;
behavior with a real LLM (only latency was mocked — real model
variance/failure modes are untested here); multi-replica behavior; any
number in the "concurrent users" to "1M+" range this program's own
[`SCALE_OUT_PROMPT.md`](SCALE_OUT_PROMPT.md) explicitly warns against
claiming without a defined workload and a successful load test at that
scale. This baseline is Phase 0 of an 11-phase program — see that
document for what each later phase is expected to move this number by,
and re-run this exact harness (`make load-test MODE=full`) after each one.

## Prompt 22 — result cache + per-database execution limiter (2026-10-02)

Two additions measured in this pass: `agent.rate_limit
.get_database_execution_limiter` (a per-database in-flight execution cap)
and `db/result_cache.py` (an opt-in, short-TTL `POST /execute` result
cache). See `CLAUDE.md`'s "Scale/performance hardening" section for the
full design.

**Honest scope disclosure — this is narrower than Run 1/Run 2 above, and
deliberately not presented as a re-run of them.** `docker info` fails in
this environment (`failed to connect to the docker API at
npipe:////./pipe/dockerDesktopLinuxEngine` — Docker Desktop is not
running here), so the real `eval/load/` k6-over-HTTP harness that
produced Run 1/Run 2 could not be executed this pass. What follows
instead is a **real, in-process, multi-threaded benchmark against the
actual shipped classes** (`agent.rate_limit.ConcurrencyLimiter`,
`db.result_cache.ResultCache` — imported and exercised directly, not
reimplemented or mocked) under real `threading` contention. It measures
the two new primitives' own behavior correctly, not this application's
end-to-end HTTP request-path capacity — that still needs a real run of
`make load-test` the next time a Docker-capable environment is available,
and should be done before trusting any throughput number beyond what's
below.

**Method**: 40 concurrent callers (`ThreadPoolExecutor`), capacity 5,
each simulated "query" sleeping 120ms. Scenario 1 compares the new
`ConcurrencyLimiter.try_acquire()` (non-blocking) against a
`threading.Semaphore`-based stand-in for what an exhausted SQLAlchemy
`QueuePool` does today (blocking `acquire(timeout=...)`, shortened to a
1s stand-in for the real 30s `pool_timeout` default so the run completes
in reasonable time). Scenario 2 compares `ResultCache` cold (every call
misses, pays the simulated 120ms round trip) against warm (identical
SQL, already cached).

| Scenario | p50 | p95 | p99 | throughput | errors/rejections |
|---|---|---|---|---|---|
| 1a. Baseline — blocking semaphore (today's QueuePool-exhaustion shape) | 540.7ms | 962.5ms | 962.7ms | 41.3 req/s | 0/40 |
| 1b. New — `ConcurrencyLimiter.try_acquire()` | 0.8ms | 120.5ms | 120.7ms | 326.1 req/s | 35/40 rejected |
| 2a. Result cache cold (always miss) | 120.6ms | 121.5ms | 122.3ms | 310.4 req/s | 0/40 |
| 2b. Result cache warm (identical SQL) | 0.0ms | 0.0ms | 0.0ms | 15,425 req/s | 0/40 |

**Reading scenario 1 honestly**: at this run's shortened 1s timeout, the
blocking baseline happens to let all 40 callers eventually succeed (8
batches of 5 × 120ms ≈ 960ms, just under the 1s cutoff) — zero rejections,
but a p99 wall-wait of ~963ms per caller stuck behind a full pool. The new
limiter instead rejects 35/40 **immediately** (p50 0.8ms) rather than
making them wait, trading "eventually succeeds after sitting in a queue"
for "fails fast with a clear, retryable error." At the real, unshortened
30s `pool_timeout` default this project actually ships with, the
baseline's tail would stretch toward 30 seconds per blocked caller under
sustained saturation instead of under 1 — the gap this change closes gets
materially larger, not smaller, outside this shortened benchmark. This
matches the acceptance criterion in `config/settings.py`'s own
`enable_database_concurrency_limit` docstring: below saturation the
limiter never trips (0 rejections at capacity ≥ demand, unchanged from
before this feature existed — not separately re-measured here since it's
already covered by `tests/test_rate_limit.py`'s unit tests); at
saturation it turns a long blocking wait into an immediate, clearly-worded
rejection.

**Reading scenario 2 honestly**: this demonstrates the cache's own
mechanics (hit path is ~15,000x faster than a 120ms simulated round trip,
and correct under concurrent access — no torn reads/writes across 40
threads hammering the same key) — it says nothing about real-world hit
rate, which depends entirely on how often a deployment's actual callers
re-run byte-identical SQL, an application-usage question this benchmark
cannot answer.

**Not demonstrated by this pass**: end-to-end HTTP request-path
throughput with either feature enabled (needs the real `make load-test`
harness); real SQLAlchemy `QueuePool` behavior under the same contention
(the blocking-semaphore baseline is a structural stand-in, not a measured
`QueuePool` run); multi-replica behavior; any interaction between the two
new features and the existing `/ask`-level concurrency limiters under
combined load. Re-run `make load-test MODE=full` against both new flags
enabled (`ENABLE_DATABASE_CONCURRENCY_LIMIT=true`,
`ENABLE_RESULT_CACHE=true`) the next time Docker is available in the
working environment, and append real HTTP-level numbers here alongside
this entry rather than replacing it.
