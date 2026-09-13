"""A small, purpose-built media-search benchmark -- deliberately a
**parallel** harness to `eval/`'s SQL benchmark, not an extension of it.

`eval/schema.py`'s `BenchmarkCase` is SQL-specific by design (execution-
accuracy grading: run `expected_sql` against the live database, compare
result sets). Media-search grading is structurally different -- "was the
correct asset (and, for video, roughly the right moment) retrieved," not
a result-set comparison -- so this package has its own dataset schema and
grading logic rather than reusing `BenchmarkCase`.

Like `scripts/run_benchmark.py`, this is manual/real-library-required, not
part of the pytest suite -- see `scripts/run_media_eval.py`. Unlike the SQL
benchmark, this repo has no checked-in sample dataset to grade against
(indexable media isn't the kind of thing that belongs in git the way
sample SQL data conceptually could be): `dataset.yaml` here is a template
the user populates by pointing `MEDIA_LIBRARY_PATH` at their own small
labeled folder and listing known (query -> expected file) pairs.
"""
