"""500-case prompt-injection/security evaluation harness.

A sibling package to `eval/` (the Text-to-SQL accuracy benchmark), same
conventions, different question: not "is the SQL correct?" but "did the
agent leak a secret, execute/attempt a write, call a source it wasn't
authorized for, reveal its own system prompt, or cross a session/role
boundary when handed an adversarial input?"

Dataset: `eval/security_benchmark/cases/prompt_injection_benchmark_500.csv`
(500 rows, 20 categories x 25 cases, sourced from an externally supplied
benchmark -- see `dataset.py`'s module docstring for the dataset's own
known characteristics, including a real duplication finding worth knowing
before interpreting a run's pass rate).

Like `eval/` itself, everything here except `dataset.py`/`detectors.py`
requires a live Ollama server and a live, configured database -- never
imported by `tests/` (the fully-mocked pytest suite). `dataset.py` and
`detectors.py` are pure/dependency-free and *are* covered by ordinary
pytest tests (`tests/test_security_benchmark_dataset.py`,
`tests/test_security_benchmark_detectors.py`).

`multiturn.py` (the true multi-turn persistence test -- see its own module
docstring) is a partial exception to that split: its live-agent-calling
functions (`run_control_turn2`, `run_multiturn_persistence_case`,
`run_multiturn_persistence_benchmark`) require live infra exactly like
`runner.py`, but its grading/comparison logic (`TurnOutcome`,
`is_critical_finding`, `unique_multiturn_payloads`) is pure and *is*
covered by `tests/test_multiturn_persistence.py` -- the same "pure logic
stays independently testable" split `detectors.py`/`runner.py` already
model, just within one module instead of two, since the live-run surface
here is small enough not to warrant a separate file.

`runner.py` itself gained one more testable-without-live-infra seam
2026-09-25: `tests/test_security_benchmark_runner.py` mocks
`run_orchestrated` to raise a real `agent.exceptions.AgentError` and
confirms `run_security_case`/`run_security_benchmark` record it as an
`"error"`-status result and keep going, rather than letting an unhandled
exception abort the whole batch -- regression coverage for a real
incident (two concurrent live runs exhausted local Ollama, and the
resulting timeout used to crash an entire in-progress multi-hour run).
This is still fully mocked, not a live-infra test -- same convention as
`multiturn.py`'s pure functions above.
"""
