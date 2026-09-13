# Troubleshooting

Common failure modes, what they mean, and what to actually do about them —
including several observed directly while building/auditing this project,
not just theoretical.

## Database connection failures

`python scripts/test_db_connection.py` (or the UI's "Test Connection"
button, or `GET /health`) classifies every connection failure via
`db.connection.ConnectionErrorCategory` — the category and guidance below
are exactly what you'll see, made browsable here:

| Category | What it means | What to do |
|---|---|---|
| `configuration` | `.env` itself is malformed or missing a required field before a connection was even attempted. | Check `.env` for missing/invalid `DB_*` settings — see `docs/CONFIGURATION.md`. |
| `driver_missing` | The Python driver package for your `DB_TYPE` isn't installed. | `pip install -r requirements.txt` — all four drivers are listed unconditionally so switching `DB_TYPE` never needs a fresh install. |
| `auth_failure` | The server was reachable but rejected the credentials. | Check `DB_USER`/`DB_PASSWORD`. If you rotated a password, restart the process — `Settings` is a cached singleton (see `docs/CONFIGURATION.md`). |
| `host_unreachable` | Couldn't establish a TCP connection at all. | Check `DB_HOST`, `DB_PORT`, VPN, and firewall rules. If running in Docker, see `docs/DEPLOYMENT.md`'s networking notes. |
| `database_not_found` | The server was reachable and auth succeeded, but the named database/catalog doesn't exist. | Check `DB_NAME` for a typo. |
| `timeout` | The connection attempt itself timed out (distinct from `QUERY_TIMEOUT_SECONDS`, which is about query *execution*). | Check network path/latency to the host. |
| `unknown` | Doesn't match any of the above known patterns. | Read the driver error text included alongside the category — it's never hidden, only classified. |

Classification is best-effort keyword matching on the underlying driver's
own error text (`db/connection.py::_classify_error`) — it's not
authoritative, and the full original error message is always included in
the result, not replaced by the category.

### "The connected database role appears to have write privileges"

A warning, not an error — surfaced by `db.connection.check_write_privileges`
in the UI sidebar and `scripts/test_db_connection.py`'s output. It means
your `DB_USER` has INSERT/UPDATE/DELETE grants, which this app never uses
but which is your real safety boundary if the SQL validator were ever
bypassed (see `SECURITY.md`'s "What is explicitly not guaranteed"). **Fix
it by pointing `DB_USER` at a genuinely read-only database role**, not by
suppressing the warning — this check itself can't restrict privileges, it
can only tell you they're wider than they should be. (This exact warning
fired during this project's own reference audit run against its dev
database — see `docs/RISK_REGISTER.md`'s R-002 area and
`docs/PRODUCTION_READINESS_REPORT.md` — it is a real, easy-to-hit
misconfiguration, not a hypothetical.)

### `SAWarning: Unrecognized server version info`

Harmless. SQLAlchemy's dialect has a known list of server versions it
recognizes for feature-detection purposes; a newer database engine version
than the driver ships metadata for triggers this warning but doesn't
prevent connecting or querying. Safe to ignore; update the driver package
if it bothers you.

## Ollama

- **"Reachable at ... " is false on `GET /health`, or `OllamaUnavailableError`
  in the terminal:** Ollama isn't running, or `OLLAMA_HOST` points
  somewhere wrong. Run `ollama serve` (or confirm it's running as a
  background service) and verify `curl $OLLAMA_HOST/api/tags` returns a
  model list. In Docker, see `docs/DEPLOYMENT.md`'s
  `host.docker.internal` guidance.
- **Model not found:** `ollama pull <model>` for whatever `OLLAMA_MODEL`
  names (default `llama3.1:8b`).
- **Very slow responses:** expected for a local model on modest hardware —
  the latest benchmark run measured `p95_latency_seconds` ≈ 80s (see
  `docs/EVALUATION.md`). Not a bug; see that document's "why" for what
  drives it.

## Chroma / schema index

- **"Chroma index is empty -- run scripts/build_embeddings.py"**
  (`SchemaRetrievalError`, surfaced by the UI, `POST /ask`, or
  `GET /health`'s `schema_index.ok: false`): the index hasn't been built
  yet for this database. Run `python scripts/build_embeddings.py` (or the
  UI's "Refresh Schema" button) once, and again after any real schema
  change.
- **Schema browser shows stale tables after a schema change:** the Chroma
  index only re-embeds when `db.schema_introspection.get_schema_fingerprint()`
  changes — re-run `build_embeddings.py` explicitly (it's cheap to run
  when nothing changed, since it skips re-embedding on a fingerprint match).

## Windows / ODBC (`DB_TYPE=mssql`)

Requires the Microsoft ODBC Driver for SQL Server installed as a *system*
package (Control Panel → ODBC Data Sources on Windows, `odbcinst -j` to
check on Linux/macOS) — not pip-installable, and the one `DB_TYPE` with
this extra manual step. `DB_ODBC_DRIVER` in `.env` must exactly match an
installed driver name (`"ODBC Driver 17 for SQL Server"`,
`"ODBC Driver 18 for SQL Server"`, ...). In Docker, see
`docs/DEPLOYMENT.md`'s mssql section for the extra image layer this needs.

## Quality gate (lint/type-check/test) failures

- **`ruff check .` fails:** usually an unused import or an unsorted
  import block — `ruff check --fix .` auto-fixes most of these; review
  the diff before trusting it blindly.
- **`black --check .` fails:** run `black .` to auto-format; it never
  needs manual intervention.
- **`mypy .` fails:** read the actual error before reaching for
  `# type: ignore` — a surprising number of real bugs surface this way
  (this project's own production-readiness audit found several: a loop
  variable silently reused across two different-typed loops, a dict-vs-
  dataclass access mismatch, a `sqlglot.find_all()` call passed a tuple
  where variadic args were expected). Reserve `# type: ignore[<code>]`
  with a one-line comment for genuine typeshed/stub limitations (e.g. a
  decorator whose stub doesn't expose a method that really exists at
  runtime) or for spreading an inherently-arbitrary dict of test
  overrides into a dataclass constructor — not as a first response to an
  error you haven't read.
- **`pytest` fails after a `.env`-adjacent change:** most tests build
  their own `Settings(...)` explicitly and never read your real `.env` —
  a failure after an unrelated `.env` edit usually means something else
  changed. Tests that *do* need a live DB/Ollama are excluded from
  `pytest` by design (`scripts/integration_test.py`,
  `scripts/run_benchmark.py` — see `CONTRIBUTING.md`).

## Docker

- **`docker build`/`docker compose up` fails with "cannot connect to the
  Docker daemon":** Docker Desktop (or the Docker Engine service) isn't
  running. Start it before running any `docker`/`docker compose` command.
- **`docker compose config` prints your real `.env` values, including
  secrets, to the terminal.** This is standard Compose behavior (it
  resolves and displays the final interpolated config), not a bug in this
  project — but don't run it in a shared terminal, CI log, or anywhere the
  output might be captured/shared. Use it locally to validate syntax, not
  as a routine command.
- **`mssql` connections fail inside a container but work locally:** see
  this document's Windows/ODBC section and `docs/DEPLOYMENT.md`'s note on
  extending the base image with Microsoft's ODBC driver package — it's not
  included by default.

## API process crashes after a query with a date column

A real, observed segfault: `pandas==2.2.3` predates real Python 3.14
wheels and crashes (`Windows fatal exception: access violation` deep in
`pandas.core.arrays.datetimes._construct_from_dt64_naive`) building a
`DataFrame` from *any* row data containing a raw `datetime.datetime`
value — i.e. any query result with a date/datetime column, via
`api/main.py`'s `pd.DataFrame(rows, columns=columns)`. This kills the
whole API process (not a catchable Python exception), which is what the
browser shows as a failed/dropped `/execute` request. **Fixed** by pinning
`pandas==2.3.3` in `requirements.txt` (see its own comment) — if you see
this exact crash shape after touching dependency pins, check that pin
hasn't regressed.
Reproduces in two lines with no app code involved:
```python
import datetime, pandas as pd
pd.DataFrame([(1, datetime.datetime(1990, 1, 1))], columns=["id", "d"])
```

## "Invalid object name 'X'" for a table that genuinely exists

Happens on a database whose real tables live under a **non-default
schema** (`DB_<NAME>_SCHEMA` set to something other than `dbo`/`public`) —
an engine resolves an unqualified table name against the *connecting
user's own default schema*, not the schema you configured for
introspection. The model correctly sees and generates `FROM Employee`
(schema-qualification was never part of the DDL it was shown — see
`CLAUDE.md`'s multi-database note on why `table_descriptions.yaml`/schema
DDL stay bare-named), but bare `Employee` then fails to resolve if the
connection's actual default schema is `dbo` and the table is really
`employee.Employee`. **Fixed** by `agent.sql_validator.qualify_table_schema`,
applied only at the execution step, transparently, using
`DB_<NAME>_SCHEMA` — you shouldn't see this anymore for a correctly
configured `DB_<NAME>_SCHEMA`. If you do, confirm the configured schema
name actually matches where the tables live (`SELECT SCHEMA_NAME(schema_id), name FROM sys.tables`
on SQL Server) rather than assuming the connection's default.

## Multi-source router picked the wrong source, or a source you configured isn't offered

See [`docs/MULTI_SOURCE_GUIDE.md`](MULTI_SOURCE_GUIDE.md#9-troubleshooting)
— almost always either a missed app restart after a `.env` change
(`Settings` is a cached singleton, same as every other setting — see
above), or a genuinely ambiguous/compound question. Check the
`[router] available=[...] sources=[...] reasoning=...` log line before
assuming something's broken.

## "Agent could not produce a working query: Gave up after N attempts"

This means every attempt failed validation, review, or execution and the
retry budget ran out — the last attempted SQL and error are shown below
the message; check the "🔁 Retry timeline" expander for the full sequence,
not just the last attempt.

A specific, previously-common root cause for a question involving a
per-group ranking ("top 3 X per Y") or a period-over-period comparison
("year-over-year growth"): the model reaching for `TOP N ... GROUP BY`
(which only limits the *total* result to N rows, not N per group) or
nesting one aggregate inside another (`AVG(CASE WHEN ... THEN SUM(x) ELSE
0 END)`, which every SQL engine rejects outright). Both are now caught
structurally rather than exhausting the retry budget blind:
`agent/sql_validator.py` rejects the nested-aggregate shape statically
before it ever reaches the database, with a targeted rewrite hint fed back
on the next attempt; and, for a question matching this same "non-trivial"
pattern, an up-front query plan (visible in the "🧭 Query plan" expander,
if it appears) and a plan-conformance check give the retry loop a semantic
signal beyond a raw driver error — see `docs/ARCHITECTURE.md`'s "Agentic
query planning and plan-conformance review" for the full mechanism. If you
still see this failure on a question of this shape, confirm
`ENABLE_QUERY_PLANNING` is `true` (the default) in `.env`, and check
whether a "🧭 Query plan" expander appeared at all — if it didn't, the
question may not have matched `agent/complexity.py`'s signal detection
(rephrasing with an explicit "per <group>" or "compared to last <period>"
often helps it match).

For any other repeated failure shape, a larger or SQL-specialized model
(`sqlcoder`, `duckdb-nsql` via `OLLAMA_MODEL`) remains the most direct
lever — see `docs/EVALUATION.md`'s measured accuracy numbers and known
limitations before assuming the app itself is broken.

## Voice mode

- **`POST /voice/synthesize` returns 503, or the dashboard shows a voice
  error when it tries to speak an answer.** The Piper voice model hasn't
  been downloaded yet — run `python scripts/download_voice_model.py` once
  (see `CLAUDE.md`'s "Voice mode" section). This is a one-time setup step,
  separate from `pip install`.
- **No microphone button, or `GET /health`'s `voice_enabled` is `false`.**
  `ENABLE_VOICE_MODE` is off (it defaults to `true`, so this usually means
  someone explicitly disabled it) — restart the app after changing it,
  same `Settings`-is-a-cached-singleton caveat as everywhere else.
- **No live word-by-word captions while speaking.** Live captions use the
  browser's own `SpeechRecognition` Web Speech API, only available in
  Chromium-based browsers — Firefox/Safari fall back to a manual "Done
  speaking" button instead. This doesn't affect transcription accuracy;
  the actual submitted text always comes from local `faster-whisper`, not
  the browser's caption.
- **`413`/a "recording exceeds limit" error.** `Settings.voice_max_upload_mb`
  (default 10MB) or `Settings.voice_max_duration_seconds` (default 30s)
  was exceeded — ask a shorter question, or have your administrator raise
  either limit in `.env`.
- **Transcription of schema-specific terms (table/column names) is
  inaccurate.** `voice/stt.py` biases Whisper toward your actual schema's
  vocabulary automatically (`_build_vocabulary_hint`) — this fails open
  (silently does nothing) if schema introspection itself errors, so a
  persistent mismatch is more likely a genuinely unusual/ambiguous term
  than a broken feature.

## Media generation

- **A generation request never seems to fire, or IMA API errors surface
  as a failed generation.** Confirm both `ENABLE_MEDIA_GENERATION=true`
  and a real `IMA_API_KEY` are set — an enabled flag with no key is not
  "available" to the router (same pattern as every other source; see
  `docs/MULTI_SOURCE_GUIDE.md`). Video generation shares its code path
  with image generation but hasn't been separately confirmed against a
  live IMA account — if only video fails, that's the most likely reason.
- **Nothing happens after asking to generate something.** By design
  (`Settings.require_generation_approval`, default `true`) — the router
  only proposes what would be generated; you must click **"▶ Generate
  image"/"▶ Generate video"** (or call `POST /generate/confirm`) before
  anything is actually created or charged. Check for that button/proposal
  in the response before assuming the request failed.
- **A generation request is answered as an ordinary data/chart question
  instead.** The router distinguishes "create new media" from "show me
  the data" fairly conservatively — try an explicit "generate an image
  of..."/"create a video of..." phrasing.
- **`MEDIA_GEN_VIDEO_DURATION_SECONDS` seems to have no effect, or the
  provider rejects the value.** The underlying video model has its own
  hard duration ceiling (commonly 4–15 seconds) — this setting can request
  a shorter clip within that range, but can't exceed what the model
  itself supports; an out-of-range value is rejected by the provider, not
  pre-validated here.

## Media search

- **The "🖼️ Media Search" page (or a chat question) says the feature is
  disabled.** Both `ENABLE_MEDIA_SEARCH=true` and a real, existing
  `MEDIA_LIBRARY_PATH` are required — same "flag alone isn't enough"
  pattern as every other source.
- **A file you know is in the library never turns up in search results.**
  Nothing is searchable until it's been indexed — run
  `python scripts/build_media_index.py` after adding files (`--force` to
  re-index files that already have an unchanged content hash on record).
- **`pip install`/first run fails or is unexpectedly slow when media
  search is enabled.** `sentence-transformers`/`torch` and
  `scenedetect`/`opencv-python` are real, meaningfully larger dependencies
  pulled in only by this feature (see `requirements.txt`'s own comments) —
  expect a bigger install and a first-run model download (the CLIP
  checkpoint, cached afterward) compared to the rest of this app.
- **On-screen text in a video frame is never matched by search.** OCR
  needs the system Tesseract binary installed separately (not
  pip-installable — see `CLAUDE.md`'s Windows-specific notes); a missing
  binary fails open (the segment is still indexed via transcript/caption
  text alone), so this looks like reduced recall, not an error.
- **Video segments have no generated caption, or captioning is skipped
  entirely.** `MEDIA_VISION_MODEL` is blank by default — set it to a
  pulled Ollama vision model (e.g. `llava`, via `ollama pull llava`) to
  enable captioning; a blank setting or any captioning-call failure fails
  open (indexed via transcript/OCR text only), never blocks indexing.
- **A search for something you're confident is in the library returns
  nothing, or an unrelated hit.** Search is embedding-based, not keyword
  matching — try describing the visual/audio content more directly
  ("crane lifting a beam" rather than a file name or an abstract label).

## "It rejected my question and I don't know why"

By design, a rejection message is deliberately generic
(`"I couldn't process that question. Try rephrasing it..."`) rather than
naming exactly which pattern or rule matched — see `SECURITY.md`'s
reasoning ("a rejection must not confirm to an attacker exactly what was
detected"). If you believe a *legitimate* question was rejected
incorrectly, check the terminal/application logs (`agent.input_guard`'s
own logger, `security.audit`'s structured event) for the actual reason
code, or open an issue with the exact question text per `SECURITY.md`'s
"Reporting a vulnerability" section if it looks like a false positive worth
fixing.
