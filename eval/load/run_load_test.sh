#!/usr/bin/env bash
# Drives the full Phase 0 load-test sequence: up -> migrate -> seed ->
# k6 -> down. Used by both `make load-test` (repo-root Makefile) and the
# `load-test-smoke` CI job (.github/workflows/ci.yml) -- one script, not
# duplicated logic in both places. See docs/SCALE_OUT_PROMPT.md's Phase 0
# section.
#
# Usage: eval/load/run_load_test.sh [smoke|full]
#   smoke (default) -- scenario_ask_flow.js only, short ramp. What CI runs
#                       on every PR.
#   full             -- scenario_ask_flow.js + scenario_burst.js, for a
#                        deliberate local baseline session (what produced
#                        docs/SCALE_BASELINE.md's numbers).
set -euo pipefail

MODE="${1:-smoke}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$SCRIPT_DIR/docker-compose.loadtest.yml" --env-file "$SCRIPT_DIR/.env.loadtest")
NETWORK="load_default"

cleanup() {
  echo "[load-test] tearing down..."
  "${COMPOSE[@]}" down -v --remove-orphans || true
}
trap cleanup EXIT

if [ ! -f "$SCRIPT_DIR/.env.loadtest" ]; then
  echo "[load-test] .env.loadtest missing -- copying from .env.loadtest.example"
  cp "$SCRIPT_DIR/.env.loadtest.example" "$SCRIPT_DIR/.env.loadtest"
fi

# Deliberately NOT `--wait` here: /health only reports healthy once the
# schema index is actually built (below), so waiting on it at this point
# would hang forever -- a real ordering bug found by running this for
# real, not a hypothetical. postgres's own `depends_on: condition:
# service_healthy` is what actually gates the steps below.
echo "[load-test] building + starting stack..."
"${COMPOSE[@]}" up -d --build

echo "[load-test] applying identity migrations..."
"${COMPOSE[@]}" run --rm api alembic -c identity/alembic.ini upgrade head

# Real bug, found by running this for real: `loadtest_model_cache` (the
# sentence-transformers/Chroma ONNX cache, /home/app/.cache) is a fresh
# named volume with no pre-existing image content to inherit ownership
# from, so Docker creates its mountpoint as root -- but the api image
# runs as non-root user `app` (see the repo-root Dockerfile), which can't
# write there. One-time fix, as root, before anything tries to use it;
# harmless/fast on a second run (chown -R on an already-correct tree).
echo "[load-test] fixing model-cache volume ownership..."
"${COMPOSE[@]}" run --rm --user root api sh -c \
  "mkdir -p /home/app/.cache && chown -R app:app /home/app/.cache"

echo "[load-test] building schema embeddings..."
"${COMPOSE[@]}" run --rm api python scripts/build_embeddings.py

echo "[load-test] seeding RBAC + test users..."
"${COMPOSE[@]}" run --rm api python -m eval.load.seed_users

PROJECT_NAME="$("${COMPOSE[@]}" ps --format json 2>/dev/null | head -1 | python3 -c "import sys,json; d=json.loads(sys.stdin.readline() or '{}'); print(d.get('Project',''))" 2>/dev/null || true)"
NETWORK="${PROJECT_NAME:-load}_default"

# Real bug, found by running this for real: the sentence-transformers
# embedding model (~79MB) downloads into the now-correctly-owned cache
# volume lazily, on the *first* real question -- retrieve_schema_node
# needs it to embed the question text. Left lazy, k6's very first request
# eats a one-time ~3-4 minute cold-start cost and looks like a hang. One
# authenticated warm-up call here, against the real running service (not
# a one-off `run --rm`, which wouldn't warm the *persistent* service's
# own in-memory model), pays that cost once, up front, outside of k6's
# own measured numbers.
echo "[load-test] waiting for api to report healthy..."
for _ in $(seq 1 30); do
  "${COMPOSE[@]}" exec -T api curl -sf -m 5 http://localhost:8000/health >/dev/null 2>&1 && break
  sleep 5
done

echo "[load-test] warming the embedding model (one real authenticated /ask call)..."
"${COMPOSE[@]}" exec -T api python -c "
import json, urllib.request
login = json.dumps({'email': 'loadtest-user-0000@loadtest.example.internal', 'password': '${LOAD_TEST_USER_PASSWORD:-LoadTest!Passw0rd123}'}).encode()
req = urllib.request.Request('http://localhost:8000/auth/login', data=login, headers={'Content-Type': 'application/json'}, method='POST')
token = json.loads(urllib.request.urlopen(req, timeout=30).read())['access_token']
ask = json.dumps({'question': 'How many orders are there?'}).encode()
req2 = urllib.request.Request('http://localhost:8000/ask', data=ask, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
print('warm-up /ask status:', urllib.request.urlopen(req2, timeout=400).status)
"

echo "[load-test] running k6 (${MODE})..."
if [ "$MODE" = "full" ]; then
  docker run --rm --network "$NETWORK" -v "$SCRIPT_DIR/k6:/scripts" \
    -e BASE_URL=http://api:8000 -e LOAD_TEST_USER_COUNT="${LOAD_TEST_USER_COUNT:-50}" \
    grafana/k6 run /scripts/scenario_ask_flow.js
  docker run --rm --network "$NETWORK" -v "$SCRIPT_DIR/k6:/scripts" \
    -e BASE_URL=http://api:8000 -e LOAD_TEST_USER_COUNT="${LOAD_TEST_USER_COUNT:-50}" \
    grafana/k6 run /scripts/scenario_burst.js
else
  docker run --rm --network "$NETWORK" -v "$SCRIPT_DIR/k6:/scripts" \
    -e BASE_URL=http://api:8000 -e LOAD_TEST_USER_COUNT="${LOAD_TEST_USER_COUNT:-50}" \
    grafana/k6 run --duration 30s --vus 5 /scripts/scenario_ask_flow.js
fi

echo "[load-test] done."
