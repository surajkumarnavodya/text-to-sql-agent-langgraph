# Security Changelog

A dated log of every deliberate change to a security-relevant control,
separate from ordinary feature/commit history, per
[`GOVERNANCE.md`](GOVERNANCE.md)'s change-control section. This is where
"when and why did X threshold change" gets answered without reconstructing
it from `git log` across unrelated commits.

**In scope:** any change to the SQL validator's allowlist
(`agent/sql_validator.py`), the column-sensitivity classification config
(once it exists — see `GOVERNANCE.md`'s data classification policy),
rate limits (`QUESTION_RATE_LIMIT_PER_MINUTE`,
`LLM_CALL_RATE_LIMIT_PER_MINUTE`), or cost-estimation thresholds
(`COST_MODERATE_ROW_THRESHOLD`, `COST_HIGH_ROW_THRESHOLD`,
`COST_ESTIMATION_ENABLED`).

**Entry format:** date, what changed (old → new), why, and whether it's a
permanent change or a time-boxed exception (cross-reference the matching
entry in [`RISK_REGISTER.md`](RISK_REGISTER.md)'s "Accepted exceptions"
section if the latter).

---

## 2026-09-01 — Governance process established

**Change:** No control values changed. This changelog, `GOVERNANCE.md`,
`COMPLIANCE.md`, `RESPONSIBLE_AI.md`, and `RISK_REGISTER.md` were added,
formalizing change control for the items listed above going forward.

**Why:** Prior to this date, changes to the validator allowlist, rate
limits, and cost thresholds were made via ordinary commits with no
dedicated audit trail separate from general project history — reasoned
about carefully at the time (see `SECURITY.md`'s calibration notes on the
cost thresholds, for instance) but not logged as a distinct, reviewable
category of change.

**Status:** Permanent (process change, not an exception).

---

## 2026-09-01 — Enterprise security audit: validator hardening + defense-in-depth additions

**Change:** A comprehensive security audit (22 attack-surface categories)
was performed against the running code, not just the design -- concrete
attack payloads were run through `agent.sql_validator.validate_sql` in this
repo's own environment rather than assumed safe from the design alone. That
surfaced two confirmed gaps, both closed in `agent/sql_validator.py`'s
allowlist (the item this changelog exists specifically to track):

1. **Critical -- closed.** The validator only checked the parsed
   statement's *root* AST node type. A data-modifying CTE
   (`WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`) has an
   ordinary `Select` root even though it deletes real rows, so it returned
   `is_valid=True`. The validator now walks the *entire* parsed tree and
   rejects an `Insert`/`Update`/`Delete`/`Merge`/`Drop`/`Create`/`Alter`/
   `TruncateTable`/`Command` node anywhere in it, not just at the root. New
   `violation_type`: `"embedded_write"` (in `SAFETY_VIOLATION_TYPES` --
   fails closed, never retried, same as `multiple_statements`).
2. **High -- closed.** Known-dangerous functions/table-valued-functions
   callable from inside an ordinary SELECT (`pg_sleep`, `pg_read_file`,
   MySQL `SLEEP`/`BENCHMARK`/`LOAD_FILE`, Oracle `UTL_HTTP.REQUEST`, MSSQL
   `OPENQUERY`/`OPENROWSET`/`OPENDATASOURCE`/`xp_cmdshell`, ...) also passed
   validation unblocked -- capable of DoS, local file disclosure, SSRF, or
   a linked-server pivot. New `violation_type`: `"dangerous_function"`
   (also in `SAFETY_VIOLATION_TYPES`), checked via an AST call-node-name
   match plus a raw-text fallback for dialect-qualified calls. Documented
   explicitly as a denylist (not exhaustive), consistent with this
   project's existing honesty about `agent/input_guard.py`'s own regex
   layer.

Additional defense-in-depth controls added in the same pass (none change
the validator allowlist itself, listed here for completeness since they're
part of the same audit): identifier quoting in `db/value_sampling.py`
(closes a second-order SQL-injection-via-malicious-identifier path),
`config/sensitive_columns.yaml`'s enforced data-classification tiers (was
previously documented in `GOVERNANCE.md` as "not yet implemented"), a
best-effort database write-privilege check (`db.connection.
check_write_privileges`), secret redaction for driver error text
(`security/redaction.py`) and a `SecretStr` wrapper for `Settings.
db_password`/`db_connection_string` (`security/secrets.py`), structured
security-event logging (`security/audit_log.py`), a RAG-poisoning
detection scan on retrieved schema context, session-scoped result caching
in `ui/app.py` (closes a process-wide `st.cache_data` cross-session leak
risk), markdown-escaping for database-sourced table/column names rendered
in the UI, and strict validation of security-relevant `.env` settings
(positive rate limits/caps/timeouts, ordered cost thresholds).

**Why:** Requested as a comprehensive enterprise-readiness security audit.
Reviewing the existing implementation first (per the audit's own
instruction) found it already unusually mature for this project's stated
scope, but the audit's discipline of testing actual payloads against the
actual code -- rather than reasoning from the design alone -- is what
surfaced the two validator gaps above; neither was previously known or
covered by `tests/test_sql_validator.py`/`tests/test_adversarial_input.py`.

**Status:** Permanent. New regression coverage:
`tests/test_sql_validator_hardening.py` (the two validator fixes),
`tests/test_value_sampling_injection.py`, `tests/test_sensitive_columns.py`,
`tests/test_write_privilege_check.py`, `tests/test_redaction.py`,
`tests/test_secrets.py`, `tests/test_audit_log.py`,
`tests/test_settings_validation.py`, `tests/test_nodes_security_wiring.py`.

---

<!--
Template for new entries — copy this block:

## YYYY-MM-DD — <short title>

**Change:** <control> changed from <old value/rule> to <new value/rule>.

**Why:** <reason>

**Status:** Permanent | Time-boxed exception (see RISK_REGISTER.md entry
dated YYYY-MM-DD, review by YYYY-MM-DD)
-->
