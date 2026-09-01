"""Text-to-SQL evaluation/benchmark framework.

Public entry points: `eval.runner.run_benchmark()` (live, requires Ollama +
a real DB -- see `scripts/run_benchmark.py`) and `eval.dataset_loader.
load_benchmark()` (pure, no live dependency -- used by both the runner and
the framework's own pytest suite in `tests/test_eval_*.py`).
"""
