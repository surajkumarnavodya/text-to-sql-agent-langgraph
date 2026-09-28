# Authentication

**Status: implemented, 2026 Phase 2.** This document describes the current authentication architecture — what existed before this pass, what was added, and how to configure each mode. See `docs/AUTHORIZATION.md` for what an authenticated identity is then allowed to *do*.

## Before this pass

A single optional static bearer token (`API_AUTH_TOKEN`, `api/auth.py`) — a timing-safe comparison against one shared secret, no-op when unset (the default). Explicitly documented as "a lightweight hook, not a full auth system": no per-user identity, no expiry, no rotation, no issuer/audience concept. Adequate for a single trusted operator on a local network; not adequate for an enterprise deployment with distinct human users. This mode still exists, unchanged in mechanism, and is still the right choice for machine-to-machine/CI callers — see "Combining modes" below.

## What Phase 2 added

Standard OIDC ID-token (JWT) validation as a second, production-grade authentication mode, implemented in `security/oidc.py` and dispatched from the same `api/auth.py::verify_api_key` FastAPI dependency every route already depended on — **no route file changes were needed**, the dependency's external contract (raise 401 or allow) is unchanged.

### The three modes (`Settings.auth_mode`)

`auth_mode` is a computed property, not a separate flag to keep in sync — it's derived from which of `OIDC_ISSUER` / `API_AUTH_TOKEN` are actually configured:

| `auth_mode` | Condition | Behavior |
|---|---|---|
| `"none"` | Neither `OIDC_ISSUER` nor `API_AUTH_TOKEN` set | No credential required (dev default, unchanged from before this pass) |
| `"static_token"` | `API_AUTH_TOKEN` set, `OIDC_ISSUER` unset | The original shared-secret bearer check |
| `"oidc"` | `OIDC_ISSUER` set | JWT validation against a real identity provider |

### Combining modes

`OIDC_ISSUER` and `API_AUTH_TOKEN` may be configured **together**. `auth_mode` reports `"oidc"` as primary, but `verify_api_key` tries OIDC validation first and falls back to the static-token check if that fails — this lets interactive human users authenticate via a real IdP while service/CI callers keep using a static secret, without needing to mint machine identities in the IdP for every automated caller. Either credential alone is sufficient.

### What OIDC validation checks (`security/oidc.py::validate_token`)

All in one `jwt.decode()` call, so there's exactly one place all of these are enforced together, not several ad hoc checks a future change could accidentally skip one of:

- **Signature** — against the identity provider's real public key, resolved via JWKS and the token's own `kid` header (`jwt.PyJWKClient`, which handles fetch/cache/key-rotation).
- **Algorithm** — checked against `OIDC_ALGORITHMS` (default `RS256`), **read from server-side config, never from the token's own `alg` header**. This is the standard defense against JWT "alg confusion" attacks — a token cannot talk its way into a weaker or absent verification scheme by simply claiming a different algorithm. `alg: none` can never be accepted; `Settings._validate_oidc_algorithms` refuses to even start if `OIDC_ALGORITHMS` is configured to include it.
- **Issuer** (`iss`) — must exactly match `OIDC_ISSUER`.
- **Audience** (`aud`) — must contain `OIDC_AUDIENCE`. `Settings._validate_oidc_requires_audience` refuses to start if `OIDC_ISSUER` is set without `OIDC_AUDIENCE` — an issuer check alone would accept a token minted for a *different* application at the same identity provider.
- **Expiration / not-before** (`exp`/`nbf`/`iat`, `iat` required) — with `OIDC_CLOCK_SKEW_SECONDS` (default 60s) leeway for ordinary clock drift between this server and the IdP.
- **Subject** (`sub`) — required; becomes `AuthIdentity.subject`.

### Configuration (`.env`)

```bash
# --- OIDC/JWT authentication (production-grade) ---
OIDC_ISSUER=https://your-tenant.auth0.com/
OIDC_AUDIENCE=your-api-identifier
# Optional -- discovered from <issuer>/.well-known/openid-configuration if unset
OIDC_JWKS_URL=
OIDC_ALGORITHMS=RS256
OIDC_CLOCK_SKEW_SECONDS=60
# Which JWT claim carries the caller's role(s) -- see docs/AUTHORIZATION.md
OIDC_ROLE_CLAIM=roles

# --- Deployment posture ---
ENVIRONMENT=production
```

Any standard OIDC-compliant provider works — Auth0, Okta, Azure AD/Entra ID, Keycloak, or a self-hosted one. This app is a pure **relying party**: it only validates tokens; it does not issue them, run a login UI, or implement the OAuth2 authorization-code exchange. Point your frontend/API clients at your IdP's normal login flow and pass the resulting access/ID token as `Authorization: Bearer <token>`.

### Dev vs. production — explicit separation, fail-closed

`ENVIRONMENT` (`development` default, or `production`) is a narrow, single-purpose flag: `Settings._require_identity_in_production` refuses to start the application at all if `ENVIRONMENT=production` and `auth_mode` would resolve to `"none"` — i.e., a production deployment can never silently come up with every endpoint (including `POST /execute`, document deletion, and paid media generation) open to any network caller. A `development`-environment deployment is completely unaffected by this check — a fresh clone with no `.env` auth config still starts and behaves exactly as it always did.

```
$ ENVIRONMENT=production uvicorn api.main:app
config.settings.ConfigurationError: ENVIRONMENT=production requires authentication
to be configured -- refusing to start with no identity check of any kind...
```

### Security properties verified by test (`tests/test_oidc.py`, `tests/test_settings_validation.py`)

- Valid token → correct identity extracted (subject + roles).
- Expired token → rejected.
- Wrong issuer → rejected.
- Wrong audience → rejected.
- Token signed by an unrelated key (forged claims, no real signature) → rejected.
- `alg: none` (unsigned) token → rejected, regardless of claims.
- Unknown `kid` → rejected.
- Missing `sub` claim → rejected.
- Malformed token → rejected.
- **No raw token ever appears in a log line**, on any rejection path.
- `OIDC_ISSUER` without `OIDC_AUDIENCE` → refuses to start.
- `OIDC_ALGORITHMS` empty or containing `"none"` → refuses to start.
- `ENVIRONMENT=production` with no auth configured → refuses to start.

### No tokens/credentials in logs (verified)

- `api/auth.py`'s failure logging (`log_security_event("auth_failed", ...)`) logs only `reason` (`missing_header`/`invalid_token`/`invalid_credential`) and client IP — never the `Authorization` header value.
- `security/oidc.py`'s failure logging (`log_security_event("oidc_token_rejected", ...)`) logs only a stable `reason` string (`expired`/`invalid_issuer`/`invalid_audience`/.../`invalid_token`) — never the raw JWT or its decoded claims.
- `TokenValidationError` messages are short, generic, and safe to return directly in a 401 body — the underlying `PyJWTError`'s own text (which can echo token-internal values) is never propagated to a log or a response.

### Secure configuration / no credentials in source

- `OIDC_ISSUER`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` are plain (non-secret) config — an issuer URL and audience identifier are not sensitive.
- `API_AUTH_TOKEN` remains a `pydantic.SecretStr`, unchanged.
- Nothing OIDC-related is hardcoded anywhere in source — every value is `.env`-driven, consistent with this project's existing "never hardcode a connection detail" convention (`CLAUDE.md`'s folder-conventions section).

## Frontend OIDC login (2026 Phase 3)

**Status: implemented.** Before this pass, the shipped React dashboard had no way to actually *obtain* an OIDC token — its only possible credential was `VITE_API_AUTH_TOKEN`, a single shared secret baked into the built JS bundle at compile time. Two compounding problems this closed: **(a)** a build-time-baked value ships inside static files served to every visitor — extractable via the browser's devtools/network tab by anyone who can reach the deployed app, not actually secret once deployed publicly; **(b)** per the static-token-grants-admin note below, that extractable token is an admin-equivalent credential. A production deployment that set `ENVIRONMENT=production` (required to boot at all) and used a static token to satisfy that requirement — since OIDC had no frontend integration — while serving the dashboard to real users, shipped every visitor an admin-equivalent credential in plain sight.

`frontend/src/lib/auth.ts` + `frontend/src/store/authStore.ts` now implement a real Authorization Code + PKCE flow (via `oidc-client-ts`, the standard, actively-maintained OIDC/OAuth2 client library — a hand-rolled PKCE implementation was deliberately avoided given how easy this class of flow is to get subtly wrong):

- **Configuration is separate, build-time, frontend-only `.env` vars**: `VITE_OIDC_AUTHORITY`, `VITE_OIDC_CLIENT_ID`, `VITE_OIDC_SCOPE`, `VITE_OIDC_REDIRECT_URI`, `VITE_OIDC_POST_LOGOUT_REDIRECT_URI` (see `.env.example`'s own comments). These are **not secrets** — a public OIDC client (Authorization Code + PKCE, no `client_secret`) is designed to be embedded in a public SPA; the authority/client_id pair alone grants nothing without an interactive login. Must be registered as its own **public/SPA client** at the identity provider (distinct from any backend service client), with `VITE_OIDC_REDIRECT_URI` registered as an allowed redirect URI there.
- **Leaving `VITE_OIDC_AUTHORITY`/`VITE_OIDC_CLIENT_ID` unset (the default) is a complete no-op** — the dashboard behaves exactly as it always did (falls back to `VITE_API_AUTH_TOKEN` or no auth), zero behavior change for the common local/single-operator deployment. This mirrors `auth_mode == "none"`'s own "unchanged unless explicitly opted into" posture on the backend.
- **Token storage is in-memory only** (`lib/auth.ts`'s `InMemoryWebStorage`, a plain in-process `Map`) — never `localStorage`/`sessionStorage`. Nothing OIDC-related is ever written to a browser storage API an XSS payload could read after the fact. The tradeoff: a hard page refresh clears the in-memory token, so app load always attempts a **silent** (`prompt=none`, hidden-iframe) re-authentication against the IdP's own session cookie before ever falling back to a visible sign-in screen — the same effective UX a persisted store would give for the common case (an already-logged-in IdP session), without the token itself ever touching a persisted store.
- **`AuthGate.tsx`** wraps the authenticated app (not `/auth/callback`, which must stay reachable to complete the flow) — shows a spinner during the silent-reauth attempt, then either the app (if authenticated) or an explicit "Sign in" screen. **This gate is UX only, never the real security boundary** — the same principle `agent/authz.py`'s own docstring states for RBAC: every API route independently re-validates the bearer token server-side regardless of whether this component ever rendered.
- **`getBearerToken()`** (`store/authStore.ts`) mirrors `api/auth.py::verify_api_key`'s own dispatch order exactly: the OIDC access token when a user is signed in, falling back to the static `VITE_API_AUTH_TOKEN` otherwise (service/CI-style callers, or OIDC not configured at all).
- **CSP `frame-src`** (`api/main.py::_default_csp`) is derived from the backend's own `OIDC_ISSUER` when set, adding that origin alongside `'self'` — OIDC silent-renew loads the identity provider in a hidden iframe, which (unlike the interactive `signinRedirect()` flow, a full top-level navigation CSP doesn't restrict at all) is subject to `frame-src`; without this, the default CSP's `default-src 'self'` inheritance would silently block every silent-renew attempt.

## Google sign-in (2026-09-28)

**Status: implemented, staging/live sign-in not yet verified end-to-end (see "What's verified vs. not" below).** A `local_auth_enabled` deployment (this app's own self-hosted accounts, `identity/` — Argon2id passwords, JWT access + rotating refresh tokens, see `docs/authentication-and-password-policy.md`) can additionally offer "Continue with Google" as a second way to reach the *same* kind of account, on both sign-in and sign-up. This is layered entirely on top of the existing local-account system; it does not touch OIDC mode, the static-token mode, or `agent/authz.py`'s RBAC.

### Chosen flow, and why

**Google Identity Services (GIS) "Sign In With Google" ID-token flow** (the GIS JS library's `google.accounts.id.initialize()`/`renderButton()`, callback delivers a signed ID token) — not the OAuth 2.0 authorization-code flow, and not the deprecated implicit flow (rejected outright; it returns an access token with no signed identity assertion and no viable server-side verification path).

The deciding factor: this app only ever needs to know *who signed in*, never to call any Google API on the user's behalf (no Gmail, no Drive, no Calendar) — Google sign-in here is authentication only, never authorization to anything, including never to a user's own configured SQL database. The ID-token flow gives exactly that: a short-lived, signed JWT asserting an identity, verified entirely server-side, with:

- **No client secret anywhere.** The GIS flow is designed for public clients (SPAs); there is nothing to leak, unlike an authorization-code exchange which would require a `client_secret` — a *server*-side secret with no natural home in a codebase whose only server-side Google integration is token verification. `GOOGLE_OAUTH_CLIENT_ID` is the only Google-related config value this app has, and a client ID is explicitly not a secret (see "Secret hygiene" below).
- **Minimal scope, implicitly.** GIS's basic sign-in flow only ever requests `openid email profile` — there is no scope parameter to widen, and no code path in this app that could request a Gmail/Drive/Calendar scope even by mistake, because the authorization-code flow that would need those scopes was never implemented.
- **No Google tokens persisted anywhere**, client or server. The raw ID token (`credential`) is sent once to `POST /auth/google`, verified, and discarded — never written to a database, a session, or a cookie. What *is* issued afterward is this app's own existing JWT access token + refresh-token cookie (see "Sessions" below) — a Google sign-in produces the exact same session artifact a password login does.

### Server-side token verification (`security/google_oidc.py::verify_google_id_token`)

Every check below runs on every `POST /auth/google` and `POST /auth/google/link` call, via one call to Google's own `google-auth` Python library (`google.oauth2.id_token.verify_oauth2_token`) plus this app's own checks layered on top — never a hand-rolled JWT decode:

| Check | How |
|---|---|
| Signature | Verified against Google's real, rotating public keys, fetched live from Google's own certs endpoint (`google.auth.transport.requests.Request`, cached/rotated by the library itself) — never trusts the token's own `alg`/`jku`/`x5u`/`kid` to pick a key or algorithm; the library resolves both from the matched JWKS key. |
| Issuer (`iss`) | Restricted to Google's own issuer strings (`accounts.google.com`/`https://accounts.google.com`) — enforced inside `verify_oauth2_token` itself, not re-implemented here. |
| Audience (`aud`) | Must exactly equal `GOOGLE_OAUTH_CLIENT_ID` — a token minted for a *different* Google OAuth client (including a different app entirely) is rejected even if otherwise perfectly valid. |
| `azp` | If present, must also equal `GOOGLE_OAUTH_CLIENT_ID` — catches the case where a token's `aud` and `azp` disagree (a Google-documented signal worth checking independently of `aud` alone). |
| Expiry / issued-at | `exp`/`iat` checked by the library, with `GOOGLE_OAUTH_CLOCK_SKEW_SECONDS` (default 60s) bounded leeway — the same bounded-skew posture `security/oidc.py`'s existing OIDC validation already uses for its own IdP tokens. |
| Nonce (replay/CSRF) | `GET /auth/google/nonce` issues a server-generated, single-use, TTL-bounded (`GOOGLE_OAUTH_NONCE_TTL_SECONDS`, default 300s) nonce, embedded in the GIS `initialize({nonce})` call before the button ever renders. `verify_google_id_token` requires the signed token's own `nonce` claim to match one this server actually issued and hasn't already consumed (`consume_signin_nonce` — pop-once, in-memory, bounded `OrderedDict`, same shape as `attachments.ai_edit`'s idempotency cache). A replayed or forged-nonce token is rejected. **Honest limitation**: this defends against replaying a *captured* credential after this server has already consumed it, and against a token minted for an unrelated sign-in attempt — it does not, and cannot, prevent a token being used within its own short lifetime if an attacker captures it before the legitimate exchange completes (the same limitation any bearer-token scheme has absent token binding). |
| `sub` | Required, non-empty — the **only** durable identity key this app ever stores (`ExternalIdentity.provider_subject`). Email/name/picture are display-only and never used as a lookup key. |
| `email` / `email_verified` | `email` required, non-empty. `email_verified` is read from the signed claim and drives the identity-linking decision below — an unverified email is never treated as proof of ownership of that address. |
| `hd` (hosted domain) | If `GOOGLE_OAUTH_ALLOWED_HOSTED_DOMAINS` is configured (comma-separated), the token's own signed `hd` claim must be present and in that list — **never inferred from the email's `@domain` suffix**, which is trivially spoofable by any Gmail-style provider claim; `hd` is itself a claim Google only sets for real Google Workspace accounts and signs along with everything else. |
| Malformed/oversized input | A `credential` that isn't a non-empty string, or exceeds 8192 characters, is rejected before ever reaching the verification library. |

Every rejection path returns a short, generic, safe message (never the underlying library exception text) and logs a structured `google_signin_token_rejected` security event with a stable `reason` code (`empty_or_malformed_input`/`oversized_input`/`issuer_rejected`/`token_verification_failed`/`azp_mismatch`/`missing_subject`/`missing_email`/`hosted_domain_not_allowed`/`nonce_missing_or_unknown`) — **the raw token is never logged, on any path**, matching this codebase's existing "no raw token ever appears in a log line" property from OIDC mode above.

Both `GET /auth/google/nonce` and `POST /auth/google` are rate-limited per caller (`agent.rate_limit.BoundedLimiterCache`, the same mechanism `/auth/login`/`/auth/register` already use), preventing both nonce-exhaustion and brute-force token-guessing patterns.

### Identity linking rules (`identity/repositories/external_identities.py`)

- **Immutable internal user IDs, unaffected by Google.** `identity.models.User.id` (a UUID) never changes based on how a user signs in.
- **`(provider="google", provider_subject=sub)` is the durable mapping** (`ExternalIdentity`, a real, database-level `UNIQUE(provider, provider_subject)` constraint) — the same Google account always resolves to the same local user, across any browser or device, indefinitely (Google's own email on that account can change; `sub` never does — verified by test, see `test_returning_sign_in_uses_stored_subject_even_if_email_changed_since`).
- **Race-safe first-login creation.** Two near-simultaneous first sign-ins for the same never-before-seen Google identity (two browser tabs, a double-click) resolve to *one* user, never two — the unique constraint on `provider_subject`, not an application-level check-then-insert, is what actually prevents the race; `link_external_identity` catches the resulting `IntegrityError` and re-resolves to whichever row won, rather than crashing or creating a duplicate.
- **A Google-only account has no password** (`users.password_hash IS NULL`) — never silently given one. Local email/password login for such an account fails with the same generic 401 every other wrong-password attempt gets (not a 500 — this was a real bug found and fixed while building this, see `tests/test_api_auth_google.py::TestGoogleOnlyAccountLocalLoginRegression`). The account settings' "Connected accounts" panel lets such a user set a first password later via the *existing* `POST /auth/change-password` route (no current-password check is possible or required when there isn't one yet).
- **Never auto-merged by email alone.** A brand-new Google sign-in whose email matches an *existing local (password) account* is refused (`ExternalAccountEmailConflictError`, HTTP 409, "sign in with your password, then link Google from settings") rather than silently attached to that account — matching email is not proof of controlling it. The **only** way to attach a Google identity to an existing account is `POST /auth/google/link`, which requires an already-authenticated local session (`user_id` is taken from the caller's own verified access token, never from the request body) — an explicit, consenting action, not an automatic inference.
  - The one exception, and why it's *not* a merge-by-email gap: if Google's own `email_verified` claim is `false` and the email still collides with an existing local account (an attacker-controlled unverified address happening to match), the response is a **generic** 401 (`GoogleSignInProvisioningError`) that does not confirm an account exists — `users.email` has a real database-level `UNIQUE` constraint, so a second account genuinely cannot be created either way, but the two cases (verified vs. unverified collision) must not be distinguishable from the response, or the unverified path would become an account-enumeration oracle. This exact distinction was found and fixed by this project's own test-writing process (see `find_or_create_user_for_google_identity`'s docstring for the full 4-step decision order) before it ever shipped.
- **Linking is refused if the identity is already claimed by a different user** (`ExternalIdentityAlreadyLinkedError`, HTTP 409) — verified against a genuine two-user race in `tests/test_identity_repository_external_identities.py`.
- **Unlinking is refused if it would leave the account with no way to sign in at all** (`CannotUnlinkLastSignInMethodError`, HTTP 409) — no password *and* no other linked identity. An account with a password can always unlink Google; a Google-only account must set a password (or link a second identity, once a second provider exists) before it can remove its only one.
- **Suspended/deactivated accounts**: `user.status != "active"` is checked identically on the Google path as on password login — a suspended account cannot sign in via Google either, same generic rejection.
- **Migration**: `password_hash` was widened to nullable via a real Alembic migration (`a09ce853cb0d_google_signin_external_identities.py`) that only ever *loosens* a constraint — no existing row is touched, no data is migrated or backfilled, and the corresponding `downgrade()` refuses to run (raises rather than silently corrupting data) if any row already has a NULL password hash it can't re-tighten around.

### Sessions — reuses the existing session system, nothing new invented

A successful Google sign-in calls the *exact same* `_issue_tokens(...)` helper `POST /auth/login`/`POST /auth/register` already use — there is no separate "Google session" concept anywhere. That means every existing session property applies unchanged: a short-lived JWT access token (in-memory on the frontend, never persisted — see `frontend/src/store/localAuthStore.ts`), a rotating opaque refresh token in a `Secure`/`HttpOnly`/`SameSite` cookie scoped to `/auth`, reuse-detection revoking the whole session family on a replayed refresh token, and the existing idle/absolute lifetime settings (`Settings.access_token_expire_minutes`, refresh-token TTL). Signing in via Google rotates a fresh session exactly like a password login does — there is no "session carried over" concept between sign-in methods.

### Secret hygiene

- **`GOOGLE_OAUTH_CLIENT_ID` is explicitly public**, by Google's own design (it appears in the GIS JS snippet the browser executes) — served to the frontend via `GET /health`'s `google_client_id` field, the same "dynamic, sanitized capability endpoint" pattern this codebase already uses for `google_signin_enabled`, `voice_enabled`, etc. It deliberately does **not** follow the pre-existing `VITE_OIDC_CLIENT_ID` precedent (a build-time-baked frontend env var) — a build-time value can't be rotated without a rebuild/redeploy, while `/health` can reflect a `.env` change on the next process restart alone. This was a deliberate choice, not an oversight — see this project's own `AskUserQuestion` record from when this was built.
- **No new secret was introduced by this feature.** There is no `client_secret`, no signing key, no service-account JSON — the ID-token flow needs none of them. `security/redaction.py` was still extended (bare-JWT pattern, `client_secret`/`access_token`/`id_token`/`refresh_token` field names) as defense-in-depth for the general shape of credential this feature's *tokens* resemble, even though this app itself never stores one.
- `scripts/scan_frontend_build_for_secrets.py` (new, CI-wired — see "Frontend secret scanning" below) confirmed the built frontend bundle contains the Google **client ID** (expected, public) and no client secret, API key, or other credential pattern.

### Failure modes, all handled explicitly (never a raw error, never `[object Object]`)

`GoogleSignInButton.tsx` and the Sign In/Register pages handle, distinctly: the GIS script failing to load (network-blocked, ad blocker), the nonce fetch failing, the user dismissing the Google prompt (GIS itself no-ops silently — no error to show), an invalid/expired/failed server-side verification (the safe message from the table above), the verified-email-conflict case (409, actionable "sign in with your password" message), the unverified-email-collision case (generic 401, no account-existence disclosure), and Google's own outage (network error surfaced as a generic "try again" message, not a crash). `google_client_id`/`google_signin_enabled` being false or absent (feature not configured on this server) hides the button entirely rather than rendering a broken one.

### Google Cloud Console setup (operator-facing, not automatable from this codebase)

1. Create (or reuse) a project in [Google Cloud Console](https://console.cloud.google.com/), then **APIs & Services → OAuth consent screen** — configure the app name, support email, and (if restricting to an organization) the internal/external user type.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**, application type **Web application**.
3. Add every origin that will render the sign-in button under **Authorized JavaScript origins** — e.g. `http://localhost:5173` (Vite dev server), `http://localhost:8000` (the built app served by FastAPI), and each real deployment's origin. GIS's ID-token flow does not use a redirect URI the way the authorization-code flow does, so **Authorized redirect URIs** can typically be left empty for this flow.
4. Copy the generated **Client ID** into `GOOGLE_OAUTH_CLIENT_ID` in `.env`. There is no client secret to copy — this flow doesn't use one.
5. **Use a separate OAuth client per environment** (dev/staging/prod) — the same hygiene as this codebase's own existing "separate OIDC/local-auth client per environment" convention; an origin authorized for `localhost` should never also be authorized for a production domain.
6. If restricting sign-in to a Google Workspace organization, set `GOOGLE_OAUTH_ALLOWED_HOSTED_DOMAINS` to that domain (or comma-separated list) in `.env` — this is enforced from the token's signed `hd` claim server-side (see the verification table above), not just a UI-side filter.
7. **Rotation**: a Google OAuth client ID does not need periodic rotation the way a secret does (it's not sensitive), but if a client is ever suspected compromised or is being deprecated, create a new OAuth client in the same project, update `GOOGLE_OAUTH_CLIENT_ID`, and delete the old client from the Cloud Console once traffic has migrated.
8. **Staging live-sign-in checklist** (see "What's verified vs. not" below for why this matters): with a real client ID configured, load the sign-in page, click "Continue with Google," complete a real Google account consent, and confirm (a) a new user is created on first use, (b) signing out and back in with the same account returns the *same* user, (c) the account settings page shows the linked identity, and (d) unlinking then re-linking behaves as documented above. None of this has been exercised in this development environment — there is no real Google Cloud project or browser available here.

### Frontend secret scanning (`scripts/scan_frontend_build_for_secrets.py`)

Because a Google OAuth client ID is expected to appear in the built frontend bundle (public, by design) while a real secret (DB password, JWT signing key, any provider API key) must **never** appear there, this feature came with a new, CI-wired scanner distinct from the existing Git-history scanners (`gitleaks`, `detect-secrets`, both scoped to source, not build output):

- Scans `frontend/dist`'s compiled JS/CSS/HTML/source maps for known secret-shaped patterns (AWS keys, bearer tokens, JWT-shaped strings, Google API-key patterns, `client_secret=`-style fields, etc.) plus, optionally (`--check-configured-secrets`), exact-value matches against this deployment's own real configured secrets (`security.redaction.configured_secret_fingerprints`) — the deeper check a pattern-only scanner can't do.
- Wired into `.github/workflows/ci.yml`'s frontend job, right after `npm run build` — fails the build on any unexpected match, reporting only a redacted file/line/category, never the matched value itself.
- Verified clean against this project's own freshly-built `frontend/dist` in both modes (see the final report for the exact run).

### What's verified vs. not (read before deploying)

**Live-verified in this development environment** (a real local PostgreSQL identity database was available and used extensively, not mocked): the Alembic migration (applied, rolled back, re-applied against real Postgres), every identity-linking repository function (new-user creation, returning-user-same-`sub`, verified-email conflict, unverified-email-collision generic rejection, already-linked-to-another-user rejection, unlink-guard rejection — all exercised directly against real Postgres, then cleaned up), and `verify_google_id_token`'s rejection path making a **real network round-trip** to Google's own certificate-verification infrastructure to reject a forged token (confirmed via `TestClient` against the running app).

**Necessarily mocked** (no way to produce a token Google's own library will accept without a real Google account completing a real sign-in in a real browser): every backend HTTP test that exercises a *successful* verification (`tests/test_api_auth_google.py`) mocks `security.google_oidc.verify_google_id_token` at the boundary, returning a crafted `GoogleIdentityClaims` — the verification function's own internal logic is instead directly unit-tested against Google's real infrastructure (`tests/test_security_google_oidc.py`, live rejection of a forged token, described above).

**Not verified at all in this environment, and must not be assumed**: a real, browser-based, end-to-end Google sign-in (an actual user clicking the actual button, completing actual Google consent, receiving an actual signed token from actual Google infrastructure, and this app accepting it) has **not** been exercised — there is no real Google Cloud OAuth client, no real browser, and no real Google account available in this environment. Every piece up to and including real verification-library behavior against real Google infrastructure has been checked; the one missing link is a live human clicking through the flow in a real browser against a real configured `GOOGLE_OAUTH_CLIENT_ID`. Treat this feature as **implemented and defensively tested, but not proven end-to-end**, exactly the same honest posture this document already applies to the pre-existing frontend OIDC integration above — complete the "Staging live-sign-in checklist" above before relying on this in production.

## What this does not do (honestly scoped)

- **No token issuance.** This app never mints, signs, or refreshes a token — it is a relying party only.
- **No session management.** Every request is authenticated independently from its own bearer token; there is no server-side session store.
- **No user provisioning/SCIM.** User accounts, group membership, and role assignment all live in the identity provider — this app only reads whatever the IdP already put in the token.
- **JWKS discovery result is cached process-lifetime** (`security/oidc.py::_cached_jwks_url`/`_get_jwk_client`, both `lru_cache`-backed) — a JWKS URL change or IdP migration requires a process restart to take effect, the same tradeoff every other process-lifetime singleton in this codebase already has (`db.connection._cached_engine`, `agent.llm_client._get_ollama_client`).
- **The static-token mode still grants full ("admin") access to anything Step 3's RBAC layer protects** — a single shared secret has no natural sub-identity to scope down further; this is a deliberate, documented continuation of what the token already implicitly granted before RBAC existed (see `docs/AUTHORIZATION.md`), not a new privilege.
- **The frontend OIDC integration has not been verified against a real identity provider in this environment** — no live IdP/browser was available to click through the full interactive flow end-to-end (real login redirect, consent screen, silent-renew-via-iframe behavior across browsers, sign-out-at-IdP). Verified: `tsc --noEmit`, `oxlint`, and `npm run build` all pass; the code follows `oidc-client-ts`'s documented API and PKCE flow shape. Treat this as **implemented but not end-to-end verified** until it's been exercised against a real Auth0/Okta/Azure AD/Keycloak tenant.
- **Google sign-in's nonce store is process-local and in-memory** (`security/google_oidc.py`'s bounded `OrderedDict`), the same disclosed limitation `agent/rate_limit.py`'s sliding-window limiters already have — a multi-worker deployment would have one independent nonce store per worker. A nonce issued by one worker and redeemed against a different worker (behind a load balancer with no sticky sessions) would fail verification even for a legitimate sign-in. Single-process deployments (this app's primary documented posture, see `README.md`'s Limitations section) are unaffected; a multi-worker deployment enabling Google sign-in should either enable sticky sessions on `/auth/google/nonce`+`/auth/google`, or treat this as a known follow-up (a shared store, e.g. Redis, would be needed — not attempted here, consistent with this codebase's existing "no shared store exists" disclosure for rate limiting).
- **A real, browser-based Google sign-in has not been end-to-end verified** — see "What's verified vs. not" above.
