# Retrieval Evaluation

Evaluation criteria and fixtures for the business-context vector retrieval
layer (`retrieval/`, see `docs/vector-retrieval-design.md` for the
architecture). This document is deliberately scoped to what can actually be
measured against this repository today.

**Disclosure**: this project already has a mature, execution-accuracy-first
Text-to-SQL benchmark (`eval/`, `scripts/run_benchmark.py`,
`docs/EVALUATION.md`/`docs/EVALUATION_CURRENT.md`) — grading gold SQL vs.
the agent's actual result set, not SQL-text similarity. This document does
**not** replace or duplicate that harness. It defines the *retrieval-specific*
criteria that harness doesn't currently measure (which tables/columns/
relationships/glossary terms/metrics/examples were actually retrieved, and
whether that changed the outcome), and proposes fixtures small enough to
run without a real Ollama call — a genuinely larger extension of `eval/`
itself (wiring retrieval metrics into `eval/metrics.py`/`eval/reporting.py`)
is named as follow-up work, not attempted here.

---

## 1. Evaluation criteria

### 1.1 Table-selection recall@k

For a labeled question ("this question needs `FactInternetSales` and
`DimProduct`"), does `retrieve_business_context`'s `table`-type results
(top-k = `Settings.retrieval_top_k_tables`) include every required table
within the top k?

```
recall@k = |retrieved_tables[:k] ∩ required_tables| / |required_tables|
```

Distinct from `embeddings/retriever.py`'s own existing table-selection
recall (which governs `schema_context_text`, the authoritative context) —
this measures whether the *business-context* layer's own table chunks
independently surface the right tables, useful for judging whether
`table`/`column`/`relationship` chunking is pulling its weight versus just
duplicating what schema retrieval already provides.

### 1.2 Column-selection recall@k

Same formula, applied to `column`-type retrieved chunks against a labeled
set of required `table.column` pairs.

### 1.3 Join-path accuracy

For a labeled question requiring a specific join (e.g. `DimProduct ->
DimProductSubcategory -> DimProductCategory`), does a retrieved
`relationship` chunk's `extra["join_condition"]` match the expected join
condition string (normalized: whitespace-insensitive, case-insensitive on
keywords)? Binary pass/fail per required hop, aggregated as
`hops_correctly_surfaced / hops_required`.

### 1.4 Business-term retrieval accuracy

For a labeled question containing a known glossary term (e.g. "reseller"),
does the correct `glossary` chunk (`source_id == "glossary:reseller"`)
appear in the retrieved results at all (not rank-sensitive — a business
term either surfaced or it didn't)?

### 1.5 Metric-definition accuracy

Same as 1.4, for `metric` chunks — plus a secondary check: does the
retrieved metric's `extra["formula"]` field match the expected formula
string exactly (this is a curated-file field, not LLM output, so an exact
match is the right bar, not a fuzzy one).

### 1.6 SQL-example usefulness

Proxy metric (no ground truth for "usefulness" itself): of the
`sql_example` chunks retrieved for a question, what fraction share at
least one table with the question's own required-table label set? A
retrieved example referencing entirely unrelated tables is very unlikely
to be a useful pattern.

### 1.7 SQL execution success rate (retrieval-enabled)

Reuses `eval/`'s existing execution-accuracy grading
(`eval/evaluators.py`) — the fraction of benchmark cases where the
generated SQL executes successfully and its result set matches gold,
**run once with `ENABLE_BUSINESS_CONTEXT_RETRIEVAL=true` and once with it
`=false`**, same question set, same model, same seed/temperature (0.0,
this project's existing generation setting). This is the actual
outcome-level signal that matters — the other criteria above are
diagnostic for *why* a retrieval-enabled run helped or didn't.

### 1.8 Retrieval-disabled vs. retrieval-enabled comparison

The single most important comparison this document defines: run
`eval/runner.py`'s existing case set (or the small fixture in §2) twice,
toggling `ENABLE_BUSINESS_CONTEXT_RETRIEVAL`, and compare:

- Execution accuracy (§1.7)
- Retry count distribution (`AttemptRecord`/`attempt_history` — does
  business context reduce the number of self-correction cycles needed?)
- `schema_anomaly_tables` occurrences (`agent.sql_validator
  .find_unexpected_table_references` — a detection-only signal already on
  state; a *decrease* with retrieval enabled would suggest the model is
  inventing fewer tables it only "knows about" from general training,
  since it now has real retrieved context grounding those same tables)

### 1.9 Retrieval latency

`RetrievalResult.metadata["duration_ms"]` — already logged per-question by
`retrieve_business_context_node` (`[retrieve_business_context] ...
duration_ms=%.1f`). Aggregate p50/p95 across a benchmark run.

### 1.10 Embedding latency

Time spent inside `EmbeddingProvider.embed_text`/`embed_batch` specifically
(a subset of §1.9's total) — not separately instrumented today; a
follow-up would add a `time.perf_counter()` wrap inside
`retrieval.retriever.retrieve_business_context` around the
`provider.embed_text(question)` call and fold it into `metadata`.

### 1.11 Token usage

Approximated via character count (this project has no tokenizer
dependency anywhere, §5 of the design doc) —
`len(_build_business_context_block(retrieved_context))` /
`Settings.retrieval_max_context_tokens * 4` as a rough "budget
utilization" ratio. Compare average prompt character count with retrieval
on vs. off to quantify the actual prompt-size cost of this feature.

### 1.12 Fallback success rate

The fraction of questions in a run where `retrieval_warnings` is non-empty
(retrieval degraded for any reason) **and** `status == "succeeded"`
anyway — i.e., did a real question still get answered when the
business-context layer failed? `tests/test_vector_fallback.py` proves this
holds structurally (retrieval never blocks a run); this metric is the
*production* confirmation of the same property under real, intermittent
failure conditions (a flaky embedding backend, a transient Chroma error).

### 1.13 Index freshness

`content_hash` staleness, operationally: time since the last successful
`python -m scripts.ingest_schema` run for a given `database_id`, compared
against the live schema's own `get_schema_fingerprint()` (already computed
for the existing schema-DDL index, §1.4 of the design doc) — a mismatch
means the business-context collection describes a schema shape that's
since changed. Not automated in this pass (no scheduled-ingestion
infrastructure exists in this project — `scripts/ingest_schema.py` is a
manual/cron-external entry point, same operational model as
`scripts/build_embeddings.py` already has); a monitoring script comparing
the two fingerprints is a reasonable follow-up.

---

## 2. Evaluation fixtures

**Disclosure**: table/column names below are real — verified via live
schema introspection of this dev environment's own "adventureworks"
sample database (AdventureWorksDW-shaped, SQL Server) while building this
feature. The "required tables/columns/relationships" labels are this
author's own judgment for demonstration purposes, not independently
reviewed by a second person or a business stakeholder — treat this fixture
as a small, clearly-labeled starting point for the criteria in §1, not a
validated ground-truth benchmark. Extending `eval/benchmark/*.yaml`
(this project's real, existing benchmark format) with a `retrieval:`
labels block per case is the natural next step, not attempted here since
it touches the existing eval harness's own schema
(`eval/schema.py`) — a deliberate scope boundary, not an oversight.

```yaml
# docs/retrieval-eval-fixture.yaml (illustrative shape -- not wired into
# eval/ automatically; a follow-up would add a `retrieval:` block to
# eval/schema.py's case model and load this the same way eval/benchmark/*.yaml
# already loads gold SQL).
cases:
  - question: "What is total widget... " # (placeholder shape only)
  - question: "What were total internet sales by product category last year?"
    required_tables: ["FactInternetSales", "DimProduct", "DimProductSubcategory", "DimProductCategory", "DimDate"]
    required_columns: ["FactInternetSales.SalesAmount", "DimProductCategory.EnglishProductCategoryName"]
    required_relationships:
      - "DimProduct.ProductSubcategoryKey -> DimProductSubcategory.ProductSubcategoryKey"
      - "DimProductSubcategory.ProductCategoryKey -> DimProductCategory.ProductCategoryKey"
    required_glossary_terms: ["internet sales", "product category"]
    required_metrics: []

  - question: "Top 3 resellers by sales amount in each sales territory region"
    required_tables: ["FactResellerSales", "DimReseller", "DimSalesTerritory"]
    required_columns: ["FactResellerSales.SalesAmount", "DimSalesTerritory.SalesTerritoryRegion"]
    required_relationships:
      - "FactResellerSales.ResellerKey -> DimReseller.ResellerKey"
      - "FactResellerSales.SalesTerritoryKey -> DimSalesTerritory.SalesTerritoryKey"
    required_glossary_terms: ["reseller", "sales territory"]
    required_metrics: []

  - question: "What is the gross margin by product category for internet sales in 2013?"
    required_tables: ["FactInternetSales", "DimProduct", "DimProductSubcategory", "DimProductCategory", "DimDate"]
    required_columns: ["FactInternetSales.SalesAmount", "FactInternetSales.TotalProductCost"]
    required_relationships:
      - "FactInternetSales.ProductKey -> DimProduct.ProductKey"
    required_glossary_terms: ["internet sales"]
    required_metrics: ["gross margin", "gross sales amount", "total product cost"]

  - question: "Which employees are sales territory managers, with their region?"
    required_tables: ["DimEmployee", "DimSalesTerritory"]
    required_columns: ["DimEmployee.SalesTerritoryKey", "DimSalesTerritory.SalesTerritoryRegion"]
    required_relationships:
      - "DimEmployee.SalesTerritoryKey -> DimSalesTerritory.SalesTerritoryKey"
    required_glossary_terms: ["sales territory"]
    required_metrics: []
```

This mirrors the four curated `sql_examples.yaml` entries (§8 of the
design doc) deliberately — the same four real questions this feature's own
sample SQL examples were written to answer, so a recall@k measurement
against these fixtures is also implicitly checking "does the curated
example for this exact question actually get retrieved for it" (§1.6).

**Manually verified during this feature's own build** (not a formal
eval run, but a real, live check against this dev environment — see
`docs/vector-retrieval-design.md`'s §4/§7 for the transcript): asking
*"What is the gross margin by product category for internet sales in
2013?"* through the real `agent.graph.run_agent()` retrieved the correct
`gross margin`/`total product cost` metric chunks, the matching curated
SQL example, and the `internet sales` glossary term — and the generated
SQL correctly joined the full `DimProduct -> DimProductSubcategory ->
DimProductCategory` hierarchy (the exact pattern from the retrieved
example), including a table (`DimDate`) that fell outside the *live
schema* retrieval's own top-k window but was still referenced correctly,
consistent with the business-context layer supplying real, additional
context recall.

---

## 3. How to run this evaluation

No automated runner ships with this pass (see §1's disclosure on scope) —
until `eval/` is extended with a `retrieval:` labels block, the fixtures
above are evaluated manually:

```powershell
# 1. Ingest business context for the database under test.
python -m scripts.ingest_schema --database-id adventureworks

# 2. For each fixture question, inspect retrieval + generation output directly:
python -c "
from agent.graph import run_agent
state = run_agent('What is the gross margin by product category for internet sales in 2013?')
print('retrieved sources:', state['retrieval_sources'])
print('retrieval_metadata:', state['retrieval_metadata'])
print('sql:', state['sql'])
print('status:', state['status'])
"

# 3. Compare against the same question with retrieval disabled:
#    set ENABLE_BUSINESS_CONTEXT_RETRIEVAL=false in .env, restart, rerun step 2.
```

## 4. Known limitations of this evaluation

- No automated recall@k computation exists yet — the fixtures in §2 define
  the *labels*; scoring them against `state["retrieval_sources"]` is a
  manual comparison today, not a script.
- Latency/token-usage numbers (§1.9-§1.11) are per-question observability
  data, not yet aggregated into a report the way `eval/reporting.py`
  aggregates execution-accuracy results.
- The fixture set (§2) is intentionally small (4 cases) — enough to
  exercise every chunk type at least once, not a statistically meaningful
  sample. `eval/benchmark/*.yaml`'s existing ~dozens of cases
  **[assumption: exact count not re-verified in this pass]** would be the
  right scale for a real retrieval-quality study.
