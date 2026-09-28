#!/usr/bin/env python3
"""Scans the **production frontend build** (`frontend/dist/` by default) for
credentials that should never reach a browser: OAuth client secrets,
private keys, cloud/API access keys, database-connection-string
credentials, and bare JWTs. Closes a real, confirmed gap: this repo's
existing CI secret scanners (`gitleaks`/`detect-secrets`, `.github/workflows
/ci.yml`'s `secret-scan` job) only ever scan Git-tracked *source* files
(`git ls-files`) -- neither has ever looked at what Vite actually bundled
into `frontend/dist`, which is the one artifact a real browser downloads.

**Never prints a discovered credential's value.** Every finding reports
only its file, line number, and category (`private_key`, `jwt`,
`google_oauth_client_secret`, ...) -- exactly the "report the type and
location, not the value" contract this project's own incident-response
posture requires (see `SECURITY.md`). A real hit here means: stop using
that credential immediately, rotate/revoke it at the provider, and treat
every deployment that already shipped the affected build as compromised --
removing it from a *future* build is not remediation for one already
distributed.

## Two independent checks, run together

1. **Generic pattern matching** (always runs, no backend import needed --
   this is what CI's `frontend` job invokes after `npm run build`, with
   nothing beyond the Python 3 standard library): private key headers, an
   AWS-style access key ID, Google's own documented OAuth client-secret
   prefix (`GOCSPX-`), a database-connection-string embedding a password,
   and a bare JWT (three dot-separated base64url segments) -- the same
   shape `security/redaction.py`'s own regex catches for backend logs,
   applied here to build output instead.
2. **Exact configured-secret-value matching** (`--check-configured-secrets`,
   opt-in, requires this project's own Python environment/dependencies) --
   reuses `security.redaction.configured_secret_fingerprints` to get every
   real secret value *this specific deployment* is configured with
   (`DB_PASSWORD`, `JWT_SECRET_KEY`, `IMA_API_KEY`, ...) and confirms none
   of them appear anywhere in the build, byte-for-byte. This is the
   authoritative check for one specific deployment's own real secrets;
   check 1 above is what a generic CI run (with no real production
   secrets configured at all) can still meaningfully do.

A Google OAuth **client ID** (e.g. `123-abc.apps.googleusercontent.com`)
appearing in the build is expected and safe -- it is a public identifier,
never flagged by either check (this app, by design, doesn't even bake it
into the build at all; see `security/google_oidc.py`'s own module
docstring -- the frontend fetches it at runtime from `GET /health`).

## Usage

    python scripts/scan_frontend_build_for_secrets.py
    python scripts/scan_frontend_build_for_secrets.py --dist-dir frontend/dist
    python scripts/scan_frontend_build_for_secrets.py --check-configured-secrets

Exit code 0: no findings. Exit code 1: one or more findings (fails CI).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# File extensions Vite's own build output actually contains -- deliberately
# excludes binary formats (images/fonts/wasm) this scanner can't
# meaningfully text-search and that never legitimately embed a credential
# as readable text anyway.
_SCANNABLE_EXTENSIONS = {
    ".js",
    ".mjs",
    ".css",
    ".html",
    ".json",
    ".map",
    ".txt",
    ".webmanifest",
    ".xml",
    ".svg",
}

# (category, compiled pattern) -- deliberately separate named categories
# rather than one combined alternation, so a finding's reported category is
# specific (a private key is a different severity/response than a bare
# JWT), matching `security/redaction.py`'s own module docstring's "report
# category, not value" principle.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Google's own documented OAuth 2.0 client-secret prefix -- a client ID
    # never starts this way, so this pattern cannot false-positive against
    # the public client ID this app's build legitimately never even embeds.
    ("google_oauth_client_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35,}\b")),
    (
        "db_connection_credential",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mssql|oracle|mongodb)(?:\+\w+)?://"
            r"[^\s:/@'\"]+:[^\s@'\"]+@",
            re.IGNORECASE,
        ),
    ),
    # A bare JWT -- see security/redaction.py's identical pattern and its
    # own comment on why "ey"-prefixed + two more dot-separated base64url
    # segments is distinctive enough not to false-positive on ordinary text.
    (
        "jwt",
        re.compile(r"\bey[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    file: str
    line: int
    category: str


def scan_text_for_patterns(text: str) -> list[tuple[int, str]]:
    """Returns `(line_number, category)` for every generic-pattern match in
    `text`. Never returns the matched substring itself."""
    hits: list[tuple[int, str]] = []
    lines = text.splitlines()
    for category, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            hits.append((line_number, category))
    del lines  # only used for the line-count computation above
    return hits


def scan_text_for_known_values(
    text: str, known_values: Sequence[tuple[str, str]]
) -> list[tuple[int, str]]:
    """Returns `(line_number, category)` for every occurrence of one of
    `known_values`'s real secret values -- `category` is always
    `"configured_secret[<label>]"`, never the value itself.

    Args:
        known_values: `(label, value)` pairs, e.g. from `security.redaction
            .configured_secret_fingerprints`. Values shorter than 4
            characters are skipped (matching that function's own
            short-value exclusion, to avoid a coincidental match against
            unrelated build content).
    """
    hits: list[tuple[int, str]] = []
    for label, value in known_values:
        if not value or len(value) < 4:
            continue
        start = 0
        while True:
            index = text.find(value, start)
            if index == -1:
                break
            line_number = text.count("\n", 0, index) + 1
            hits.append((line_number, f"configured_secret[{label}]"))
            start = index + 1
    return hits


def _iter_scannable_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SCANNABLE_EXTENSIONS:
            yield path


def scan_directory(
    root: Path, *, known_values: Sequence[tuple[str, str]] = ()
) -> list[SecretFinding]:
    """Scans every scannable file under `root` (recursively) with both
    checks. Returns every finding, sorted by file then line -- never
    raises for an unreadable/binary-ish file, just skips it (this is a
    best-effort net over build output, not a reason the build itself
    should fail to scan)."""
    findings: list[SecretFinding] = []
    if not root.exists():
        return findings

    for path in _iter_scannable_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        relative = str(path.relative_to(root))
        for line_number, category in scan_text_for_patterns(text):
            findings.append(SecretFinding(file=relative, line=line_number, category=category))
        if known_values:
            for line_number, category in scan_text_for_known_values(text, known_values):
                findings.append(SecretFinding(file=relative, line=line_number, category=category))

    findings.sort(key=lambda f: (f.file, f.line, f.category))
    return findings


def format_report(findings: Sequence[SecretFinding]) -> str:
    """Human-readable report -- file/line/category only, never a value.
    Safe to print to CI logs or any other shared output."""
    if not findings:
        return "No credentials found in the scanned build output."
    lines = [f"Found {len(findings)} potential credential exposure(s) in the build output:"]
    for finding in findings:
        lines.append(f"  {finding.file}:{finding.line} -- category: {finding.category}")
    lines.append(
        "\nDo not paste or log the actual value anywhere. Treat any real hit as an "
        "exposed credential: stop using it, rotate/revoke it at the provider, and "
        "review every deployment that already shipped a build containing it -- "
        "removing it from a future build does not remediate one already distributed."
    )
    return "\n".join(lines)


def _load_configured_secret_values() -> list[tuple[str, str]]:
    """Lazy-imports this project's own backend config -- only reached when
    `--check-configured-secrets` is passed, so the default (CI-friendly,
    dependency-free) path never needs this project's Python dependencies
    installed at all."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config.settings import get_settings
    from security.redaction import configured_secret_fingerprints

    settings = get_settings()
    return [(fp.label, fp.value) for fp in configured_secret_fingerprints(settings)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-dir",
        default="frontend/dist",
        help="Directory to scan (default: frontend/dist).",
    )
    parser.add_argument(
        "--check-configured-secrets",
        action="store_true",
        help=(
            "Also check for this deployment's own real configured secret values "
            "(requires config/security modules to be importable)."
        ),
    )
    args = parser.parse_args(argv)

    known_values: list[tuple[str, str]] = []
    if args.check_configured_secrets:
        known_values = _load_configured_secret_values()

    findings = scan_directory(Path(args.dist_dir), known_values=known_values)
    print(format_report(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
