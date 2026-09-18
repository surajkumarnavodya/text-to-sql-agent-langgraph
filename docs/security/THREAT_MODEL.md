# Threat Model — Pointer

The full STRIDE-format threat model for this application already exists
at [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md) (79 lines: assets,
Spoofing/Tampering/Repudiation/Information-Disclosure/DoS/Elevation-of-
Privilege tables, each with threat/impact/likelihood/mitigation/residual-
risk columns) plus a dedicated
[`docs/FILE_UPLOAD_THREAT_MODEL.md`](../FILE_UPLOAD_THREAT_MODEL.md) for
the file-ingestion attack surface specifically. This document is
deliberately **not** a rewrite of either — re-authoring a substantive,
already-accurate artifact would violate this gate's own "do not make
unnecessary changes" rule. Instead: what this final gate independently
verified about it, and what's changed since it was last dated.

## Spot-verification performed this session

`docs/THREAT_MODEL.md` cites specific test classes as evidence for several
mitigations (e.g. `tests/test_api_authz.py::TestVerticalPrivilegeEscalation`/
`TestHorizontalPrivilegeEscalation`). These were independently confirmed
to actually exist via `grep` this session, not taken on the document's own
word — per this gate's Rule #1 ("previous documentation is evidence to
investigate, not proof"). Confirmed present and passing.

## What's changed since `docs/THREAT_MODEL.md`'s own "2026 Phase 2" dating

Not reflected in that document's own tables (a currency gap, not an
accuracy gap in what it does cover):

- **Elevation of Privilege / Information Disclosure**: Phase 3 added
  per-document `restricted_roles` RBAC (on top of the fixed sensitivity
  categories the existing document already covers) and blocked
  system-catalog/`information_schema` SQL access — both closing real,
  previously-open items the existing document's own "remaining risk"
  columns still describe as open.
- **Tampering / DoS (file upload)**: the mandatory content-moderation gate
  (Azure Content Safety + text blocklist) and, this engagement, the
  `MalwareScanner` abstraction — neither existed when the existing
  document was written; that document's own file-upload rows predate both.
- **New asset, not in the existing document's asset table**: the
  universal server-side chat history (`identity.models.Conversation`/
  `Prompt`/`AiOutput`) — a locally-authenticated user's full conversation
  history is now a persistent asset with its own IDOR surface (tested,
  see `docs/security/FINAL_PRODUCTION_GATE.md`'s IDOR row), not covered
  by the existing document's asset table at all.
- **New AI-specific threats this engagement's own work surfaced,
  not in the existing document**: encoded/multilingual prompt-injection
  bypass of the regex detection layer (Base64/URL-encoding/non-Latin
  script — confirmed as a real, disclosed detection-layer gap this
  engagement, structurally contained by the unchanged SQL-validator
  backstop) — see `tests/security/test_prompt_injection_multilingual_and_encoded.py`.

**Recommendation, not performed this session** (a documentation refresh,
not a code change, but a real one worth doing deliberately rather than
folded into this gate): update `docs/THREAT_MODEL.md`'s own tables to
reflect the four items above, and re-date it. Left as a named follow-up
rather than rewritten here, to avoid two competing, drifting copies of the
same STRIDE analysis.
