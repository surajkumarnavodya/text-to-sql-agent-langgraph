"""Smoke-tests each SME-confirmed golden question's candidate SQL against
the live connection before publish -- Prompt 08
(`08_ONBOARDING_ENGINE_CONTRACT.md`)'s "evaluation" stage.

**Deliberately not `eval/`'s own execution-accuracy benchmark**, and not
the full `agent.graph.run_agent` pipeline -- both assume a database
already published into `Settings.databases` and already embedded into
its own Chroma collection, neither of which is true yet at onboarding
time (the whole point of this engine is to validate a database *before*
that publish step). This module instead re-validates and executes each
candidate's already-deterministic `candidate_sql`
(`onboarding.golden_questions.GoldenQuestionCandidate`) through the exact
same two gates every other SQL in this codebase goes through --
`agent.sql_validator.validate_sql`/`enforce_row_limit`, then
`db.execution.execute_readonly_sql` -- never a second, parallel
validation/execution path. A pass here means "this question's SQL is
syntactically valid and actually executes against this database," not
"the returned numbers are correct" (there is no gold answer to compare
against for a database with no prior history) -- the real,
execution-accuracy benchmark `eval/` runs is a materially stronger claim
than this module makes, and this module's own report says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from agent.sql_validator import enforce_row_limit, validate_sql
from db.connection import DbConnectionLike
from db.execution import execute_readonly_sql
from onboarding.golden_questions import GoldenQuestionCandidate
from security.redaction import redact_secrets

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 15
_DEFAULT_MAX_ROWS = 100


@dataclass(frozen=True)
class EvaluationResult:
    """One golden question's smoke-test outcome."""

    question: str
    question_type: str
    passed: bool
    error: str | None
    row_count: int | None


def evaluate_candidates(
    candidates: list[GoldenQuestionCandidate],
    engine: Engine,
    dialect: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    max_rows: int = _DEFAULT_MAX_ROWS,
    connection_for_redaction: DbConnectionLike | None = None,
) -> list[EvaluationResult]:
    """Re-validates and executes each candidate's `candidate_sql`.

    Args:
        candidates: Only ever the SME-**confirmed** subset in practice
            (`onboarding/jobs.py`'s own publish flow never calls this
            with a pending/rejected candidate) -- this function itself
            doesn't know or enforce decision state, that's a job-
            orchestration concern, not an evaluation concern.
        engine: A read-only engine for the live connection -- supplied
            fresh by the caller (never persisted, see
            `identity.models.OnboardingJob`'s own docstring).
        dialect: sqlglot dialect (`db.connection.get_sqlglot_dialect
            (db_type)`) -- `None` uses sqlglot's generic dialect.

    Returns:
        One `EvaluationResult` per candidate, in the same order. A
        validation rejection or an execution error is recorded as
        `passed=False` with the reason in `error` -- never raised, since
        one bad candidate must not abort evaluating the rest.
    """
    results: list[EvaluationResult] = []
    for candidate in candidates:
        validation = validate_sql(candidate.candidate_sql, dialect=dialect)
        if not validation.is_valid:
            results.append(
                EvaluationResult(
                    question=candidate.question,
                    question_type=candidate.question_type,
                    passed=False,
                    error=f"validation failed: {validation.error}",
                    row_count=None,
                )
            )
            continue

        # validation.is_valid guarantees normalized_sql is set -- see
        # ValidationResult's own model_validator.
        assert validation.normalized_sql is not None
        limited_sql = enforce_row_limit(validation.normalized_sql, max_rows, dialect=dialect)
        try:
            _columns, rows = execute_readonly_sql(
                limited_sql, timeout_seconds, max_rows, engine=engine
            )
        except (SQLAlchemyError, TimeoutError) as exc:
            # Redacted before it's kept anywhere, per this codebase's
            # established "centralized exception handling" contract --
            # raw driver text is never trusted to be safe to store/show.
            safe_error = redact_secrets(str(exc), connection_for_redaction)
            logger.debug("[evaluation] execution failed for %r: %s", candidate.question, safe_error)
            results.append(
                EvaluationResult(
                    question=candidate.question,
                    question_type=candidate.question_type,
                    passed=False,
                    error=f"execution failed: {safe_error}",
                    row_count=None,
                )
            )
            continue

        results.append(
            EvaluationResult(
                question=candidate.question,
                question_type=candidate.question_type,
                passed=True,
                error=None,
                row_count=len(rows),
            )
        )
    return results
