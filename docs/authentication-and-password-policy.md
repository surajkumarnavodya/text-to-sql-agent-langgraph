# Authentication: Display Name & Password Policy

This document covers the two authentication hardening changes made in this
pass — mandatory display names and a strengthened password policy — on top
of the pre-existing local-account system (`identity/`, see
`docs/AUTHENTICATION.md` for the full authentication picture, unchanged by
this pass beyond what's described here).

## Display name (mandatory at sign-up)

### What changed

- `identity.schemas.RegisterRequest.display_name`: was `str | None = None`,
  now `str = Field(..., min_length=1, max_length=200)` — a `field_validator`
  (`identity.display_name.validate_display_name`) enforces the real rules
  below.
- `frontend/src/pages/auth/Register.tsx`: the display-name field is now
  `required`, with a visible hint (`auth.localDisplayNameHint`) and no
  more "(optional)" wording.
- `identity.models.User.display_name` **stays nullable** at the database
  level — an account created before this change (or one otherwise missing
  a name) must not break; see "Existing users" below.
- A new `PATCH /auth/me` endpoint (`identity.schemas.UpdateProfileRequest`)
  lets a user set/change their own display name — used both for an
  ordinary profile edit and the profile-completion prompt.

### Validation rules (`identity/display_name.py::validate_display_name`)

1. Unicode-normalized (`unicodedata.normalize("NFKC", ...)`) before any
   other check — folds visually-confusable/compatibility variants (e.g.
   full-width Latin letters) into their standard form first.
2. Trimmed, and internal whitespace runs collapsed to a single space.
3. Rejected: empty, whitespace-only.
4. Rejected: control characters (`\x00`-`\x1f`, `\x7f`-`\x9f`) — a pasted
   name can never inject a newline, null byte, or terminal escape sequence.
5. Rejected: `<`/`>` — a blunt but effective guard against HTML/script
   injection via a display name. (This app also never renders a display
   name as raw HTML — React's default JSX text interpolation already
   escapes it — so this is defense-in-depth, not the only thing standing
   between an attacker and a stored-XSS bug.)
6. Length: 2-100 characters (`MIN_LENGTH`/`MAX_LENGTH` in
   `identity/display_name.py`), checked after normalization/collapsing.

**Never used as an identity key** — `users.email`/`users.id` remain the
only identifiers any lookup or foreign key is built from anywhere in this
codebase; `display_name` is purely a rendered label. **No uniqueness is
enforced** — this codebase's `RegisterRequest` never required it before
this change either, and nothing in the existing application depends on
display names being distinct.

### Existing users without a display name

`display_name` stays nullable in the database specifically so an account
created before this change (or an admin-provisioned one) never breaks —
login, `/ask`, and every other route work identically regardless of
whether `display_name` is set.

`identity.schemas.UserOut.needs_profile_completion` (`user.display_name is
None`) drives a **non-blocking** frontend prompt
(`frontend/src/components/auth/ProfileCompletionBanner.tsx`, rendered by
`AuthGate.tsx` above the app, never gating access to it) offering to set
one via `PATCH /auth/me`. It's dismissible and reappears on the next
sign-in until a name is actually set — there is no permanent "don't ask
again," since a stale missing name is worth a gentle, low-friction nudge
each session rather than a one-time interruption that's easy to forget.

## Password policy

### What changed

Before this pass, every password-accepting endpoint
(`register`/`change-password`/`reset-password`) only checked
`len(password) < Settings.password_min_length` (default 12) — no common-password
check, no pattern detection, no identity-fragment check, and no explicit
maximum length. All three now call
`identity.password_policy.validate_password_strength`, which returns a
**list** of violation messages (not just the first one found) so a UI can
show everything wrong in one round trip.

### Rules (`identity/password_policy.py`)

| Rule | Detail |
|---|---|
| Minimum length | `Settings.password_min_length`, default 12 |
| Maximum length | `Settings.password_max_length`, default 64 — **rejected outright, never silently truncated** |
| Spaces | Explicitly allowed (passphrases) |
| Common passwords | A small, hand-picked list of drastically overrepresented breach-corpus passwords (`_COMMON_PASSWORDS`), plus a "common base word + trailing digits" check (`_has_common_base_word`) that catches the `Password123456`/`Qwerty123` family without needing one list entry per possible digit suffix |
| Sequential digit runs | Any 6+ digit ascending/descending run anywhere in the password (`_has_sequential_digit_run`) — an unbounded family by construction, not a fixed string list |
| Low-variety repeats | A short (1-3 char) unit repeated to fill most of the password (e.g. `abcabcabcabc`) |
| Keyboard walks | `qwerty`, `asdfgh`, `zxcvbn`, `qazwsx`, `1qaz2wsx` as substrings |
| Contains email | The email's local-part (before `@`), if 4+ alphanumeric characters, as a substring |
| Contains display name | Same substring check |
| Contains username | Same substring check |

A short (<4 char) coincidental overlap is never flagged — e.g. a 2-letter
username matching two letters buried in an otherwise-strong, unrelated
passphrase is not a real signal of a weak password derived from that
username.

### Not implemented (named, not silently skipped)

- **Breached-password checking against a live database (e.g.
  HaveIBeenPwned's k-anonymity API)** — "reject breached passwords where
  feasible" per this feature's own spec; a live network call to a
  third-party service is a real, disclosed exception to this project's
  local-first posture (the same class of tradeoff
  `docs/vector-retrieval-design.md`/`SECURITY.md`'s moderation-gate
  section already name explicitly for their own external-API
  dependencies) — deliberately not added in this pass. The common-password
  list above is the offline approximation.
- **Periodic forced password rotation** — not implemented, per this
  feature's own explicit instruction not to add one "unless required by
  policy or compromise."

### Hashing, storage, logging

Unchanged — `identity/security.py::hash_password` (Argon2id, via
`argon2-cffi`, OWASP-tuned defaults) was already correct before this pass
and needed no changes. A password is never logged (verified: no call site
in `identity/`/`api/identity_auth.py` passes a raw password to `logging`),
never returned in any response body (`identity.schemas`'s response models
have no password field), and never stored in plaintext anywhere.

### Rate limiting / brute-force protection

Already in place before this pass and unchanged:
`api/identity_auth.py`'s per-client-IP limiters for login/register/refresh/
password-reset (`agent.rate_limit.BoundedLimiterCache`), plus
account-level lockout after `Settings.max_login_attempts` consecutive
failures (`identity.repositories.users.record_login_failure`).

### Frontend mirror (`frontend/src/lib/passwordPolicy.ts`)

A client-side copy of the same rule shapes, for UX only —
`Register.tsx` calls it before submitting so a weak password is caught
before a round trip. **The backend is the sole authority**: a bypass of
the client-side check (disabled JavaScript, a direct API call) changes
nothing, since `validate_password_strength` runs unconditionally on every
password-accepting endpoint regardless of what the client already checked.

### Tests

`tests/test_identity_password_policy.py` (17 tests): length bounds
(including "not truncated" for an over-length password), passphrases with
spaces, common/patterned passwords, sequential digits, keyboard walks,
identity-fragment containment (including the "short overlap is not a
false positive" case), and Unicode passwords. `tests/test_api_identity_auth.py`
exercises the same policy at the HTTP layer via the existing
register/reset/change-password test classes.
