"""Password hashing, locally-issued access JWTs, and opaque refresh tokens.

Three independent concerns, each deliberately simple and using what's
already installed or the modern, actively-maintained choice for the job --
see this module's own functions for why each library was picked.

## Password hashing (Argon2id)

`argon2-cffi` (a new dependency -- `bcrypt`/`passlib` were both absent from
this codebase and are either an older KDF or an unmaintained wrapper
library respectively) implements Argon2id, this feature's own preferred
algorithm, directly -- no vendor SDK, matching this codebase's general
"call the real thing directly, no unnecessary wrapper" convention
(`search/web_search.py`, `moderation/provider.py`). `argon2.PasswordHasher`'s
defaults are already tuned to OWASP-recommended parameters; never override
them without re-reading argon2-cffi's own tuning guidance first.

## Access tokens (locally-issued JWTs)

Signed with `pyjwt[crypto]`, already a direct dependency of
`security/oidc.py` (which only ever *validates* externally-issued tokens --
this is the first place in this codebase that *issues* one). `HS256`
(shared secret, `Settings.jwt_secret_key`) is the default for zero-friction
local dev; `RS256`/`ES256` (asymmetric, `Settings.jwt_private_key_path`/
`jwt_public_key_path`) are the production-grade options -- both already
supported by the same installed library, no new dependency either way.
`config.settings.Settings._validate_local_auth_signing_material` already
guarantees whichever algorithm is configured has its required signing
material present before this module is ever reached.

A token issued here is recognized as "locally issued" by `api/auth.py`
via its `iss` claim (`Settings.jwt_issuer`) *before* attempting full
validation -- see `looks_like_local_token` below -- so a request bearing an
externally-issued OIDC token never wastes a signature-verification attempt
against the wrong key/algorithm (and vice versa).

## Refresh tokens (opaque, hashed at rest)

Deliberately **not** a second JWT -- a high-entropy random string
(`secrets.token_urlsafe`), matching this feature's own "never store raw
refresh tokens in the database" requirement in the simplest way that
satisfies it: the database only ever stores `hash_refresh_token(raw)`
(SHA-256), and rotation/reuse-detection is a single indexed lookup by that
hash (`identity/repositories/sessions.py`), needing no JWT parsing/
signature-verification machinery for what is, underneath, just an opaque
session-lookup key.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from config.settings import Settings
from identity.exceptions import LocalTokenValidationError

# A single process-wide PasswordHasher, using argon2-cffi's own tuned
# defaults (OWASP-recommended time/memory cost as of this library's current
# release) -- never constructed per-call, since building one has a small
# but real fixed cost independent of hashing/verifying itself.
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hashes `password` with Argon2id. Never raises for a normal-length
    password; the encoded hash string itself carries the algorithm
    parameters, so a future `PasswordHasher()` tuning change can still
    verify an old hash correctly (`argon2-cffi`'s own forward-compatible
    encoded-hash format)."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True if `password` matches `password_hash`, False otherwise --
    never raises. `argon2.PasswordHasher.verify` is itself timing-safe
    (constant-time digest comparison internally); this wrapper only adds
    "return False instead of an exception" so every call site gets a plain
    boolean rather than needing its own try/except for the mismatch case.
    """
    try:
        _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


@lru_cache(maxsize=4)
def _load_key_file(path: str) -> bytes:
    """Process-lifetime cache of a PEM key file's bytes, keyed on path --
    same "read once, reuse for the life of the process" posture as
    `security.oidc._get_jwk_client`. A key-file change requires a process
    restart to take effect, consistent with every other such cache in this
    codebase."""
    return Path(path).read_bytes()


def _signing_key(settings: Settings) -> str | bytes:
    if settings.jwt_algorithm == "HS256":
        assert settings.jwt_secret_key is not None  # guaranteed by Settings' own validator
        return settings.jwt_secret_key.get_secret_value()
    assert settings.jwt_private_key_path is not None  # guaranteed by Settings' own validator
    return _load_key_file(str(settings.jwt_private_key_path))


def _verification_key(settings: Settings) -> str | bytes:
    if settings.jwt_algorithm == "HS256":
        assert settings.jwt_secret_key is not None
        return settings.jwt_secret_key.get_secret_value()
    assert settings.jwt_public_key_path is not None
    return _load_key_file(str(settings.jwt_public_key_path))


def create_access_token(subject: str, roles: tuple[str, ...], settings: Settings) -> str:
    """Issues a short-lived access JWT for `subject` (the user's `id`, as a
    string) carrying `roles` -- deliberately only the base role names
    (`viewer`/`user`/`analyst`/`admin`/...), never the granular
    `identity.rbac.Permission` codes (see that module's own docstring for
    why: permissions are re-derived from the database per request instead,
    so a revoked permission takes effect immediately rather than only
    after this token expires).

    Claims: `sub`, `roles`, `token_type="access"`, `iat`, `exp` (from
    `Settings.access_token_expire_minutes`), `jti` (a fresh UUID per
    token), `iss`/`aud` (`Settings.jwt_issuer`/`jwt_audience`) -- exactly
    this feature's own required claim set, deliberately excluding anything
    that could be sensitive (no password, no email, no full identity
    record).
    """
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "roles": list(roles),
        "token_type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
        "jti": str(uuid.uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(claims, _signing_key(settings), algorithm=settings.jwt_algorithm)


@dataclass(frozen=True)
class LocalTokenClaims:
    """Decoded, fully-validated claims from a locally-issued access token
    -- what `api/auth.py` turns into a `security.oidc.AuthIdentity` with
    `mode="local"`."""

    subject: str
    roles: tuple[str, ...]


def looks_like_local_token(token: str, settings: Settings) -> bool:
    """Cheaply checks whether `token`'s (unverified) `iss` claim matches
    `Settings.jwt_issuer`, *before* attempting full signature verification.

    This is what lets `api/auth.py::verify_api_key` try local-token
    validation first without wasting a doomed verification attempt (wrong
    key, wrong algorithm) against a token that's actually an externally-
    issued OIDC token or a malformed value -- and, symmetrically, lets an
    OIDC/static-token request skip local validation entirely. Never raises:
    any parse failure (malformed token, missing claim) means "no, this
    doesn't look like one of ours."

    Explicitly does **not** verify the signature here (`options={
    "verify_signature": False, ...}`) -- that would defeat the entire
    point of checking `iss` first (a token forged with a false `iss` claim
    is still caught by `validate_local_token`'s real, signature-verifying
    decode; this function only ever decides which validation path to
    attempt, never grants access on its own).
    """
    try:
        unverified = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError:
        return False
    return unverified.get("iss") == settings.jwt_issuer


def validate_local_token(token: str, settings: Settings) -> LocalTokenClaims:
    """Fully validates a locally-issued access JWT -- signature (against
    `settings.jwt_algorithm`'s own key material, never the token's own
    `alg` header), issuer, audience, expiration, and `token_type=="access"`
    (so a refresh token -- which isn't even a JWT, see this module's own
    docstring -- or some other token shape can never be mistaken for one).

    Raises:
        LocalTokenValidationError: on any validation failure. Always a
            short, generic message -- never the underlying `PyJWTError`'s
            raw text, which could echo back attacker-controlled
            header/claim values (the same principle
            `security.oidc.TokenValidationError` already applies).
    """
    try:
        claims = jwt.decode(
            token,
            _verification_key(settings),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "token_type"]},
        )
    except jwt.PyJWTError as exc:
        raise LocalTokenValidationError(f"Local JWT validation failed: {exc}") from exc

    if claims.get("token_type") != "access":
        raise LocalTokenValidationError("Token is not an access token.")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise LocalTokenValidationError("Token is missing a subject claim.")

    roles = claims.get("roles")
    role_tuple = tuple(str(r) for r in roles) if isinstance(roles, list) else ()
    return LocalTokenClaims(subject=subject, roles=role_tuple)


def generate_refresh_token() -> str:
    """A fresh, high-entropy opaque refresh token -- 48 random bytes,
    URL-safe base64-encoded (`secrets.token_urlsafe`, the standard-library
    CSPRNG-backed choice for exactly this). Never a JWT -- see this
    module's own docstring."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw_token: str) -> str:
    """SHA-256 hex digest of a raw refresh token -- what's actually stored
    in `identity.models.AuthSession.refresh_token_hash`. Plain SHA-256 (not
    Argon2id) is the right tool here, not a downgrade: a refresh token is
    already a high-entropy random value, not a human-memorable secret an
    attacker could feasibly dictionary/brute-force offline -- what this
    hash defends against is a database read (backup, replication lag,
    misconfigured access) leaking a value directly usable to impersonate a
    session, not an offline guessing attack, so a fast, collision-resistant
    hash is the correct, standard choice (the same reasoning
    `identity.models.PasswordResetToken`/`EmailVerificationToken` apply to
    their own tokens).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
