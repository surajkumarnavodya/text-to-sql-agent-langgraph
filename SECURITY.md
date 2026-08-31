# Security

This is a **demo/portfolio project**, not a production-hardened product.
Please read this before pointing it at anything real.

## What's actually enforced

- Generated SQL is restricted to a single read-only `SELECT` (or
  `UNION`/`EXCEPT`/`INTERSECT`) statement via an AST-based allowlist
  (`agent/sql_validator.py`, parsed with `sqlglot` — not a regex
  blocklist). This check runs on every attempt, including retries and SQL
  you hand-edit in the UI before clicking **Confirm and Run**.
- Query execution goes through a read-only-by-convention SQLAlchemy
  engine, a row cap enforced two independent ways, and a query timeout —
  see `CLAUDE.md`'s "SQL is untrusted output, always" section for the
  full detail.
- The app never logs a connection string, password, or full result row.

## What is explicitly **not** guaranteed

- **The validator does not physically prevent writes.** There is no
  generic, cross-database way to strip write privileges purely at the
  SQLAlchemy layer. The real guarantee requires *also* pointing `.env` at
  a genuinely read-only database role — the app-level validator is one
  layer, not the only layer, and a bug in it should not be your only line
  of defense.
- **This has not been through an independent security review** (pen
  testing, threat modeling by someone other than the author, adversarial
  prompt-injection testing against the SQL generation path). It has been
  reasoned about carefully, but "the author thought about it" is not the
  same as "someone tried to break it."
- **Not designed for multi-tenant or production deployment.** No
  authentication, no per-user authorization, no rate limiting, no audit
  log beyond application logging.

## Bottom line

**Do not point this at a production database, or a database containing
real sensitive/personal data, without an independent security review and
your own risk assessment first.** For local exploration, portfolio
demos, or a sandboxed/synthetic dataset, the read-only validator plus a
genuinely-read-only DB account is a reasonable posture. For anything more
sensitive, treat this as a starting point, not a finished answer.

## Reporting a vulnerability

This is a personal project without a dedicated security contact — please
open a GitHub issue (or a private security advisory, if enabled on this
repo) describing the issue. For something that would let generated SQL
bypass the read-only allowlist, please include the exact question/SQL
that triggered it.
