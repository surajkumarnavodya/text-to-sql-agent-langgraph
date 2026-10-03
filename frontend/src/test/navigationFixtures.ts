import type { Mock } from 'vitest'
import type { NavigationOut } from '@/lib/types'

/** Test fixtures for the server's navigation response (Prompt 32).
 *
 * `navigationFor(roles)` mirrors `security/navigation.py`'s persona grants, so a
 * component test can state "this caller is an analyst" and get exactly what
 * `GET /navigation` would return for it. The authoritative mapping is asserted
 * on the server in `tests/test_security_navigation.py` and
 * `tests/test_api_navigation.py`. If the two disagree, the server suite wins.
 */

const SCREEN_ORDER: { id: string; path: string; group: 'workspace' | 'review' | 'administration' }[] = [
  { id: 'chat', path: '/', group: 'workspace' },
  { id: 'knowledge_sources', path: '/knowledge-sources', group: 'workspace' },
  { id: 'media_search', path: '/media-search', group: 'workspace' },
  { id: 'recommendations', path: '/recommended-actions', group: 'review' },
  { id: 'db_onboarding', path: '/db-onboarding', group: 'review' },
  { id: 'semantic_review', path: '/semantic-review', group: 'review' },
  { id: 'tenant_admin', path: '/tenant-admin', group: 'administration' },
  { id: 'platform_admin', path: '/platform-admin', group: 'administration' },
]

export function navigationFor(roles: string[]): NavigationOut {
  const has = (...names: string[]) => names.some((name) => roles.includes(name))
  const baseAi = has('viewer', 'user', 'analyst', 'admin')
  const reviewer = has('analyst', 'admin', 'platform_admin')
  const adminish = has('admin', 'platform_admin')
  const tenantDashboard = adminish || has('auditor', 'manager')

  const screenAllowed: Record<string, boolean> = {
    chat: baseAi,
    knowledge_sources: baseAi,
    media_search: baseAi,
    recommendations: reviewer,
    db_onboarding: reviewer,
    semantic_review: reviewer,
    tenant_admin: tenantDashboard,
    platform_admin: has('platform_admin'),
  }

  return {
    items: SCREEN_ORDER.filter((screen) => screenAllowed[screen.id]).map(({ id, path, group }) => ({
      id,
      path,
      group,
    })),
    capabilities: {
      ask: baseAi,
      execute_sql: has('user', 'analyst', 'admin'),
      manage_documents: adminish,
      refresh_schema: adminish,
      manage_onboarding: adminish,
      review_onboarding: reviewer,
      manage_catalog: adminish,
      review_catalog: reviewer,
      review_recommendations: reviewer,
      manage_recommendations: adminish,
      manage_tenant_users: adminish,
      view_tenant_dashboard: tenantDashboard,
      platform_admin: has('platform_admin'),
    },
    roles,
    tenant_id: 'tenant-a',
  }
}

/** What the server returns for a signed-out or no-role caller. */
export const EMPTY_NAVIGATION: NavigationOut = {
  items: [],
  capabilities: {},
  roles: [],
  tenant_id: null,
}

/** Makes a mocked `getNavigation` answer for `roles` (or nothing, when `null`). */
export function serveNavigation(getNavigation: Mock, roles: string[] | null): void {
  getNavigation.mockResolvedValue(roles === null ? EMPTY_NAVIGATION : navigationFor(roles))
}
