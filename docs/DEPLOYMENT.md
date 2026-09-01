# Deployment

How to run this project outside a developer's local `pip install` +
`streamlit run` workflow — Docker/Compose, environment configuration,
connecting containers to an external Ollama and database, and what's
deliberately **not** included (Kubernetes, a bundled database, a bundled
LLM server).

Read [`SECURITY.md`](../SECURITY.md) and
[`docs/PRODUCTION_CHECKLIST.md`](PRODUCTION_CHECKLIST.md) before deploying
this anywhere beyond your own machine — this document covers *how*, not
*whether you should yet*.

## What's provided, and what isn't

- **Provided:** a `Dockerfile` (single image, non-root, pinned deps,
  health-checked) and a `docker-compose.yml` running two services from
  that image — the Streamlit UI (`app`) and the REST API
  (`api`, [`docs/API.md`](API.md)).
- **Not provided, by design:** Ollama and the target database as
  containers. Both are external/user-provided per this project's own
  architecture (config-driven "connect to your own database," a fully
  local Ollama you already run) — bundling either would contradict
  "your own database" and would make the image responsible for a stateful
  LLM server it has no reason to own.
- **Not provided:** Kubernetes manifests. A single Compose deployment is
  the right scale for this project today (single-region, no need for
  autoscaling beyond stateless UI/API replicas) — see this document's
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
docker compose exec app python scripts/build_embeddings.py

# UI:  http://localhost:8501
# API: http://localhost:8000/health
```

`docker compose config` will render your actual `.env` values into its
output (including secrets) — don't paste that output anywhere, and be
careful running it in a shared terminal/CI log.

## Connecting to Ollama running on the host

Both services declare `extra_hosts: ["host.docker.internal:host-gateway"]`
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
mounted at `/app/embeddings/.chroma` in both services) so it survives
container restarts/recreates without needing `build_embeddings.py` re-run
every time. Rebuilding the image does **not** clear this volume; only
`docker compose down -v` or an explicit `docker volume rm` does.

After a real schema change:

```bash
docker compose exec app python scripts/build_embeddings.py
```

(Cheap to run when nothing changed — `embeddings.schema_indexer.build_index`
skips re-embedding if the schema fingerprint hasn't changed.)

## Health checks

- **`app`** (Streamlit): Docker `HEALTHCHECK` hits Streamlit's own built-in
  `/_stcore/health` endpoint — no app code needed for this.
- **`api`**: `docker-compose.yml`'s `healthcheck` hits `GET /health`
  ([`docs/API.md`](API.md)), which actually verifies the database, Ollama,
  and the Chroma index are all reachable — a real dependency check, not
  just "the process is running."

## Reverse proxy and auth

Neither the UI nor the API has real authentication (see
[`docs/RISK_REGISTER.md`](RISK_REGISTER.md)'s R-001,
[`docs/API.md`](API.md)'s "Auth" section). For anything beyond
local/trusted-network use, put an authenticating reverse proxy in front of
both — e.g. `oauth2-proxy`, or your platform's managed auth/ingress layer.
Neither service needs to know this exists; point the proxy at
`app:8501`/`api:8000` and terminate TLS + auth there. The API's optional
`API_AUTH_TOKEN` shared-secret check can layer underneath this (defense in
depth) but should never be the *only* layer for anything but a single
trusted caller.

## Horizontal scaling considerations

- **`app`/`api` are effectively stateless per-request** (LangGraph rebuilds
  the graph per call; the DB engine is a pooled, reused connection) —
  running multiple replicas behind a load balancer is safe for the request
  path itself.
- **Rate limiting is per-process, not distributed.** `agent/rate_limit.py`'s
  LLM-call limiter and `api/main.py`'s per-IP question limiter are
  in-memory (`SlidingWindowRateLimiter`) — multiple replicas each enforce
  their own independent limit, not a shared one. Fine for one replica;
  revisit (a shared store like Redis) before running several replicas
  behind a load balancer if the rate limits need to mean what their
  numbers say across the whole deployment, not per-replica.
- **The Chroma index is the one piece of real shared state.** All replicas
  must mount the same `chroma_index` volume (or, for true multi-host
  scaling, move to Chroma's client-server mode or a hosted vector DB
  instead of the embedded/persistent-directory mode this project uses
  today — a real architectural change, not a config flag; see the V2
  roadmap in `docs/PRODUCTION_READINESS_REPORT.md`).
- **Ollama itself is the actual bottleneck** for concurrent load in
  practice (one local model, `p95_latency_seconds` ≈ 80s in the latest
  benchmark run — see `docs/EVALUATION.md`), not this app's own code.
  Scaling app/API replicas doesn't help if they're all waiting on the same
  single Ollama instance; a real concurrent-user deployment needs either a
  more capable Ollama host or a pool of them behind their own load
  balancer, which this project's `OLLAMA_HOST` config doesn't currently
  abstract over (one URL, not a pool).

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
- `DB_PASSWORD`/`DB_CONNECTION_STRING`/`API_AUTH_TOKEN` are wrapped in
  `security.secrets.SecretStr` the moment `Settings` is constructed — never
  logged in plaintext by this app's own code (`SECURITY.md`). This does
  not protect against `docker compose config` rendering your `.env` values
  to the terminal (a `docker compose` behavior, not this app's) — avoid
  running that in a shared/logged context.

## Reproducible builds

`requirements.txt` is fully version-pinned (see its own header comment).
The `Dockerfile`'s base image tag (`python:3.11-slim`) is not
digest-pinned — for a stricter reproducibility guarantee, pin it to a
specific digest (`python:3.11-slim@sha256:...`) once you've settled on a
base image you don't want to drift.
