.PHONY: setup check-db embed run test lint format clean

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

run: check-db embed
	$(VENV)/bin/streamlit run ui/app.py

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
