# OIDC End-to-End Test

**Status: NOT VERIFIED.** No live Identity Provider (Auth0, Okta, Azure
AD, Keycloak, or otherwise) and no browser-automation tooling were
available in this sandboxed environment, in this session or any prior
session across this repository's history. This has been honestly
disclosed at every prior pass (`docs/AUTHENTICATION.md` line 115,
`SECURITY_FINAL_REPORT.md` §5) and remains true today — re-confirmed by
directly re-reading that disclosure this session, not merely trusting it.

**What has been verified** (code-level, not end-to-end):
- `security/oidc.py`: algorithm allowlist (server-side, never trusts the
  token's own `alg`), mandatory audience validation, bounded clock skew,
  JWKS-based signature verification — covered by `tests/test_oidc.py`
  (forged signature, `alg=none`, wrong issuer/audience, expired/malformed
  tokens), re-run this session, passing.
- `frontend/src/lib/auth.ts`/`store/authStore.ts`: Authorization Code +
  PKCE via `oidc-client-ts`, in-memory-only token storage. Passes
  `tsc --noEmit`, `oxlint`, `npm run build` (all re-run this session,
  clean).
- Config-time validation: `Settings._validate_oidc_algorithms` refuses to
  start if `OIDC_ALGORITHMS` includes `none`; the frontend's `frame-src`
  CSP directive is derived from `OIDC_ISSUER` when set (needed for silent-
  renew's hidden iframe).

**What has never been exercised, and cannot be from this environment:**

1. Browser → real IdP login redirect (actual consent screen, actual
   credential entry)
2. Authorization Code exchange against a real token endpoint
3. Silent-renew-via-hidden-iframe behavior across real browsers (Chrome/
   Firefox/Safari each have different third-party-cookie/iframe policies
   that a mocked test cannot reproduce)
4. Real JWKS key rotation (does this app's caching correctly pick up a
   rotated key without a restart?)
5. Sign-out-at-IdP propagating back to this app's own session state
6. Behavior when the IdP is temporarily unreachable at token-validation
   time (versus at login time, which is a different code path)

## Procedure to actually close this gap

Requires a real or realistic test IdP tenant (any of Auth0/Okta/Azure AD/
Keycloak's free/dev tiers would work) and a browser:

1. Configure `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`VITE_OIDC_AUTHORITY`/
   `VITE_OIDC_CLIENT_ID` against the test tenant.
2. Click through: dashboard → redirect to IdP → login → consent →
   callback → confirm the dashboard renders as an authenticated user with
   the expected role(s) from the IdP's claims.
3. Leave the tab open past the access token's expiry — confirm silent
   renew works without a visible redirect or a forced re-login.
4. Sign out at the IdP directly (not via this app) — confirm this app's
   own session state reflects that on next action/reload.
5. Rotate the signing key at the IdP (most providers support this in
   their admin UI) — confirm this app's JWKS fetch picks up the new key
   without restarting the API process.
6. Attempt a request with a token from a *different* configured client
   ID / different issuer — confirm rejection (this specific case IS
   covered by the existing mocked `tests/test_oidc.py`, but re-confirming
   it against a real IdP-issued token is still worth doing once, to catch
   any claim-shape mismatch a mock might not reproduce).

Until this procedure has actually been run, `docs/security/FINAL_PRODUCTION_GATE.md`'s
OIDC row stays `PARTIAL`, not `PASS` — per this gate's own explicit rule
not to convert source-code-level confidence into a live-flow PASS.
