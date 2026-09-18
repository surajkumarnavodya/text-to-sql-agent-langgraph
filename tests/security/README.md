# tests/security/

Adversarial-scenario regression tests, added in the 2026-09-18
CI-gate-hardening pass. This directory is **additive**, not a
reorganization: this codebase already has extensive security-relevant
coverage scattered across `tests/test_sql_validator_hardening.py`,
`tests/test_adversarial_input.py`, `tests/test_media_gen.py` (SSRF),
`tests/test_api_authz.py`/`tests/test_api_chat_history.py`/
`tests/test_api_documents.py` (RBAC/IDOR), `tests/test_rate_limit.py`, and
others — none of that was moved here, and none of it is duplicated here.
Everything in this directory targets a gap that genuinely wasn't covered
anywhere else at the time it was written:

- `test_malware_scanner_gate.py` — the `security/malware_scanner.py`
  abstraction and its fail-closed wiring into `rag/ingestion.py`/
  `media/ingest.py` (a brand-new capability this same pass added — no
  prior tests could have existed for it).
- `test_prompt_injection_multilingual_and_encoded.py` — Base64/URL-encoded
  injection phrasing and non-Latin-script (Hindi/Chinese/Arabic) injection
  attempts, neither of which `test_adversarial_input.py` exercises (that
  file covers plain-English phrasing and Latin-script Unicode homoglyphs/
  zero-width obfuscation only).
- `test_rate_limit_header_spoofing.py` — locks in that
  `api/rate_limit.py` never trusts a client-suppliable
  `X-Forwarded-For`/`X-Real-IP` header (it already doesn't; this is a
  regression test against a future change accidentally introducing that
  trust without a proxy-allowlist check).

Before adding a new file here, check whether the scenario already has
coverage under its own module's `tests/test_<module>.py` first — this
directory is for genuinely cross-cutting adversarial scenarios or brand
-new security capabilities, not a place to relocate existing tests.
