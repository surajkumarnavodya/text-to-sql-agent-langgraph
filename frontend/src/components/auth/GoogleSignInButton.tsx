import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { fetchGoogleSigninNonce } from '@/lib/identityApi'
import { loadGoogleIdentityScript } from '@/lib/googleIdentity'

/** Renders Google Identity Services' own "Continue with Google" button and
 * hands the resulting ID token to `onCredential` -- the raw credential is
 * never stored, decoded, or trusted by this component; it exists on the
 * client only long enough to be handed off, matching this app's own "the
 * backend is the trust boundary" design (see `security/google_oidc.py`'s
 * module docstring).
 *
 * One component, two call sites: the sign-in/sign-up screen
 * (`onCredential` -> `useLocalAuthStore().loginWithGoogle`) and the
 * account-settings "connected accounts" panel (`onCredential` ->
 * `linkGoogleAccount`) -- both need the identical button/nonce/script
 * mechanics, only what happens with the resulting credential differs.
 *
 * Renders nothing (not even an error) if the script fails to load or
 * `clientId` isn't actually configured -- `disabled` lets a caller that
 * already knows the capability is off skip mounting this at all, but this
 * component fails closed on its own too, since a network hiccup loading
 * Google's own script is a real, non-hypothetical failure mode.
 */
export function GoogleSignInButton({
  clientId,
  onCredential,
  disabled = false,
}: {
  clientId: string
  onCredential: (credential: string) => void | Promise<void>
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const containerRef = useRef<HTMLDivElement>(null)
  const [loadError, setLoadError] = useState(false)
  // A ref, not a dependency, so a re-render from the *caller* passing a new
  // function identity for onCredential (e.g. an inline arrow function)
  // never re-runs the effect below and re-initializes/duplicates the
  // button -- only clientId/disabled changing should do that.
  const onCredentialRef = useRef(onCredential)
  useEffect(() => {
    onCredentialRef.current = onCredential
  })

  useEffect(() => {
    if (disabled || !clientId) return
    let cancelled = false

    async function setup() {
      try {
        await loadGoogleIdentityScript()
        if (cancelled) return
        // One nonce per button mount, embedded into the ID token Google
        // signs -- see security/google_oidc.py's own docstring for what
        // this protects against. If the user waits past its TTL before
        // clicking, the backend rejects the resulting sign-in cleanly
        // (the same "verification failed" error as any other rejection);
        // reloading the page fetches a fresh one.
        const { nonce } = await fetchGoogleSigninNonce()
        if (cancelled || !window.google?.accounts?.id || !containerRef.current) return

        window.google.accounts.id.initialize({
          client_id: clientId,
          nonce,
          callback: (response) => {
            void onCredentialRef.current(response.credential)
          },
        })
        window.google.accounts.id.renderButton(containerRef.current, {
          theme: 'outline',
          size: 'large',
          text: 'continue_with',
          shape: 'rectangular',
          width: 320,
        })
      } catch {
        if (!cancelled) setLoadError(true)
      }
    }

    void setup()
    return () => {
      cancelled = true
    }
  }, [clientId, disabled])

  if (disabled || !clientId) return null
  if (loadError) {
    return (
      <p className="text-xs text-[var(--muted-foreground)]" role="status">
        {t('auth.googleUnavailable')}
      </p>
    )
  }
  return <div ref={containerRef} aria-label={t('auth.continueWithGoogle')} />
}
