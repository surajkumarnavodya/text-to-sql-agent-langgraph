# Evaluation

The Text-to-SQL benchmark's methodology and the **actual latest measured
results** — reported here honestly, including the numbers that don't look
good, per this project's own stated principle that accuracy claims should
be backed by real numbers, not feature-list language (see
`docs/RESPONSIBLE_AI.md`'s "Overreliance" section).

## Methodology summary

Full detail lives in code (`eval/schema.py`, `eval/evaluators.py`,
`eval/metrics.py`) and `README.md`'s feature description; the essentials:

- **Execution-accuracy first, not SQL-text similarity.** Gold SQL
  (`expected_sql`/`alternative_sql`) is executed live against the real
  database; the agent's actual result set is compared against that gold
  result set (`eval/evaluators.py::evaluate_result_set`,
  `compare_result_sets` — type-tagged, tolerance-rounded, row-order-
  agnostic unless the case specifically tests `ORDER BY`).
  `exact_sql_match` is computed too but is explicitly diagnostic-only,
  never gating pass/fail (`eval/evaluators.py`'s own module docstring: "do
  not optimize for SQL string similarity alone").
- **57 cases** across `eval/benchmark/*.yaml`: easy, medium, hard,
  real_world, adversarial difficulty tiers; 20+ category subtypes (joins,
  aggregation, date filtering, GROUP BY/HAVING, window functions, nested
  queries, follow-up conversations, prompt-injection/malicious-SQL/
  unauthorized-data adversarial cases, cost-estimation, ...).
- **Regression detection** against a stored baseline
  (`eval/baselines/latest.json`) — `python scripts/run_benchmark.py
  --check-regression` exits non-zero if accuracy, retrieval, security-
  rejection, or latency/cost regresses beyond tolerance
  (`eval/regression.py`).

## Latest measured results

**Run:** `run_20260901T151533Z` · 2026-09-01 · model `llama3.1:8b` ·
database `AdventureWorksDW2025` (SQL Server) · 57 cases. Full artifacts:
`eval/results/run_20260901T151533Z.json` (compact),
`_full.json` (with generated SQL/rows), `live_run_output.log` (519-line
narrated log). This is also this project's currently-saved baseline
(`eval/baselines/latest.json`).

### Headline numbers

| Metric | Value | Reading it |
|---|---|---|
| `sql_execution_accuracy` | **92.3%** | The agent produced *a* successful execution most of the time. |
| `result_set_accuracy` | **29.6%** | ...but the result was only correct 3 times in 10. This is the gap that matters. |
| `final_accuracy` | **35.0%** | The overall pass/fail signal (`compute_overall_pass` — combines execution, result-set correctness, and security-classification expectations). |
| `exact_sql_match` | 0.0% | Diagnostic only, never gates pass/fail — 0% here just means the model never produces textually-identical SQL to the gold reference, which is expected and not itself concerning. |
| `schema_retrieval_recall` | 82.2% | The right tables are usually retrieved... |
| `relevant_table_precision` | 25.0% | ...but a lot of *irrelevant* tables come along too, mostly via FK-adjacency bridge expansion casting a wide net. |
| `security_rejection_accuracy` | **100%** | Every one of the 20 adversarial cases was correctly rejected/handled. The one dimension performing at ceiling. |
| `average_latency_seconds` / `p95_latency_seconds` | 33.3s / 79.5s | Local 8B-model, CPU/GPU-bound. A real interactive-use consideration, not just a benchmark artifact. |

### Per-difficulty breakdown

| Difficulty | Cases | Pass rate | Avg latency |
|---|---|---|---|
| adversarial | 17 | **100%** | 9.3s |
| easy | 6 | 67% | 25.9s |
| medium | 10 | 40% | 52.9s |
| hard | 8 | 25% | 57.3s |
| real_world | 16 | 25% | 37.2s |

Adversarial cases (security rejection) are both the fastest and the most
reliable — expected, since rejection happens before any LLM generation
attempt for most of them. Real-world and hard questions — the ones that
actually matter for a "chat with your database" use case — pass a quarter
of the time.

### Per-category breakdown (categories with ≥2 cases)

| Category | Cases | Pass rate |
|---|---|---|
| prompt_injection | 9 | 100% |
| malicious_sql | 4 | 100% |
| null_handling | 2 | 100% |
| schema_manipulation | 2 | 100% |
| simple_filtering | 2 | 100% |
| unauthorized_data | 2 | 100% |
| cost_estimation | 2 | 100% |
| follow_up | 10 | 40% |
| aggregation | 2 | 50% |
| date_filtering | 2 | 50% |
| joins | 2 | 50% |
| sorting | 2 | 50% |
| ambiguous_wording | 2 | 0% |
| business_questions | 2 | 0% |
| ctes | 2 | 0% |
| group_by_having | 2 | 0% |
| incomplete_questions | 2 | 0% |
| multi_table_analysis | 2 | 0% |
| nested_queries | 2 | 0% |
| window_functions (1 case) | 1 | 0% |

`window_function_correctness` and `ambiguous_question_handling_accuracy`
both measured **0.0%** as aggregate metrics — real, current weaknesses of
this model on this dataset, not edge cases to hand-wave past.

### A concrete failure mode worth knowing about

`eval/results/streamlit_run.log` captured one question ("Show total sales
by year and product name") retrying the full `MAX_RETRIES` budget (4
attempts) and generating **byte-identical SQL on attempts 2, 3, and 4**
despite the error-feedback prompt changing each time. `agent/llm_client.py`
runs generation at `temperature=0.0` (deterministic) — a real tension with
the self-correction loop's premise: if the retry prompt doesn't change the
model's reasoning enough, a deterministic model reproduces its own mistake
rather than exploring an alternative. In this observed case, 3 of 4
retries were pure wasted latency/LLM-call budget. Not fixed as part of
this audit's hardening pass (see `docs/PRODUCTION_READINESS_REPORT.md`'s
scope decisions) — flagged here as a concrete, evidenced limitation rather
than a hypothetical one.

## Running it yourself

```bash
python scripts/run_benchmark.py                    # full 57-case dataset
python scripts/run_benchmark.py --limit 20          # quicker partial run
python scripts/run_benchmark.py --check-regression  # compare against eval/baselines/latest.json
python scripts/run_benchmark.py --save-baseline     # record this run as the new baseline
```

Requires a live database connection and a running Ollama with the
configured model pulled — same requirements as the app itself. Not part of
the `pytest` suite or CI (`.github/workflows/ci.yml`'s own comment is
explicit about why) — see `CONTRIBUTING.md` for adding new cases.

`python scripts/monitoring_summary.py` prints a compact summary of the
current baseline (and, given `--log-file`, a captured run's security-event
counts) for periodic review — see `docs/GOVERNANCE.md`'s review cadence.

## What would move these numbers

Not attempted as part of this pass (would change AI-quality behavior,
outside this audit's "fix + Docker + docs, skip deep refactors" scope —
see `docs/PRODUCTION_READINESS_REPORT.md`):

- A larger or SQL-specialized model (`sqlcoder`, `duckdb-nsql`, or a bigger
  general model) — the most direct lever per `README.md`'s own "Known
  limitations."
- Retry-prompt diversification (e.g. a small temperature bump on retry
  attempts specifically, or explicitly instructing the model to try a
  structurally different approach) to address the identical-retry failure
  mode above.
- Tightening `relevant_table_precision` — the FK-adjacency bridge
  expansion's bridge-budget/tie-breaking logic
  (`docs/ARCHITECTURE.md`'s "§3... one sharp edge worth knowing") is a
  known, documented source of over-inclusion.
