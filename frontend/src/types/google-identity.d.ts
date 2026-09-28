// Minimal ambient types for Google Identity Services' "Sign In With
// Google" button (`https://accounts.google.com/gsi/client`) -- not part
// of any npm @types package; hand-written against Google's own documented
// shape (developers.google.com/identity/gsi/web/reference/js-reference),
// scoped just deeply enough for `GoogleSignInButton.tsx`'s actual usage.
// Mirrors `types/speech-recognition.d.ts`'s exact convention for the same
// reason: a real, narrow, non-standard browser/vendor API this app talks
// to directly rather than through a wrapper library.

interface GoogleCredentialResponse {
  /** The signed Google ID token (JWT) -- sent directly to this app's own
   * backend for verification (`POST /auth/google`), never decoded or
   * trusted client-side. */
  credential: string
  /** How the credential was obtained -- not used by this app's own logic,
   * present for completeness with Google's documented shape. */
  select_by?: string
}

interface GoogleIdentityInitializeConfig {
  client_id: string
  callback: (response: GoogleCredentialResponse) => void
  /** Embedded in the resulting ID token's own signed `nonce` claim --
   * verified server-side against a nonce this app itself issued
   * (`GET /auth/google/nonce`, `security/google_oidc.py`). */
  nonce?: string
  /** This app never uses Google's automatic One Tap re-prompt (a
   * standing UX decision -- the explicit button is the only entry point),
   * so this is always left at its default (unset/false) rather than
   * enabled here. */
  auto_select?: boolean
  ux_mode?: 'popup' | 'redirect'
}

interface GoogleIdentityButtonOptions {
  type?: 'standard' | 'icon'
  theme?: 'outline' | 'filled_blue' | 'filled_black'
  size?: 'large' | 'medium' | 'small'
  text?: 'signin_with' | 'signup_with' | 'continue_with' | 'signin'
  shape?: 'rectangular' | 'pill' | 'circle' | 'square'
  logo_alignment?: 'left' | 'center'
  width?: string | number
  locale?: string
}

interface GoogleIdentityAccountsId {
  initialize: (config: GoogleIdentityInitializeConfig) => void
  renderButton: (parent: HTMLElement, options: GoogleIdentityButtonOptions) => void
  disableAutoSelect: () => void
  cancel: () => void
}

interface GoogleIdentityNamespace {
  accounts: { id: GoogleIdentityAccountsId }
}

interface Window {
  google?: GoogleIdentityNamespace
}
