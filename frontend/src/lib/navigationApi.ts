import { request } from './api'
import type { NavigationOut } from './types'

/** Thin wrappers around `/navigation` (`api/navigation.py`, Prompt 32).
 *
 * `getNavigation` is the single source of what this caller may see and do. The
 * server computes it from the same permission checks the real API routes make
 * (`security/navigation.py`), so this is the only input the screens and route
 * guards read. A role name is never looked at in the browser. */
export function getNavigation(): Promise<NavigationOut> {
  return request<NavigationOut>('/navigation')
}

/** Tells the server that a signed-in caller was turned away from a screen. Only
 * a real screen path is recorded there, and the response is always empty, so a
 * failure here is ignored: an audit ping must never break the page. */
export function reportAccessDenied(path: string): Promise<void> {
  return request<void>('/navigation/access-denied', {
    method: 'POST',
    body: JSON.stringify({ path }),
  }).then(
    () => undefined,
    () => undefined,
  )
}
