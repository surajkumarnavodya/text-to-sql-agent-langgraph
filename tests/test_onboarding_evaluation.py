"""Unit tests for onboarding/evaluation.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- the "evaluation" stage.

Uses a **real** in-memory SQLite database (`StaticPool`, same pattern as
`tests/test_onboarding_profiling.py`) so the two gates this module
actually re-runs (`agent.sql_validator.validate_sql`/`enforce_row_limit`,
then `db.execution.execute_readonly_sql`) are exercised for real, not
mocked. No network, no external service.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from onboarding.evaluation import evaluate_candidates
from onboarding.golden_questions import GoldenQuestionCandidate


def _engine():
    return create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def _candidate(sql, question="q?", question_type="count"):
    return GoldenQuestionCandidate(
        question=question,
        tables_involved=("t",),
        question_type=question_type,
        confidence=0.9,
        candidate_sql=sql,
    )


class TestEvaluateCandidatesSuccess:
    def test_good_query_passes_and_reports_a_row_count(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            conn.execute(text("INSERT INTO t VALUES (1)"))
            conn.execute(text("INSERT INTO t VALUES (2)"))
            conn.commit()

        results = evaluate_candidates([_candidate("SELECT COUNT(*) FROM t")], engine)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].error is None
        assert results[0].row_count == 1

    def test_row_count_reflects_actual_rows_returned(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            for i in range(5):
                conn.execute(text("INSERT INTO t VALUES (:i)"), {"i": i})
            conn.commit()

        results = evaluate_candidates([_candidate("SELECT id FROM t")], engine, max_rows=100)

        assert results[0].passed is True
        assert results[0].row_count == 5


class TestEvaluateCandidatesRejection:
    def test_a_malicious_write_candidate_is_rejected_by_validation_not_executed(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            conn.commit()

        results = evaluate_candidates([_candidate("DROP TABLE t")], engine)

        assert results[0].passed is False
        assert results[0].error is not None
        assert results[0].error.startswith("validation failed:")
        assert results[0].row_count is None

        # And the table really is still there -- the rejection happened
        # before anything reached the database.
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM t")).scalar()
        assert count == 0

    def test_a_query_against_a_nonexistent_table_fails_execution_cleanly(self):
        engine = _engine()  # no tables created at all

        results = evaluate_candidates([_candidate("SELECT * FROM does_not_exist")], engine)

        assert results[0].passed is False
        assert results[0].error is not None
        assert results[0].error.startswith("execution failed:")
        assert results[0].row_count is None


class TestEvaluateCandidatesIndependence:
    def test_one_bad_candidate_does_not_abort_evaluating_the_rest(self):
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER)"))
            conn.execute(text("INSERT INTO t VALUES (1)"))
            conn.commit()

        candidates = [
            _candidate("SELECT COUNT(*) FROM t", question="good"),
            _candidate("SELECT * FROM does_not_exist", question="bad"),
            _candidate("SELECT COUNT(*) FROM t", question="good2"),
        ]
        results = evaluate_candidates(candidates, engine)

        assert [r.question for r in results] == ["good", "bad", "good2"]
        assert [r.passed for r in results] == [True, False, True]
