"""Captures Ollama's own per-call timing/token counts from `agent.llm_client`'s
log output -- currently the only channel that exposes this data at all
(`AgentState` doesn't carry it; see `agent.llm_client._log_ollama_timing`'s
own docstring for why wall-clock alone can't substitute for it: it can't
distinguish model-load time from prompt-processing time from generation
time, and only Ollama's own response fields can).

Extracted from `eval/runner.py`'s original private `_TokenCaptureHandler`
(built first for the Text-to-SQL benchmark's `prompt_tokens`/
`completion_tokens` fields) into this shared module so the same mechanism
also backs the API/observability layer's per-request `LlmCallTiming` list
-- one implementation, not two independently-maintained copies of the same
regex-on-a-log-line trick.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_TIMING_LINE_RE = re.compile(
    r"\[timing\] stage=llm_call total_ms=(?P<total>[\d.]+) load_ms=(?P<load>[\d.]+) "
    r"prompt_eval_ms=(?P<prompt_eval>[\d.]+) generation_ms=(?P<generation>[\d.]+) "
    r"prompt_tokens=(?P<prompt_tokens>\d+|None) output_tokens=(?P<output_tokens>\d+|None)"
)


@dataclass(frozen=True)
class LlmCallTiming:
    """One real Ollama call's own reported timing/token breakdown -- see
    `agent.llm_client._log_ollama_timing` for what each field measures."""

    total_ms: float
    load_ms: float
    prompt_eval_ms: float
    generation_ms: float
    prompt_tokens: int | None
    completion_tokens: int | None


class LlmTimingCapture(logging.Handler):
    """Collects one `LlmCallTiming` per real LLM call logged during its
    lifetime. Attach via the `capture_llm_timings()` context manager below,
    not directly -- that function owns the logger-level/handler bookkeeping
    so it's never duplicated at each call site.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.calls: list[LlmCallTiming] = []

    def emit(self, record: logging.LogRecord) -> None:
        match = _TIMING_LINE_RE.search(record.getMessage())
        if not match:
            return
        groups = match.groupdict()
        prompt_tokens = None if groups["prompt_tokens"] == "None" else int(groups["prompt_tokens"])
        completion_tokens = (
            None if groups["output_tokens"] == "None" else int(groups["output_tokens"])
        )
        self.calls.append(
            LlmCallTiming(
                total_ms=float(groups["total"]),
                load_ms=float(groups["load"]),
                prompt_eval_ms=float(groups["prompt_eval"]),
                generation_ms=float(groups["generation"]),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

    @property
    def prompt_tokens_total(self) -> int:
        return sum(c.prompt_tokens or 0 for c in self.calls)

    @property
    def completion_tokens_total(self) -> int:
        return sum(c.completion_tokens or 0 for c in self.calls)

    @property
    def saw_any_tokens(self) -> bool:
        """False means no timing line was captured at all for this scope
        (e.g. the question was rejected before any LLM call, or an older
        Ollama server doesn't report token counts) -- distinct from "saw a
        call with 0 tokens," which shouldn't happen in practice but is
        handled the same conservative way either way."""
        return any(c.prompt_tokens is not None for c in self.calls)


@contextmanager
def capture_llm_timings() -> Iterator[LlmTimingCapture]:
    """Context manager: yields an `LlmTimingCapture` populated with one
    `LlmCallTiming` per real LLM call made by `agent.llm_client` during the
    `with` block -- including every retry attempt's call, and the insight
    call if one happens, since this is call-scoped (attached to the logger
    for the whole block), not limited to the first call.

    Forces the `agent.llm_client` logger to INFO for the duration
    (`_log_ollama_timing` logs at INFO -- if the process-wide log level is
    configured coarser, e.g. WARNING, the logger's *effective* level would
    suppress the record before it's even constructed, regardless of a
    handler being attached) and restores its prior level afterward, rather
    than requiring the whole process to run at INFO just for this.
    """
    capture = LlmTimingCapture()
    llm_logger = logging.getLogger("agent.llm_client")
    previous_level = llm_logger.level
    llm_logger.setLevel(logging.INFO)
    llm_logger.addHandler(capture)
    try:
        yield capture
    finally:
        llm_logger.removeHandler(capture)
        llm_logger.setLevel(previous_level)
