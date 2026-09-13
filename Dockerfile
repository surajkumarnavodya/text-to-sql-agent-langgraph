# ---- Frontend build stage ----
# Builds the React dashboard (frontend/) into static files that the final
# image serves directly from the FastAPI process (see api/main.py's
# StaticFiles mount under `if _frontend_dist.is_dir():`) -- single origin,
# no separate web server/container, no CORS configuration needed in
# production. This replaced a separate Streamlit UI container that used to
# ship alongside the API; the API now serves the UI itself.
#
# Digest-pinned for the same reason the Python base image below is (see its
# comment) -- verified against Docker Hub's registry API at pin time
# (`docker-content-digest` for `node:22-slim`), not guessed.
FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ---- Python application image ----
# Base matches this project's actual target Python (pyproject.toml's
# `requires-python = ">=3.11"`, CI's python-version: "3.11") -- NOT the
# 3.14 this project's own dev machine happens to run (see CLAUDE.md's
# Python-version note); the pinned driver versions in requirements.txt were
# chosen for 3.14 wheel availability on that dev machine and have not been
# independently verified against 3.11 here -- see docs/RISK_REGISTER.md.
#
# Digest-pinned (not just the floating `3.11-slim` tag) -- the tag can be
# silently repointed at a different image by the upstream maintainer at any
# time; the digest can't. This is the manifest-*list* digest (multi-arch),
# verified against Docker Hub's own registry API at the time this was
# pinned (`docker-content-digest` response header for
# `python:3.11-slim`), not guessed -- resolves per-architecture at pull
# time exactly like the tag did, just immutably. Re-verify and update this
# digest deliberately (a real, reviewed bump), not automatically, the same
# change-controlled treatment `docs/GOVERNANCE.md` already gives other
# security-relevant config.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS base

# unixodbc-dev: build/runtime headers pyodbc needs for DB_TYPE=mssql (see
# requirements.txt's pyodbc pin). Does NOT include Microsoft's ODBC Driver
# 17/18 for SQL Server itself -- that's a separate, larger install (its own
# apt repo) only needed if you're actually connecting to SQL Server; see
# docs/DEPLOYMENT.md for the optional extra layer that adds it, kept out of
# this base image so postgres/mysql/oracle-only deployments stay smaller.
# build-essential: some transitive deps compile from source on slim images
# lacking a matching manylinux wheel; removed after pip install so it
# doesn't bloat the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential unixodbc-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user -- the app never needs root (it only opens outbound
# connections to the configured DB/Ollama and reads/writes its own
# Chroma persist directory), so it doesn't run as one.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home --shell /bin/bash app

WORKDIR /app

# Dependencies installed before the rest of the source so this layer is
# cached across ordinary code changes (only invalidated when
# requirements.txt itself changes) -- requirements.txt is fully pinned
# (see its own header comment), so this build is reproducible.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
# Overlays the frontend build stage's output onto the source copied above --
# frontend/dist is gitignored (build output, not source), so this is the
# only place it exists in the image.
COPY --from=frontend-build /frontend/dist ./frontend/dist

# embeddings/.chroma is where the schema index persists -- created here
# (and owned by `app`) so it exists as a valid mount point even before
# scripts/build_embeddings.py has ever run against a fresh volume.
RUN mkdir -p embeddings/.chroma && chown -R app:app /app

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Serves both the REST API and the built React dashboard (see the
# StaticFiles mount this comment points to above) from the same process.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
