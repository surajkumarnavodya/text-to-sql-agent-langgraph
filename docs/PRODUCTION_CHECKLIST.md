# Production Checklist

A concrete go/no-go checklist derived directly from this project's own
2026-09-01 production-readiness audit (`docs/PRODUCTION_READINESS_REPORT.md`)
— not a generic template. Work through this **per deployment**, not once
globally: a new database connection, a new environment, or a new set of
users each re-open several of these items.

## Blocking — do not deploy without these

- [ ] **`DB_USER` is a genuinely read-only database role.** Verify with
      `python scripts/test_db_connection.py` — it must report *no*
      write-privilege warning. This is the real safety boundary underneath
      the SQL validator (`SECURITY.md`); the app cannot enforce it from
      inside itself. (This exact check failed against this project's own
      reference environment during the audit — don't assume it's fine
      without checking.)
- [ ] **Sensitive columns are classified**, if the connected database
      carries any real PII or otherwise sensitive data.
      `config/sensitive_columns.yaml` ships empty — see
      `docs/GOVERNANCE.md`'s "Data classification policy" for how, and
      `docs/RISK_REGISTER.md`'s R-002.
- [ ] **`.env` is not committed, not baked into a container image layer,
      and not exposed by `docker compose config` output shared anywhere.**
      See `docs/DEPLOYMENT.md`'s "Production secrets."
- [ ] **A reverse proxy with real authentication sits in front of any
      deployment reachable beyond a trusted local network** — neither the
      UI nor the API has real auth (`docs/RISK_REGISTER.md`'s R-001). The
      optional `API_AUTH_TOKEN` is a hook, not a substitute — see
      `docs/DEPLOYMENT.md`'s "Reverse proxy and auth."
- [ ] **You have read `SECURITY.md` in full**, specifically "What is
      explicitly not guaranteed" and "Bottom line" — this project
      explicitly states it has not been through an independent security
      review.

## Strongly recommended before real users touch it

- [ ] **Run the full quality gate and confirm it's clean:** `pytest`,
      `ruff check .`, `black --check .`, `mypy .` — all four, not a
      subset. (All four were red at the start of this project's own
      2026-09-01 audit; verify your working tree, not this document's
      claim about a past state.)
- [ ] **Run the benchmark and read the actual numbers**, don't assume the
      last recorded baseline still reflects your model/database/prompt
      combination: `python scripts/run_benchmark.py --check-regression`.
      See `docs/EVALUATION.md` for how to interpret the output — a 35%
      final-accuracy baseline is the documented starting point, not a
      target to feel good about matching.
- [ ] **Decide, explicitly, what accuracy bar is acceptable for your use
      case**, and don't deploy past it silently. This project does not
      set that bar for you — it only measures and reports it honestly.
- [ ] **Verify Ollama capacity matches expected concurrent load.**
      `p95_latency_seconds` ≈ 80s in the reference benchmark run — a
      single local Ollama instance serializes requests; see
      `docs/DEPLOYMENT.md`'s "Horizontal scaling considerations" for why
      scaling app/API replicas alone doesn't fix this.
- [ ] **Health checks are wired into your deployment platform's own
      monitoring**, not just present in `docker-compose.yml` — `GET
      /health` (`docs/API.md`) actually verifies DB/Ollama/Chroma
      reachability; point real alerting at it, don't just trust the
      container stays "running."
- [ ] **`docs/monitoring_summary.py` (or your own equivalent) has somewhere
      to run periodically** — see `docs/GOVERNANCE.md`'s review cadence.
      It's a script today, not a scheduled job; scheduling it is a
      per-deployment decision this project doesn't make for you.

## Worth doing, not blocking

- [ ] Pin the `Dockerfile`'s base image to a specific digest
      (`docs/DEPLOYMENT.md`'s "Reproducible builds") if you want a
      stricter reproducibility guarantee than a floating tag provides.
- [ ] Verify the pinned dependency versions (chosen for this project's dev
      machine's Python 3.14, per `requirements.txt`'s own comment) against
      whatever Python version your deployment actually runs — CI runs 3.11
      and passes, which is reassuring but not the same as an explicit
      driver-by-driver check (`docs/RISK_REGISTER.md`'s R-004).
- [ ] If you extend this for `DB_TYPE=mssql` in Docker, confirm the
      Microsoft ODBC driver layer (`docs/DEPLOYMENT.md`'s mssql section)
      actually connects — it's an extra manual step this project's base
      image deliberately doesn't include.
- [ ] Review `docs/COMPLIANCE.md` and `docs/RESPONSIBLE_AI.md` against
      your organization's own requirements, if you have any beyond what
      this project self-assesses.

## Explicitly out of scope for "production-ready" as shipped

Named here so no one discovers these the hard way:

- Real multi-user authentication/authorization (`docs/RISK_REGISTER.md`'s R-001).
- A circuit breaker or retry-with-backoff around Ollama/DB connection
  failures — every failure is caught and classified, but a sustained
  outage isn't short-circuited (`docs/RISK_REGISTER.md`'s R-006).
- Distributed rate limiting across multiple replicas
  (`docs/DEPLOYMENT.md`'s "Horizontal scaling considerations").
- Kubernetes support — a single Compose deployment is what's provided; see
  `docs/DEPLOYMENT.md`'s "If you outgrow this."
- An independent, third-party security review
  (`docs/RISK_REGISTER.md`'s R-005).
