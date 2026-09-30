"""The DATABASE_FACT / AI_INFERENCE / CONFIRMED_BUSINESS_TRUTH vocabulary
`00_MASTER_IMPLEMENTATION_CONTRACT.md`'s rules 9-10 require and
`01_BASELINE_ARCHITECTURE_AND_REUSE_INVENTORY.md` §4 found missing
end-to-end from this codebase, despite the closest existing mechanism --
`agent.insight.is_insight_grounded` -- already enforcing an informal
version of it (an `AI_INFERENCE` narrative claim is rejected unless it's
traceable to a `DATABASE_FACT` number computed straight from the real
query result).

This module defines the shared, typed vocabulary only. It is deliberately
**not** wired into `agent/insight.py`, `analytics/`, or any live node in
this increment -- see `02_TARGET_ARCHITECTURE.md`'s "stubbed today, wired
later" table for which future prompt retrofits which producer. Introducing
the vocabulary now, ahead of its consumers, is what lets every later
producer (insight narration, analytics findings, a future recommendation
engine) share one definition of "how sure are we of this" instead of each
inventing its own ad hoc confirmed/unconfirmed flag.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator


class DataTruthLevel(str, Enum):
    """How a piece of information this platform surfaces came to be known.

    A closed, three-value set -- deliberately not a free-form string or a
    boolean "is this trusted" flag, because the three levels are not points
    on one confidence scale: they are different *kinds* of claim with
    different obligations attached.

    Attributes:
        DATABASE_FACT: A value read or computed directly and deterministically
            from a real, executed query result -- no LLM involved in
            producing the value itself. E.g. a row count, a `SUM`, a
            `agent.insight.ColumnStat.stddev`. The ground truth every other
            level must ultimately be traceable back to.
        AI_INFERENCE: A claim an LLM generated -- a narrative sentence, a
            suggested next question, a recommended action. Never
            trustworthy on its own; per rule 10, an `AI_INFERENCE` must
            never be silently promoted to `CONFIRMED_BUSINESS_TRUTH`. The
            existing precedent for constraining one of these is
            `agent.insight.is_insight_grounded`, which rejects an
            `AI_INFERENCE` insight sentence unless every number in it
            matches a `DATABASE_FACT` the same result actually contains.
        CONFIRMED_BUSINESS_TRUTH: A claim a human has explicitly reviewed
            and approved as correct -- not merely "the LLM said it
            confidently" or "it ran without error." The existing precedents
            for this level are `embeddings.golden_examples` (a human
            thumbs-up on a confirmed, executed SQL query) and
            `config.table_descriptions`/`config.sensitive_columns`
            (hand-reviewed business semantics). Promotion to this level is
            always an explicit human action in this codebase today -- never
            automatic, and this module does not change that.
    """

    DATABASE_FACT = "database_fact"
    AI_INFERENCE = "ai_inference"
    CONFIRMED_BUSINESS_TRUTH = "confirmed_business_truth"


class ProvenancedClaim(BaseModel):
    """Pairs a value with its `DataTruthLevel` and, for an `AI_INFERENCE`,
    the `DATABASE_FACT` claim(s) it must be traceable to.

    This is the typed shape `agent.insight.is_insight_grounded`'s informal
    check already implements in spirit (an insight sentence's numbers must
    match `ResultSummary.allowed_values()`/`allowed_percents()`) -- a
    future retrofit of that node can construct a `ProvenancedClaim` per
    number instead of returning a bare grounded/ungrounded boolean, without
    changing what "grounded" actually means.

    Attributes:
        value: The claim itself, as rendered text (e.g. a full insight
            sentence, or one recommended action's description). Deliberately
            `str`, not `Any` -- this module governs claims that get shown
            to a person, not arbitrary internal data structures.
        level: How this claim came to be known -- see `DataTruthLevel`.
        grounded_in: For `level == AI_INFERENCE` only, the `DATABASE_FACT`
            claim(s) this inference must be traceable back to (e.g. the
            specific numbers from a `ResultSummary` an insight sentence
            cites). Empty for `DATABASE_FACT`/`CONFIRMED_BUSINESS_TRUTH`
            claims, which are their own grounding.
        source: A short, human-readable pointer to where this claim came
            from (e.g. `"agent.insight.generate_insight_from_llm"`,
            `"embeddings.golden_examples"`) -- for audit/debugging, never
            parsed programmatically.
    """

    model_config = ConfigDict(frozen=True)

    value: str
    level: DataTruthLevel
    grounded_in: tuple[str, ...] = ()
    source: str | None = None

    @model_validator(mode="after")
    def _ai_inference_should_disclose_grounding(self) -> ProvenancedClaim:
        """Not a hard validation failure (a `grounded_in`-less AI_INFERENCE
        claim is a real, valid state -- e.g. a recommendation with no
        specific numeric grounding, such as "consider reviewing this
        category") -- deliberately permissive. Rule 10's actual enforcement
        (never silently *promoting* an ungrounded inference to confirmed
        truth) is a behavioral guarantee belonging to whichever node
        constructs and displays a `ProvenancedClaim`, not something a
        Pydantic validator can verify from the shape alone. This validator
        exists only to fail closed on the one combination that is a real
        modeling error: claiming `CONFIRMED_BUSINESS_TRUTH` while also
        citing `grounded_in` sources, which conflates "a human confirmed
        this" with "this was inferred from and traceable to specific
        facts" -- two different claims this vocabulary keeps deliberately
        distinct.
        """
        if self.level == DataTruthLevel.CONFIRMED_BUSINESS_TRUTH and self.grounded_in:
            raise ValueError(
                "A CONFIRMED_BUSINESS_TRUTH claim is confirmed by human review, not "
                "'grounded in' other facts -- set grounded_in only on an AI_INFERENCE claim."
            )
        return self
