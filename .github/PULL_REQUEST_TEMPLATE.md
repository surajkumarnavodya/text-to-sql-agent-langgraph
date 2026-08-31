## What does this change and why?

## How was this tested?

- [ ] `pytest` passes
- [ ] `ruff check . && black --check . && mypy .` are clean
- [ ] If this touches `agent/sql_validator.py` or the SQL-execution path,
      I've called that out explicitly below (see `CONTRIBUTING.md`)
- [ ] If this adds/changes agent behavior, I ran `scripts/run_eval.py`
      against a real database and it still passes

## Anything reviewers should look at closely?
