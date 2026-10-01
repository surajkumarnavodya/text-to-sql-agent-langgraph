#!/usr/bin/env python3
"""Compares a "before" `.secrets.baseline` snapshot against the current one
and fails (exit 1) if anything beyond the `generated_at` timestamp differs --
i.e. `detect-secrets scan --baseline FILE` (run just before this script, see
`.github/workflows/ci.yml`'s `secret-scan` job) found a genuinely new,
un-reviewed finding. A naive `git diff --exit-code .secrets.baseline` would
fail on *every* run even with zero new secrets, since `--baseline` always
rewrites the file's own `generated_at` field -- this strips that field from
both sides before comparing, so the step only fails on a real new finding
(verified: a planted fake PEM private key in a new file is correctly caught
by this exact comparison).

**Why this is its own script, not an inline `python -c "..."` in the
workflow YAML (where it used to live):** the message printed on a real
finding names the remediation command, `` `detect-secrets audit
.secrets.baseline` ``, in backticks -- inside a `run: |` block's `python -c
"..."` argument, those backticks are **not** inert Python syntax to bash;
bash performs command substitution on any unescaped backtick pair inside a
double-quoted string, unconditionally, while constructing the string that
becomes `-c`'s argument, regardless of where that text falls inside the
Python source's own (unrelated) control flow. In practice this meant
`detect-secrets audit .secrets.baseline` -- an interactive, TTY-expecting
command -- was being executed as a subshell substitution on every single
run of that step, and its own output (not valid Python) was spliced into
the middle of the `print(...)` call, corrupting the script into a syntax
error. A real, confirmed, deterministic bug (reproduced locally via
`bash -x`, which shows the substitution firing), not a hypothetical --
found and fixed by moving this logic into a real `.py` file, where a
backtick is never anything but a literal character.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "before_snapshot",
        type=Path,
        help="Path to the baseline file as it was before `detect-secrets scan --baseline` ran.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".secrets.baseline"),
        help="Path to the current baseline file, after the scan ran (default: .secrets.baseline).",
    )
    args = parser.parse_args(argv)

    before = json.loads(args.before_snapshot.read_text(encoding="utf-8"))
    after = json.loads(args.baseline.read_text(encoding="utf-8"))
    before.pop("generated_at", None)
    after.pop("generated_at", None)

    if before != after:
        print(
            "New, un-baselined finding(s) detected -- review with "
            "`detect-secrets audit .secrets.baseline` and commit the result."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
