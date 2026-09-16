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

## What this does not do (honestly scoped)

- **No token issuance.** This app never mints, signs, or refreshes a token — it is a relying party only.
- **No session management.** Every request is authenticated independently from its own bearer token; there is no server-side session store.
- **No user provisioning/SCIM.** User accounts, group membership, and role assignment all live in the identity provider — this app only reads whatever the IdP already put in the token.
- **JWKS discovery result is cached process-lifetime** (`security/oidc.py::_cached_jwks_url`/`_get_jwk_client`, both `lru_cache`-backed) — a JWKS URL change or IdP migration requires a process restart to take effect, the same tradeoff every other process-lifetime singleton in this codebase already has (`db.connection._cached_engine`, `agent.llm_client._get_ollama_client`).
- **The static-token mode still grants full ("admin") access to anything Step 3's RBAC layer protects** — a single shared secret has no natural sub-identity to scope down further; this is a deliberate, documented continuation of what the token already implicitly granted before RBAC existed (see `docs/AUTHORIZATION.md`), not a new privilege.
