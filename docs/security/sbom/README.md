# Software Bill of Materials

Generated 2026-09-18, CycloneDX format, using tooling this project already
depends on — no new SBOM-generation tool was introduced (`pip-audit` and
`npm` both support CycloneDX natively; per this pass's own "do not
introduce unnecessary tooling" constraint).

| File | Contents | Command | Components |
|---|---|---|---|
| `backend-sbom.cyclonedx.json` | Python/backend dependencies (`requirements.txt`, direct + transitive, resolved against PyPI) | `pip-audit -r requirements.txt --format cyclonedx-json` | 159 |
| `frontend-sbom.cyclonedx.json` | Node/frontend dependencies (`frontend/package-lock.json`, direct + transitive) | `npm sbom --sbom-format cyclonedx` (run from `frontend/`) | 579 |

**Not included:** a container-image-level SBOM (would need the
`text-to-sql-dashboard` image actually built — see
`docs/security/CVE_TRIAGE.md` §4 for why that didn't complete this
session; `syft <image>` or `trivy image --format cyclonedx` would be the
tool once an image exists to point at).

**Regeneration:** re-run both commands above whenever `requirements.txt`
or `frontend/package-lock.json` changes meaningfully (e.g. before a
release) — these are point-in-time snapshots, not continuously
regenerated. Recommended as a CI addition (Phase 11 of this pass's own
brief) rather than a manual step long-term — see
`docs/security/DEPENDENCY_SECURITY_FINAL_REPORT.md`'s supply-chain
section for the specific recommendation (add an SBOM-generation step to
`.github/workflows/ci.yml`, upload as a build artifact).
