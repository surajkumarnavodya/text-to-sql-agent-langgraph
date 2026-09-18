/** Client-side mirror of `identity/password_policy.py` -- UX only, not the
 * authority. The backend (`validate_password_strength`, called from
 * `POST /auth/register`/`change-password`/`reset-password`) is what
 * actually gates account creation/password changes; a bypass of this
 * client-side check (disabled JS, a direct API call) changes nothing
 * server-side. Kept intentionally simple and close to the backend's own
 * rule set -- see that module's docstring for the full rationale of each
 * rule -- so the two never drift far apart, even though this file is not
 * generated from it.
 */

const MIN_LENGTH = 12
const MAX_LENGTH = 64

const COMMON_PASSWORDS = new Set([
  'password',
  'password1',
  'password123',
  'password1234',
  'password12345',
  '12345678',
  '123456789',
  '1234567890',
  'qwerty123',
  'qwertyuiop',
  'letmein123',
  'welcome123',
  'admin12345',
  'iloveyou123',
])

const COMMON_BASE_WORDS = new Set([
  'password',
  'qwerty',
  'admin',
  'welcome',
  'letmein',
  'dragon',
  'master',
  'superman',
  'batman',
  'iloveyou',
  'sunshine',
  'princess',
  'football',
  'monkey',
])

const KEYBOARD_WALKS = ['qwerty', 'asdfgh', 'zxcvbn', 'qazwsx', '1qaz2wsx']

function stripNonAlnumLower(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '')
}

function hasSequentialDigitRun(stripped: string, minRun = 6): boolean {
  const runs = stripped.match(/\d+/g) ?? []
  for (const run of runs) {
    if (run.length < minRun) continue
    let ascending = true
    let descending = true
    for (let i = 0; i < run.length - 1; i += 1) {
      const delta = Number(run[i + 1]) - Number(run[i])
      if (delta !== 1) ascending = false
      if (delta !== -1) descending = false
    }
    if (ascending || descending) return true
  }
  return false
}

function containsIdentityFragment(passwordLower: string, fragment: string | undefined, minLen = 4): boolean {
  if (!fragment) return false
  const normalized = stripNonAlnumLower(fragment)
  if (normalized.length < minLen) return false
  return stripNonAlnumLower(passwordLower).includes(normalized)
}

export interface PasswordPolicyContext {
  email?: string
  displayName?: string
}

/** Returns a list of violation messages -- empty means the password
 * clears every client-side check (the backend still re-validates). */
export function validatePasswordStrength(password: string, context: PasswordPolicyContext = {}): string[] {
  const violations: string[] = []
  const length = password.length

  if (length < MIN_LENGTH) violations.push(`Password must be at least ${MIN_LENGTH} characters.`)
  if (length > MAX_LENGTH) violations.push(`Password must be at most ${MAX_LENGTH} characters.`)

  const lowered = password.toLowerCase()
  const stripped = stripNonAlnumLower(password)
  const withoutTrailingDigits = stripped.replace(/\d+$/, '')

  if (
    COMMON_PASSWORDS.has(lowered) ||
    COMMON_PASSWORDS.has(stripped) ||
    COMMON_BASE_WORDS.has(stripped) ||
    COMMON_BASE_WORDS.has(withoutTrailingDigits)
  ) {
    violations.push('This password is too common. Please choose a less predictable one.')
  }
  if (hasSequentialDigitRun(stripped)) {
    violations.push('Password must not contain a long sequential digit run (e.g. 123456).')
  }
  if (/^(.{1,3})\1{3,}$/.test(stripped) && stripped.length >= 8) {
    violations.push('Password must not be a short pattern repeated over and over.')
  }
  if (KEYBOARD_WALKS.some((walk) => stripped.includes(walk))) {
    violations.push('Password must not contain a common keyboard pattern (e.g. qwerty).')
  }

  const emailLocalPart = context.email?.includes('@') ? context.email.split('@')[0] : context.email
  if (containsIdentityFragment(lowered, emailLocalPart)) {
    violations.push('Password must not contain your email address.')
  }
  if (containsIdentityFragment(lowered, context.displayName)) {
    violations.push('Password must not contain your display name.')
  }

  return violations
}
