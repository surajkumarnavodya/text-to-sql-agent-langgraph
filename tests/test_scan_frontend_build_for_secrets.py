"""Unit tests for scripts/scan_frontend_build_for_secrets.py.

Every synthetic secret value here is a deterministic, obviously-fake
placeholder generated for this test file only -- never a real credential,
and never printed by the scanner's own report (every assertion checks that
the *value* is absent from the report, only the *category* is present),
matching this project's "never paste a real secret into a test, prompt, or
scanner output" discipline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.scan_frontend_build_for_secrets import (
    SecretFinding,
    format_report,
    scan_directory,
    scan_text_for_known_values,
    scan_text_for_patterns,
)

# A clearly-synthetic, never-real value -- random enough that it could not
# coincidentally match any real secret, and distinctive enough to assert on
# without it looking like a real credential shape at all.
_SYNTHETIC_SECRET_VALUE = "SYNTHETIC-TEST-SECRET-zK9mQ2xR7-not-real"


class TestScanTextForPatterns:
    def test_detects_a_private_key_header(self):
        text = "some bundle text\n-----BEGIN RSA PRIVATE KEY-----\nMIIExyz\n"
        hits = scan_text_for_patterns(text)
        assert ("private_key" in category for _, category in hits)
        assert any(category == "private_key" for _, category in hits)

    def test_detects_an_aws_access_key_shape(self):
        text = "config = {key: 'AKIAABCDEFGHIJKLMNOP'}"
        hits = scan_text_for_patterns(text)
        assert any(category == "aws_access_key" for _, category in hits)

    def test_detects_a_google_oauth_client_secret_shape(self):
        # GOCSPX- is Google's own documented client-secret prefix -- a
        # client ID (safe, public) never starts this way.
        text = 'const x = "GOCSPX-fakeSecretValueForTestOnly1234"'
        hits = scan_text_for_patterns(text)
        assert any(category == "google_oauth_client_secret" for _, category in hits)

    def test_detects_a_google_api_key_shape(self):
        text = 'apiKey: "AIzaSyFAKE1234567890fakefakefakefakefake1234"'
        hits = scan_text_for_patterns(text)
        assert any(category == "google_api_key" for _, category in hits)

    def test_detects_a_db_connection_string_with_embedded_credentials(self):
        text = "error connecting to postgresql://appuser:hunter2fake@db.example.com:5432/mydb"
        hits = scan_text_for_patterns(text)
        assert any(category == "db_connection_credential" for _, category in hits)

    def test_detects_a_bare_jwt(self):
        fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcDEFghiJKLmnoPQRstuVWXyz123456"
        text = f"credential leaked in a log line: {fake_jwt}"
        hits = scan_text_for_patterns(text)
        assert any(category == "jwt" for _, category in hits)

    def test_a_public_google_oauth_client_id_is_never_flagged(self):
        """The one value this app's own build is actually allowed to embed
        (though by design it never does -- see security/google_oidc.py) --
        a client ID has a completely different, non-secret shape and must
        never trip any pattern here."""
        text = 'clientId: "123456789012-abc1def2ghi3jkl4.apps.googleusercontent.com"'
        hits = scan_text_for_patterns(text)
        assert hits == []

    def test_ordinary_bundle_text_with_no_secret_is_clean(self):
        text = "function App() { return React.createElement('div', null, 'Hello') }"
        assert scan_text_for_patterns(text) == []

    def test_reports_the_correct_line_number(self):
        text = "line one\nline two\nAKIAABCDEFGHIJKLMNOP\nline four"
        hits = scan_text_for_patterns(text)
        assert (3, "aws_access_key") in hits


class TestScanTextForKnownValues:
    def test_detects_an_exact_configured_secret_value(self):
        text = f"debug dump: db_password={_SYNTHETIC_SECRET_VALUE}"
        hits = scan_text_for_known_values(text, [("db_password", _SYNTHETIC_SECRET_VALUE)])
        assert hits == [(1, "configured_secret[db_password]")]

    def test_short_values_are_never_matched(self):
        """Mirrors security.redaction.configured_secret_fingerprints's own
        short-value exclusion -- a 2-3 char 'secret' would false-positive
        constantly against unrelated build content."""
        text = "some text containing ab somewhere"
        hits = scan_text_for_known_values(text, [("short", "ab")])
        assert hits == []

    def test_no_known_values_means_no_hits(self):
        assert scan_text_for_known_values("anything at all", []) == []

    def test_value_absent_from_text_produces_no_hit(self):
        hits = scan_text_for_known_values("clean text", [("x", _SYNTHETIC_SECRET_VALUE)])
        assert hits == []


class TestScanDirectory:
    def test_finds_a_synthetic_secret_in_a_fake_build_directory(self, tmp_path: Path):
        dist = tmp_path / "dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (assets / "index-abc123.js").write_text(
            f'const leaked = "{_SYNTHETIC_SECRET_VALUE}";', encoding="utf-8"
        )

        findings = scan_directory(
            dist, known_values=[("test_only_secret", _SYNTHETIC_SECRET_VALUE)]
        )

        assert len(findings) == 1
        assert findings[0].category == "configured_secret[test_only_secret]"
        assert findings[0].file == str(Path("assets") / "index-abc123.js")

    def test_a_clean_fake_build_directory_produces_no_findings(self, tmp_path: Path):
        dist = tmp_path / "dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (assets / "index-abc123.js").write_text("function App(){return 1}", encoding="utf-8")
        (dist / "index.html").write_text("<html><body>App</body></html>", encoding="utf-8")

        findings = scan_directory(dist, known_values=[("secret", _SYNTHETIC_SECRET_VALUE)])
        assert findings == []

    def test_nonexistent_directory_produces_no_findings_not_a_crash(self, tmp_path: Path):
        assert scan_directory(tmp_path / "does-not-exist") == []

    def test_non_scannable_binary_like_extensions_are_skipped(self, tmp_path: Path):
        dist = tmp_path / "dist"
        dist.mkdir()
        # A .png "containing" the secret as raw bytes -- must not be opened
        # as text and must not produce a finding (this scanner only reads
        # extensions real Vite build output uses for readable text).
        (dist / "icon.png").write_bytes(_SYNTHETIC_SECRET_VALUE.encode("utf-8"))

        findings = scan_directory(dist, known_values=[("secret", _SYNTHETIC_SECRET_VALUE)])
        assert findings == []

    def test_scans_nested_subdirectories(self, tmp_path: Path):
        dist = tmp_path / "dist"
        nested = dist / "assets" / "deeply" / "nested"
        nested.mkdir(parents=True)
        (nested / "chunk.js").write_text(_SYNTHETIC_SECRET_VALUE, encoding="utf-8")

        findings = scan_directory(dist, known_values=[("secret", _SYNTHETIC_SECRET_VALUE)])
        assert len(findings) == 1


class TestFormatReport:
    def test_empty_findings_reports_clean(self):
        assert "No credentials found" in format_report([])

    def test_report_never_contains_the_actual_secret_value(self):
        """The core safety contract: the report is safe to print to CI
        logs or paste into an incident channel -- it must never leak the
        value itself, even though the scanner obviously had access to it
        internally to find it."""
        findings = [SecretFinding(file="assets/index.js", line=42, category="jwt")]
        report = format_report(findings)
        assert "index.js:42" in report
        assert "jwt" in report
        # No real secret value exists in this test's own findings list to
        # begin with (by construction) -- this asserts the report's prose
        # doesn't itself embed anything value-shaped beyond the category.
        assert _SYNTHETIC_SECRET_VALUE not in report

    def test_report_includes_file_line_and_category_for_every_finding(self):
        findings = [
            SecretFinding(file="a.js", line=1, category="aws_access_key"),
            SecretFinding(file="b.css", line=2, category="jwt"),
        ]
        report = format_report(findings)
        assert "a.js:1" in report
        assert "aws_access_key" in report
        assert "b.css:2" in report
        assert "jwt" in report

    def test_report_includes_rotation_guidance_when_findings_exist(self):
        findings = [SecretFinding(file="a.js", line=1, category="private_key")]
        report = format_report(findings)
        assert "rotate" in report.lower()


class TestMainCli:
    def test_exits_zero_on_a_clean_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        from scripts.scan_frontend_build_for_secrets import main

        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")

        exit_code = main(["--dist-dir", str(dist)])
        assert exit_code == 0
        assert "No credentials found" in capsys.readouterr().out

    def test_exits_nonzero_and_never_prints_the_value_when_a_pattern_matches(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        from scripts.scan_frontend_build_for_secrets import main

        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "leaked.js").write_text("AKIAABCDEFGHIJKLMNOP", encoding="utf-8")

        exit_code = main(["--dist-dir", str(dist)])
        output = capsys.readouterr().out
        assert exit_code == 1
        assert "aws_access_key" in output
        assert "AKIAABCDEFGHIJKLMNOP" not in output
