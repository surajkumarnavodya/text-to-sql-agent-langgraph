-- Tiny business schema for the Phase 0 load-test harness only (see
-- docs/SCALE_OUT_PROMPT.md's Phase 0 section and
-- eval/load/mock_ollama_server.py's docstring). Deliberately small (one
-- table, a few hundred rows) -- Phase 0 measures API-tier overhead, not
-- query performance on a realistic warehouse, and the mock LLM's one
-- fixed SQL response (`SELECT id, customer_name, amount FROM orders
-- ORDER BY id LIMIT 50`) must match this exactly.
--
-- Applied once, at load-test-stack startup, against the `loadtest_business`
-- database (see docker-compose.loadtest.yml) -- idempotent (DROP TABLE IF
-- EXISTS first) so re-running `make load-test` against an already-seeded
-- volume is a safe no-op, not a duplicate-row error.

DROP TABLE IF EXISTS orders;

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- generate_series -- a plain SQL loop, no client-side seeding step needed.
INSERT INTO orders (id, customer_name, amount, created_at)
SELECT
    n,
    'Customer ' || (n % 37),
    round((random() * 500 + 10)::numeric, 2),
    now() - (random() * interval '365 days')
FROM generate_series(1, 300) AS n;
