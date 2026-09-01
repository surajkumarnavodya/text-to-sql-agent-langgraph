# Single-stage image serving both the Streamlit UI and the FastAPI layer
# (api/) from the same codebase -- docker-compose.yml runs two containers
# from this one image with different CMDs, rather than maintaining two
# images for what is otherwise identical code + dependencies.
#
# Base matches this project's actual target Python (pyproject.toml's
# `requires-python = ">=3.11"`, CI's python-version: "3.11") -- NOT the
# 3.14 this project's own dev machine happens to run (see CLAUDE.md's
# Python-version note); the pinned driver versions in requirements.txt were
# chosen for 3.14 wheel availability on that dev machine and have not been
# independently verified against 3.11 here -- see docs/RISK_REGISTER.md.
FROM python:3.11-slim AS base

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

# embeddings/.chroma is where the schema index persists -- created here
# (and owned by `app`) so it exists as a valid mount point even before
# scripts/build_embeddings.py has ever run against a fresh volume.
RUN mkdir -p embeddings/.chroma && chown -R app:app /app

USER app

# Streamlit's own built-in health endpoint (no app code needed) -- used by
# docker-compose.yml's `app` service. The `api` service overrides this
# with its own HEALTHCHECK hitting GET /health (see docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

EXPOSE 8501 8000

# Default: the Streamlit UI. docker-compose.yml's `api` service overrides
# this CMD to run `uvicorn api.main:app` instead, from the same image.
CMD ["streamlit", "run", "ui/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
