/** Loads Google Identity Services' script exactly once per page load,
 * regardless of how many `GoogleSignInButton` instances mount (the sign-in
 * form and the account-settings "link Google" panel can both render one).
 * Cached as a shared promise -- a second/third call while the first is
 * still loading awaits the same in-flight request rather than injecting a
 * duplicate `<script>` tag. */

let scriptLoadPromise: Promise<void> | null = null

const GIS_SCRIPT_SRC = 'https://accounts.google.com/gsi/client'

export function loadGoogleIdentityScript(): Promise<void> {
  if (typeof window === 'undefined') {
    return Promise.reject(new Error('Google Identity Services requires a browser environment.'))
  }
  if (window.google?.accounts?.id) return Promise.resolve()
  if (scriptLoadPromise) return scriptLoadPromise

  scriptLoadPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${GIS_SCRIPT_SRC}"]`)
    if (existing) {
      existing.addEventListener('load', () => resolve())
      existing.addEventListener('error', () =>
        reject(new Error('Failed to load Google Identity Services.')),
      )
      return
    }
    const script = document.createElement('script')
    script.src = GIS_SCRIPT_SRC
    script.async = true
    script.defer = true
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('Failed to load Google Identity Services.'))
    document.head.appendChild(script)
  })
  return scriptLoadPromise
}
