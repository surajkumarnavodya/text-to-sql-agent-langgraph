# Deployment

How to run this project outside a developer's local `pip install` +
`uvicorn` workflow — Docker/Compose, environment configuration, connecting
containers to an external Ollama and database, and what's deliberately
**not** included (Kubernetes, a bundled database, a bundled LLM server).

Read [`SECURITY.md`](../SECURITY.md) and
[`docs/PRODUCTION_CHECKLIST.md`](PRODUCTION_CHECKLIST.md) before deploying
this anywhere beyond your own machine — this document covers *how*, not
*whether you should yet*.

## What's provided, and what isn't

- **Provided:** a multi-stage `Dockerfile` (a Node stage builds the React
  dashboard, a Python stage — non-root, pinned deps, health-checked —
  serves both the REST API and that build) and a `docker-compose.yml`
  running one `api` service from that image ([`docs/API.md`](API.md)).
  A separate Streamlit UI container used to ship alongside the API; it's
  gone now that the React dashboard is served directly by the API process.
- **Not provided, by design:** Ollama and the target database as
  containers. Both are external/user-provided per this project's own
  architecture (config-driven "connect to your own database," a fully
  local Ollama you already run) — bundling either would contradict
  "your own database" and would make the image responsible for a stateful
  LLM server it has no reason to own.
- **Not provided:** Kubernetes manifests. A single Compose deployment is
  the right scale for this project today (single-region, no need for
  autoscaling beyond stateless API replicas) — see this document's
  "If you outgrow this" section for when that might change, and
  `docs/PRODUCTION_READINESS_REPORT.md`'s V2 roadmap for the reasoning.

## Quick start

```bash
cp .env.example .env
# edit .env: point DB_* at your database (a genuinely read-only account --
# see SECURITY.md), and OLLAMA_HOST at your Ollama server.

docker compose build
docker compose up -d

# One-time (and after any real schema change):
docker compose exec api python scripts/build_embeddings.py

# Dashboard: http://localhost:8000/
# API:       http://localhost:8000/health
```

`docker compose config` will render your actual `.env` values into its
output (including secrets) — don't paste that output anywhere, and be
careful running it in a shared terminal/CI log.

## Connecting to Ollama running on the host

The `api` service declares `extra_hosts: ["host.docker.internal:host-gateway"]`
in `docker-compose.yml`. Set in `.env`:

```
OLLAMA_HOST=http://host.docker.internal:11434
```

This resolves automatically on Docker Desktop (Windows/Mac). On Linux
(Docker Engine 20.10+), `host-gateway` makes it resolve too. If your
Ollama runs somewhere else entirely (another host, a dedicated Ollama
container/server), just point `OLLAMA_HOST` at that instead — nothing else
needs to change.

## Connecting to an external database

Same `.env` mechanism as running locally — `DB_TYPE`/`DB_HOST`/`DB_PORT`/
etc., or `DB_CONNECTION_STRING` as a full override (see
`.env.example`, `docs/CONFIGURATION.md`). Nothing Docker-specific here:
the containers reach out to whatever host your `.env` names, same as the
local dev workflow.

**`DB_TYPE=mssql` note:** this image's `unixodbc-dev` covers pyodbc's
*build* requirement, not Microsoft's ODBC Driver 17/18 for SQL Server
itself (a separate, larger install from Microsoft's own apt repo). If
you're connecting to SQL Server, extend the base image:

```dockerfile
FROM text-to-sql-dashboard:latest
USER root
RUN curl -sSL -O https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/* packages-microsoft-prod.deb
USER app
```

Kept out of the base image so Postgres/MySQL/Oracle-only deployments
don't carry that extra weight — see `Dockerfile`'s own comment on this.

## Persistent vector storage

The Chroma schema index lives in a named Docker volume (`chroma_index`,
mounted at `/app/embeddings/.chroma`) so it survives container
restarts/recreates without needing `build_embeddings.py` re-run every
time. Rebuilding the image does **not** clear this volume; only
`docker compose down -v` or an explicit `docker volume rm` does.

After a real schema change:

```bash
docker compose exec api python scripts/build_embeddings.py
```

(Cheap to run when nothing changed — `embeddings.schema_indexer.build_index`
skips re-embedding if the schema fingerprint hasn't changed.)

## Multi-source RAG / web search (optional)

Nothing Docker-specific here either if you turn these on
(`docs/MULTI_SOURCE_GUIDE.md`) — same `.env` mechanism:

- **`RAG_STORE_CONNECTION_STRING`** must point at a **SQL Server 2025+/
  Azure SQL** database reachable from the container (native `VECTOR`
  column type — `rag/store.py`). If it's a separate host from your
  `DB_CONNECTIONS` database, it needs the same network reachability as
  any other external database (see "Connecting to an external database"
  above); if it's SQL Server, it needs the same `mssql` image-extension
  note as `DB_TYPE=mssql` above (`pyodbc`'s build requirement is covered,
  Microsoft's ODBC driver itself is not, by design).
- **`WEB_SEARCH_API_KEY`** (Tavily by default) means the container makes
  outbound HTTPS calls to a third-party API carrying question text — the
  one exception to this project's otherwise fully-local, no-external-calls
  posture (see `SECURITY.md`'s multi-source section). Confirm your
  network/firewall policy allows outbound HTTPS from the container before
  relying on this in a locked-down environment.

## Voice mode (on by default)

Nothing Docker-specific to configure — `ENABLE_VOICE_MODE=true` is the
default, and both `faster-whisper` and Piper run fully inside the
container. Two one-time model-download steps still apply the same as a
bare-metal install:

- **Piper voice model**: run once, inside the container, after it's up:
  ```bash
  docker compose exec api python scripts/download_voice_model.py
  ```
  `POST /voice/synthesize` returns a clean `503` until this has run — not
  a crash, just an unmet one-time setup step. Persist `voice/models/`
  as a named volume (mirroring `chroma_index` above) if you don't want to
  re-run this after every container recreate.
- **`faster-whisper`'s own model** auto-downloads from Hugging Face Hub on
  first use and caches on disk — the container needs outbound HTTPS
  reachability for that first transcription request (or a pre-warmed
  cache mounted in), same as any other on-first-use model download.

Set `ENABLE_VOICE_MODE=false` if you'd rather not carry either dependency
in a deployment that has no use for spoken input/output.

## AI-guided image editing (optional, off by default)

`ENABLE_IMAGE_EDITING` shares the exact same outbound-network/secret
consideration as `ENABLE_MEDIA_GENERATION` just below — real, metered
HTTPS calls to IMA Studio, gated behind the same `IMA_API_KEY`. No
separate Docker/volume concern beyond that: a generated edit is stored as
a normal attachment (`ATTACHMENT_STORAGE_DIR` — see
[`docs/CONFIGURATION.md`](CONFIGURATION.md)'s "Chat attachments" section),
not a separate cache.

## Media generation (optional, off by default)

Turning on `ENABLE_MEDIA_GENERATION` means the container makes outbound
HTTPS calls to IMA Studio's API carrying question text and (for a
confirmed generation) real, metered spend — the same "one exception to
the fully-local posture" consideration as `WEB_SEARCH_API_KEY` above.
Nothing else is Docker-specific: `IMA_API_KEY` is just another `.env`
secret (see "Production secrets" below), and generated media is cached
in-process (`media_gen/cache.py`, 100-entry FIFO) rather than written to a
volume — a container restart loses any not-yet-fetched generated media,
an accepted tradeoff, not something to work around with a mount.

## Media search (optional, off by default)

The heaviest optional feature to run in a container, in three ways:

1. **A real dependency-footprint increase.** `ENABLE_MEDIA_SEARCH=true`
   pulls `torch`/`sentence-transformers` (local CLIP embeddings) and
   `opencv-python`/`scenedetect` (video scene detection) into the image —
   meaningfully larger than the rest of this project's dependencies (see
   `requirements.txt`'s own comments). Leave the flag off for a
   deployment that doesn't need this feature rather than paying that image
   size unconditionally.
2. **Tesseract is not in the base image.** OCR (`media/ocr.py`, via
   `pytesseract`) needs the system Tesseract binary, the same "extra
   manual layer" shape as `DB_TYPE=mssql`'s ODBC driver above — it is
   **not** installed by this project's `Dockerfile`. Add it yourself if
   you need OCR:
   ```dockerfile
   FROM text-to-sql-dashboard:latest
   USER root
   RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
       && rm -rf /var/lib/apt/lists/*
   USER app
   ```
   Without it, `media/ocr.py` fails open — indexing still succeeds, on-
   screen text in video frames just isn't part of what's searchable, not
   a hard failure.
3. **A real library folder must be mounted into the container.**
   `MEDIA_LIBRARY_PATH` needs to resolve to something inside the
   container's filesystem — bind-mount or volume-mount your actual media
   folder at that path in `docker-compose.yml`, then run indexing the same
   way schema embeddings are built:
   ```bash
   docker compose exec api python scripts/build_media_index.py
   ```
   Persist a Chroma-backed media index the same way the schema index is
   (the `chroma_index` volume already mounted at `/app/embeddings/.chroma`
   covers this — media search reuses the same `PersistentClient`, no
   second volume needed).

Captioning (`MEDIA_VISION_MODEL`) reuses your existing Ollama connection —
no additional container-networking concern beyond what "Connecting to
Ollama running on the host" above already covers, just an additional
`ollama pull <model>` on whichever Ollama instance `OLLAMA_HOST` points at.

## Health checks

- **`api`**: `docker-compose.yml`'s `healthcheck` hits `GET /health`
  ([`docs/API.md`](API.md)), which actually verifies the database, Ollama,
  and the Chroma index are all reachable — a real dependency check, not
  just "the process is running."
- **`GET /live`** (enterprise scalability assessment, 2026-09-27) is a
  separate, near-zero-cost liveness probe that never touches the database,
  Chroma, or Ollama — just "is this process able to answer HTTP at all."
  If you move to an orchestrator that distinguishes liveness from readiness
  (Kubernetes, ECS, Nomad), point the frequently-polled *liveness* probe at
  `/live` and the less-frequent *readiness*/startup probe at `/health` —
  polling `/health` at liveness-probe frequency across many replicas would
  otherwise turn health-checking itself into real, avoidable DB/Chroma/
  Ollama load. `docker-compose.yml`'s own healthcheck stays on `/health`
  (Compose has no separate liveness/readiness concept, and its 30s interval
  is infrequent enough not to matter).
- **Graceful shutdown**: on `SIGTERM`, the API drains its bounded `/ask`
  thread pool (waits for in-flight requests to finish rather than
  abandoning them) before the process exits. Set your orchestrator's
  termination grace period comfortably above `REQUEST_TIMEOUT_SECONDS`
  (default 600s) so a slow in-flight request isn't killed mid-shutdown
  anyway — a grace period shorter than that defeats the point of draining.

## Reverse proxy and auth

By default (nothing configured), neither the dashboard nor the API has
real authentication (see [`docs/RISK_REGISTER.md`](RISK_REGISTER.md)'s
R-001, [`docs/API.md`](API.md)'s "Auth" section) — suitable only for
local/trusted-network use. **Two of `Settings.auth_mode`'s four values do
provide real, per-user identity**: `local` (`LOCAL_AUTH_ENABLED=true`,
this app's own self-hosted accounts, optionally including Google sign-in —
see below) and `oidc` (`OIDC_ISSUER` set, any standard external identity
provider) — configure one of these for a genuinely multi-user deployment,
rather than relying on the reverse-proxy pattern below as the only
boundary. For anything still short of that (or as defense-in-depth on top
of it), put an authenticating reverse proxy in front of the service — e.g.
`oauth2-proxy`, or your platform's managed auth/ingress layer. The service
needs no code to know this exists; point the proxy at `api:8000` and
terminate TLS + auth there. The API's optional `API_AUTH_TOKEN`
shared-secret check can also layer underneath either mode (defense in
depth) but should never be the *only* layer for anything but a single
trusted caller.

### Local accounts, Google sign-in, and conversation sharing

If you set `LOCAL_AUTH_ENABLED=true`, run the identity database's
migrations **before** first use (and after pulling any update that
touches `identity/models.py` — including this project's own Google
sign-in and conversation-sharing tables):

```bash
alembic -c identity/alembic.ini upgrade head
```

Then seed the RBAC roles/permissions tables (idempotent — safe, and
necessary, to re-run after *any* update that adds a new permission code,
including this one, since this codebase has no automatic "re-seed on
startup" hook):

```bash
python scripts/bootstrap_admin.py
```

To also enable **Google sign-in**, set `GOOGLE_OAUTH_CLIENT_ID` (see
[`docs/AUTHENTICATION.md`](AUTHENTICATION.md#google-sign-in-2026-09-28)
for the Google Cloud Console setup steps — a public client ID only, no
secret involved). To allow an owner to create "anyone with the link"
shares (off by default), set `SHARE_PUBLIC_LINKS_ENABLED=true` — see
[`docs/SHARING_SECURITY.md`](SHARING_SECURITY.md) and
`docs/CONFIGURATION.md`'s own reference table for every tunable.

**Once you put a reverse proxy/load balancer in front of this app, also
set `TRUSTED_PROXY_COUNT`** (default `0`) to the exact number of proxy
hops between the internet and this process — usually `1`. Without this,
every rate limiter and audit-log entry keyed by client IP
(`security/client_ip.py`) sees only the proxy's own address for every
caller, collapsing every distinct user into one shared rate-limit bucket —
a real availability problem (innocent users rate-limited together), not
just an inaccuracy. This is deliberately opt-in and an *exact hop count*,
never "trust the header if present" — a misconfigured count fails closed
to the direct TCP peer rather than trusting a client-suppliable header.

`docker-compose.yml`'s port mapping is bound to `127.0.0.1` by default
(`${API_BIND_HOST:-127.0.0.1}:8000:8000`) — an unqualified `"8000:8000"`
mapping binds every host interface, reachable from the whole network the
host sits on, not just the host itself. Set `API_BIND_HOST` in `.env`
(e.g. to `0.0.0.0`, or the specific interface your reverse proxy connects
from) only once that proxy is actually in place — the localhost-only
default is what makes "put a reverse proxy in front of it" a real
boundary rather than something a network-reachable port mapping already
bypassed.

## Horizontal scaling considerations

- **`api` is effectively stateless per-request** (LangGraph rebuilds
  the graph per call; the DB engine is a pooled, reused connection) —
  running multiple replicas behind a load balancer is safe for the request
  path itself.
- **Rate limiting is per-process, not distributed.** `agent/rate_limit.py`'s
  LLM-call limiter and `api/main.py`'s per-caller question limiter are
  in-memory (`SlidingWindowRateLimiter`) — multiple replicas each enforce
  their own independent limit, not a shared one (so N replicas effectively
  give N× the configured budget). Fine for one replica; revisit (a shared
  store like Redis) before running several replicas behind a load balancer
  if the rate limits need to mean what their numbers say across the whole
  deployment, not per-replica. What *is* fixed as of the enterprise
  scalability assessment (2026-09-27): every limiter now keys on real
  caller identity when one exists, and falls back to a correctly-resolved
  client IP (honoring `TRUSTED_PROXY_COUNT`, see "Reverse proxy and auth"
  above) rather than the load balancer's own address — without that fix,
  *every* caller behind one LB would previously have shared one bucket
  regardless of which user they were, a strictly worse problem than the
  still-open per-process-budget one described here.
- **Each replica's own DB connection pool bounds its real parallelism.**
  `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` (default 10 + 20 = 30 connections per
  configured database, per replica) is independent of
  `MAX_CONCURRENT_ASK_REQUESTS` (default 50) — a replica can *admit* more
  concurrent `/ask` requests than it can actually run against the database
  in parallel; the excess simply queues for a pooled connection rather than
  failing (a startup warning now flags this mismatch, see
  `api/main.py::_warn_on_ask_concurrency_pool_mismatch`). When sizing
  multiple replicas, remember the real constraint is `replicas × pool
  capacity ≤ your database server's own max_connections`, not just each
  replica's own numbers in isolation.
- **The Chroma index is the one piece of real shared state.** All replicas
  must mount the same `chroma_index` volume (or, for true multi-host
  scaling, move to Chroma's client-server mode or a hosted vector DB
  instead of the embedded/persistent-directory mode this project uses
  today — a real architectural change, not a config flag; see the V2
  roadmap in `docs/PRODUCTION_READINESS_REPORT.md`).
- **Ollama itself is the actual bottleneck** for concurrent load in
  practice (one local model, `p95_latency_seconds` ≈ 80s in the latest
  benchmark run — see `docs/EVALUATION.md`), not this app's own code.
  Scaling API replicas doesn't help if they're all waiting on the same
  single Ollama instance; a real concurrent-user deployment needs either a
  more capable Ollama host or a pool of them behind their own load
  balancer, which this project's `OLLAMA_HOST` config doesn't currently
  abstract over (one URL, not a pool).

## Backup & recovery

Prompt 25 (production readiness & release gate) closed a real,
previously-disclosed gap: no backup/recovery procedure existed anywhere
in this project's docs. Scoped honestly: this app is a **read-only**
client of whatever business database you connect it to
(`db.connection.get_read_only_engine`) — it never writes to that
database and has no opinion on, or mechanism for, backing it up. That
remains entirely your own infrastructure's responsibility, same as
before. What follows is a real procedure for the things *this app itself
owns*.

- **The identity database (only if `LOCAL_AUTH_ENABLED=true`)** — a
  normal Postgres database (`AUTH_DATABASE_URL`), containing accounts,
  sessions, chat history, onboarding jobs, the semantic catalog, and
  recommendation governance records. Back it up with standard Postgres
  tooling (`pg_dump`/`pg_basebackup`/your managed Postgres provider's own
  snapshot feature) on whatever schedule your data-retention policy
  requires — this app has no special requirements beyond "it's an
  ordinary Postgres database." Restore with the matching `pg_restore`/
  snapshot-restore, then confirm `alembic -c identity/alembic.ini
  current` reports the expected head revision before letting traffic
  back in.
- **The Chroma vector index** (`CHROMA_PERSIST_DIR`, default
  `embeddings/.chroma/`) — deliberately **not a backup/restore target**.
  It's a derived cache: `python scripts/build_embeddings.py --force`
  rebuilds it from the live connected database's own schema from
  scratch, and `python -m scripts.ingest_schema --database-id <name>`
  rebuilds the business-context knowledge-base collection from
  `data/knowledge/*.yaml`. Losing this directory entirely just means the
  next startup (or an explicit `POST /schema/refresh`) pays the
  embedding cost again — no data is actually lost, since the source of
  truth is the live database's own schema, not this cache.
- **`.env`/configuration** — this app deliberately never persists your
  `.env` anywhere itself (`.gitignore`d, `.dockerignore`d). Keep your own
  copy in a secrets manager or an encrypted, access-controlled location
  outside this repo — losing it means re-entering every connection
  string/API key/secret by hand, not losing application data, but still
  worth treating as a real recovery dependency.
- **Golden examples / message feedback** (`embeddings/golden_examples.py`,
  `feedback/store.py`) — both live inside the same Chroma persist
  directory as the schema index above. A full loss means these
  human-curated few-shot examples and feedback history are gone (unlike
  the schema index, there's no live source to rebuild them from) —
  if this content matters to your deployment, back up
  `CHROMA_PERSIST_DIR` as a filesystem-level backup (a plain directory
  copy/snapshot), separate from the identity database's own Postgres
  backup above.

## Rollback

Also new in this pass — a documented procedure where none existed.

- **Application code**: both base images in the `Dockerfile` are
  digest-pinned, and your own built application image should be tagged
  per release (e.g. by git SHA or version), not just `latest`. Rolling
  back is redeploying the previous tag — no code-level rollback
  mechanism is needed beyond your own image tagging discipline.
- **Identity database schema**: `alembic -c identity/alembic.ini
  downgrade -1` reverts the most recently applied migration;
  `downgrade <revision>` targets a specific one. Every one of this
  project's 9 migrations has a real `downgrade()` function (verified by
  direct read, not assumed) — but as with any schema downgrade, verify
  against a copy of production data first if the migration you're
  reverting involved a data transformation, not just a schema change
  (Alembic reverts schema; it does not re-derive data your application
  logic computed forward).
- **A bad new feature, without a code rollback at all**: every optional
  feature added since Prompt 08 (onboarding, semantic catalog,
  recommendation governance, Query Store intelligence, multi-tenancy's
  own per-database tenant binding, the result cache, the per-database
  concurrency limiter, ...) is gated behind its own `.env` flag,
  defaulting to the behavior that existed before that feature shipped.
  Setting the flag back (e.g. `ENABLE_RESULT_CACHE=false`,
  `ENABLE_DATABASE_CONCURRENCY_LIMIT=false`) and restarting the process
  is almost always faster and safer than rolling back a container image,
  and is the first thing to try if a specific feature (not the whole
  application) is suspected.

## If you outgrow this

Kubernetes becomes worth the operational overhead once you need: multiple
independently-scaled replicas with autoscaling policies, rolling deploys
across a fleet, or multi-region placement. None of that is this project's
current shape (a demo/small-team tool, per `SECURITY.md`). If you get
there, the natural migration is: containerize identically (this
`Dockerfile` needs no change), move the Chroma volume to a
`PersistentVolumeClaim` or an external Chroma/vector-DB service, and put
the rate limiters behind a shared store (Redis) first — Kubernetes itself
solves none of that on its own.

## Production secrets

- `.env` is never baked into the image (`.dockerignore`) and never
  committed (`.gitignore`) — pass it via `docker compose`'s `env_file`,
  a secrets manager injecting environment variables, or your
  orchestrator's native secret mechanism. Don't bake secrets into a custom
  image layer (they'd persist in image history even if a later layer
  removes the file).
- `DB_PASSWORD`/`DB_CONNECTION_STRING`/`API_AUTH_TOKEN`/
  `RAG_STORE_CONNECTION_STRING`/`WEB_SEARCH_API_KEY` are wrapped in
  `security.secrets.SecretStr` the moment `Settings` is constructed — never
  logged in plaintext by this app's own code (`SECURITY.md`). This does
  not protect against `docker compose config` rendering your `.env` values
  to the terminal (a `docker compose` behavior, not this app's) — avoid
  running that in a shared/logged context.

## Reproducible builds

`requirements.txt` is fully version-pinned (see its own header comment).
Both base images the `Dockerfile` uses — `python:3.11-slim` (the final
application image) and `node:22-slim` (the frontend-build stage) — are
digest-pinned (`@sha256:...`), not just tag-pinned: a floating tag can be
silently repointed at a different image by the upstream maintainer at any
time, the digest can't. Re-verify and update either digest deliberately (a
real, reviewed bump), not automatically — see each `FROM` line's own
comment in the `Dockerfile` for how the digest was obtained.
