# Contributing

This started as a personal/portfolio project, but contributions,
suggestions, and bug reports are welcome.

## Dev environment setup

Follow the **Setup** section in [`README.md`](README.md) — clone, create a
venv, `pip install -r requirements.txt`, copy `.env.example` to `.env` and
point it at a real (ideally read-only, non-production) database. The
mocked `pytest` suite doesn't need a real database or Ollama; the eval
harness (`scripts/run_eval.py`) and `scripts/integration_test.py` do.

## Coding standards

This codebase optimizes for being explainable, not just working — see
[`CLAUDE.md`](CLAUDE.md) for the full design rationale. In short:

- Type hints and a docstring on every public function.
- No `print()` outside `ui/app.py` and the standalone CLI scripts in
  `scripts/` — everything else uses the `logging` module.
- Never log a connection string, password, or full result row — log
  `DB_TYPE`/`DB_NAME`/table names/row counts only. This is a real security
  property of the codebase, not a style preference.
- Formatting/linting/type-checking config lives in `pyproject.toml`
  (Black, line length 100; Ruff with `E,F,I,UP,B,SIM`; mypy). Don't
  hand-tune formatting — run the formatter.

## Before submitting a PR

```bash
pytest                          # mocked unit tests -- must pass
ruff check . && black --check . && mypy .   # must be clean
```

(Or `.\tasks.ps1 test` / `.\tasks.ps1 lint` on Windows.) If your change
touches `agent/sql_validator.py` or anything on the SQL-execution path,
say so explicitly in the PR description — that module is the app's
security boundary (see CLAUDE.md's "SQL is untrusted output, always"), and
changes there get read more carefully.

## Adding a new eval question

Eval questions live in `eval/eval_questions.yaml` and run via
`python scripts/run_eval.py` — a real-DB, real-Ollama check separate from
the mocked `pytest` suite, so it's not run in CI. To add one:

```yaml
- question: "Your natural-language question here"
  min_rows: 1                     # fail if the query returns fewer rows than this
  expect_readable_result: true    # fail if every column in the first row is a bare int (a raw surrogate key)
  expect_tables_used:             # fail if the executed SQL doesn't reference every table listed here
    - SomeTableName
  notes: >-
    Why this question is here -- which bug or scenario it guards against.
```

`expect_tables_used` matters more than it looks: a query can skip a
required join hop and still return a plausible, non-empty, readable result
by pure key-range coincidence (this happened during development — see the
Bikes-category entry's notes for the real example). Row count and
readability alone won't catch that; asserting a specific table was
actually used in the SQL will.

Prefer questions that guard against a specific failure mode you found
(a wrong join, a missed filter-value match, a raw key leaking into the
output) over generic coverage — the `notes` field should say what would
have shipped silently without this entry.

## Reporting issues

Open a GitHub issue using the provided template. For anything that looks
like a security issue (a way to get the validator to accept a non-SELECT
statement, for example), see [`SECURITY.md`](SECURITY.md) first.
