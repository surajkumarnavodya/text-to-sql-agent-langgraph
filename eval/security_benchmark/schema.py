"""Result data model -- what actually running one `SecurityCase` against the
live agent produces, and how a full run's results are aggregated.

Mirrors `eval/schema.py`'s `CaseRunResult`/`BenchmarkReport` split: one flat
record per case (`SecurityCaseResult`), reduced into one report
(`SecurityBenchmarkReport`). Kept as a separate dataclass rather than
extending `eval.schema.CaseRunResult` because the two benchmarks grade
fundamentally different things (SQL correctness vs. safety guardrails) --
forcing a shared shape would mean either benchmark carrying fields
meaningless to it (see `eval/security_benchmark/dataset.py`'s docstring for
the same reasoning applied to the case schema).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SecurityCaseResult:
    """Raw signal + verdicts for one case, one run.

    The `*_leak`/`*_violation` fields never hold the leaked secret's actual
    value (see `detectors.detect_secret_leak`) -- only labels of *what*
    leaked, so a saved result file is never itself a second place a real
    secret ends up written in the clear. `response_text_excerpt` is
    similarly a bounded excerpt (see `runner.py`), not the full response,
    for the same reason.
    """

    case_id: str
    category: str
    attack_channel: str
    expected_behavior: str
    caller_roles: tuple[str, ...]

    # Raw agent outcome
    final_status: str = "unknown"
    retry_count: int = 0
    wall_time_seconds: float = 0.0
    response_text_excerpt: str = ""
    generated_sql: str | None = None

    # Guardrail signal (see detectors.py)
    write_attempted: bool = False
    write_executed: bool = False
    unauthorized_sources: tuple[str, ...] = ()
    media_generated_without_approval: bool = False
    system_prompt_leak_fingerprints: tuple[str, ...] = ()
    secret_leak_labels: tuple[str, ...] = ()
    refusal_detected: bool = False

    # Guardrail verdicts, keyed by the benchmark's own boolean column names
    # -- True means the guardrail was HONORED (no violation), mirroring the
    # CSV's own "must_not_*" polarity so a report reads directly against
    # the source columns. None means "not applicable to this case" (the
    # case's own must_not_* column was False).
    guardrail_results: dict[str, bool | None] = field(default_factory=dict)

    # expected_behavior match
    behavior_matched: bool = False
    behavior_reason: str = ""

    overall_pass: bool = False
    failure_reasons: tuple[str, ...] = ()
    error_detail: str | None = None


@dataclass
class SecurityBenchmarkReport:
    """Aggregated output of a full security-benchmark run."""

    run_id: str
    timestamp: str
    model: str
    caller_roles: tuple[str, ...]
    total_cases: int
    metrics: dict[str, float | None]
    per_category: dict[str, dict[str, float]]
    per_attack_channel: dict[str, dict[str, float]]
    consistency: dict[str, float | None]
    results: list[SecurityCaseResult]

    @property
    def failures(self) -> list[SecurityCaseResult]:
        return [r for r in self.results if not r.overall_pass]

    @property
    def critical_findings(self) -> list[SecurityCaseResult]:
        """Cases where a hard security gate actually failed (not just an
        expected-behavior mismatch) -- write executed, a privileged source
        was reached without authorization, media was generated without
        approval, a secret leaked, or the system prompt leaked. Surfaced
        separately from `failures` since these are categorically more
        severe than "the agent refused in a way the heuristic didn't
        recognize as a refusal.\""""
        return [
            r
            for r in self.results
            if r.write_executed
            or r.unauthorized_sources
            or r.media_generated_without_approval
            or r.secret_leak_labels
            or r.system_prompt_leak_fingerprints
        ]
