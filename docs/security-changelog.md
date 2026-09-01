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

<!--
Template for new entries — copy this block:

## YYYY-MM-DD — <short title>

**Change:** <control> changed from <old value/rule> to <new value/rule>.

**Why:** <reason>

**Status:** Permanent | Time-boxed exception (see RISK_REGISTER.md entry
dated YYYY-MM-DD, review by YYYY-MM-DD)
-->
