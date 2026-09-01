# Contributing

This started as a personal/portfolio project, but contributions,
suggestions, and bug reports are welcome.

## Dev environment setup

Follow the **Setup** section in [`README.md`](README.md) — clone, create a
venv, `pip install -r requirements.txt`, copy `.env.example` to `.env` and
point it at a real (ideally read-only, non-production) database. The
mocked `pytest` suite doesn't need a real database or Ollama; the benchmark
harness (`scripts/run_benchmark.py`) and `scripts/integration_test.py` do.

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

## Adding a new benchmark case

Benchmark cases live in `eval/benchmark/*.yaml` (split by
difficulty/category — `easy.yaml`, `medium.yaml`, `hard.yaml`,
`real_world.yaml`, `adversarial.yaml`, `follow_up.yaml`,
`cost_estimation.yaml`) and run via `python scripts/run_benchmark.py` — a
real-DB, real-Ollama check separate from the mocked `pytest` suite, so it's
not run in CI. See `eval/schema.py`'s `BenchmarkCase` for the full field
reference; the essentials:

```yaml
cases:
  - id: unique_snake_case_id            # stable -- used as the regression-baseline key
    question: "Your natural-language question here"
    database: AdventureWorksDW2025
    difficulty: easy                    # easy | medium | hard | real_world | adversarial
    category: simple_filtering          # see eval/schema.py's KNOWN_CATEGORIES
    expected_sql: "SELECT ... "         # gold SQL -- executed live to derive the correct
                                         # result set; the agent's SQL is graded by comparing
                                         # its ACTUAL result against this, not by text
                                         # similarity (see eval/evaluators.py's module docstring)
    expected_tables: [SomeTable]        # optional -- for retrieval-recall/join-correctness checks
    alternative_sql: []                 # optional -- other equally-correct gold SQL (ambiguous questions)
    notes: >-
      Why this question is here -- which bug or scenario it guards against.
```

**Verify `expected_sql` against the real database before committing it** —
run it yourself (`scripts/test_db_connection.py`'s engine, or any SQL
client) and confirm it returns a sensible result. A wrong gold query fails
silently at eval time (`eval.evaluators.fetch_gold_result` logs an error
and the case is skipped as "not applicable," not "the agent got it wrong")
— see every case's `notes` in the existing files for the pattern of citing
what was actually verified.

`expected_tables` matters more than it looks: a query can skip a required
join hop and still return a plausible, non-empty, readable result by pure
key-range coincidence (this happened during development — see
`real_world.yaml`'s `ambig_bikes_top_territory` for the real example).
Result-set comparison alone won't always catch that if the coincidence
also happens to produce the right-looking numbers; asserting a specific
table was actually used closes that gap.

Prefer questions that guard against a specific failure mode you found (a
wrong join, a missed filter-value match, a raw key leaking into the
output) over generic coverage — the `notes` field should say what would
have shipped silently without this entry. For a follow-up (multi-turn)
case, use `followup_cases`/`turns` instead — see `follow_up.yaml` for the
shape.

## Reporting issues

Open a GitHub issue using the provided template. For anything that looks
like a security issue (a way to get the validator to accept a non-SELECT
statement, for example), see [`SECURITY.md`](SECURITY.md) first.
