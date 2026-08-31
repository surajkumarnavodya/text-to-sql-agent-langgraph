# Sample eval run

A real, captured run of `scripts/run_eval.py` against the public
`AdventureWorksDW2025` sample database, kept as portfolio evidence that the
eval harness runs against a live LLM + live database, not a mocked
demonstration. Scrubbed of nothing sensitive — there's nothing sensitive
to scrub (public sample schema, aggregate numbers only, no connection
details, no row-level PII).

- **Date:** 2026-08-31
- **Model:** `llama3.1:8b` via Ollama (local)
- **Database:** SQL Server, `AdventureWorksDW2025` (public Microsoft sample)
- **Command:** `python scripts/run_eval.py`

```
[PASS] 'Which sales territory had the highest sales for the Bikes category?' -- status=succeeded rows=1

1/1 passed.
```

**Generated SQL (attempt 1, no retries needed):**
```sql
SELECT TOP 1 st.SalesTerritoryRegion
FROM DimSalesTerritory AS st
INNER JOIN FactResellerSales AS rs ON st.SalesTerritoryKey = rs.SalesTerritoryKey
INNER JOIN DimProduct AS p ON rs.ProductKey = p.ProductKey
INNER JOIN DimProductSubcategory AS ps ON p.ProductSubcategoryKey = ps.ProductSubcategoryKey
INNER JOIN DimProductCategory AS pc ON ps.ProductCategoryKey = pc.ProductCategoryKey
WHERE pc.EnglishProductCategoryName = 'Bikes'
ORDER BY rs.SalesAmount DESC
```

Independently verified against raw aggregated data: `Southwest` is in fact
the top reseller-channel territory for the Bikes category in this dataset.

## Why this one question, and what it's actually testing

This isn't a broad accuracy suite yet (see `eval/eval_questions.yaml`'s
header for the growable format) — it's a **regression guard for a specific,
previously-real failure mode**: the schema has a column
(`DimProduct.ProductLine`) whose name suggests it might hold a category
like "Bikes," but which actually holds unrelated short manufacturing codes
(`M`/`R`/`S`/`T`). During development, this question caught the model:

1. Filtering on `ProductLine = 'Bikes'` directly (wrong column — no match, 0 rows).
2. Inventing a nonexistent `FactResellerSales.ProductCategoryKey` column to
   skip the real `DimProduct -> DimProductSubcategory -> DimProductCategory`
   join chain.
3. Comparing `DimProduct.ProductSubcategoryKey` directly to
   `DimProductCategory.ProductCategoryKey` — skipping the
   `DimProductSubcategory` hop entirely. This one is the most interesting
   failure: it returned a **non-empty, readable, plausible-looking result**
   purely because subcategory ID 1 and category ID 1 happen to coincide in
   this data — a row-count-only check would have called this a pass.

That third case is why the eval entry asserts `expect_tables_used:
[DimProductSubcategory]` — checking the executed SQL text for a table that
*must* have been part of a correct join, not just checking that *some*
non-empty answer came back. See `CONTRIBUTING.md` for the field format and
`docs/ARCHITECTURE.md` for the retrieval-pipeline fix (FK-adjacency bridge
expansion) that made the correct join chain reachable in the first place.
