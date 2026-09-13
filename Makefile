.PHONY: setup check-db embed run frontend-install frontend-build frontend-dev test lint format clean

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@test -f .env || cp .env.example .env

check-db:
	$(PYTHON) scripts/test_db_connection.py

embed:
	$(PYTHON) scripts/build_embeddings.py

# Starts the FastAPI server (api/main.py), which also serves the React
# dashboard's built static files (frontend/dist) from the same process/port
# if `make frontend-build` has been run -- see api/main.py's StaticFiles
# mount. For frontend hot-reload during active frontend development, run
# `make frontend-dev` in a separate terminal instead (Vite proxies API
# calls to this same server).
run: check-db embed
	$(VENV)/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000

frontend-install:
	cd frontend && npm install

frontend-build:
	cd frontend && npm run build

frontend-dev:
	cd frontend && npm run dev

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/black --check .
	$(VENV)/bin/mypy .

format:
	$(VENV)/bin/black .
	$(VENV)/bin/ruff check --fix .

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
