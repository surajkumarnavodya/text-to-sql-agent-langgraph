import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  useAssignTenantUserRole,
  useCapabilities,
  useRefreshTenantDatabases,
  useRemoveTenantUserRole,
  useTenantAssignableRoles,
  useTenantAuditEvents,
  useTenantDatabases,
  useTenantEvaluationResults,
  useTenantGoldenQuestions,
  useTenantPendingReviews,
  useTenantPerformanceMetrics,
  useTenantProfile,
  useTenantRecommendationQualityMetrics,
  useTenantRecommendations,
  useTenantSemanticCatalogStatus,
  useTenantUsers,
} from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import type { PlatformUserOut, SchemaRefreshResponse } from '@/lib/types'

/** The tenant/client admin dashboard -- Prompt 29, the tenant-scoped
 * counterpart to `PlatformAdmin.tsx`. Introduces **no new permission or
 * role** -- a tenant admin is exactly what the existing, tenant-scoped
 * `admin` role already means. Every query this page makes is answered
 * entirely server-side from the caller's own account; there is no
 * `tenantId` anywhere in this file to pass, override, or leak (see
 * `api/tenant_admin.py`'s own docstring for the structural guarantee
 * behind that).
 *
 * **Reused rather than duplicated**: Users/Roles/Databases/Audit reuse
 * the identical `PlatformUserOut`/`RoleOut`/`DatabaseStatusOut`/
 * `SecurityEventOut` shapes `PlatformAdmin.tsx` already uses (only the
 * query scope differs, enforced server-side). AI usage and
 * recommendations are **not** served by any new `/tenant-admin/*`
 * route at all -- `GET /metrics/performance` and `GET /recommendations`
 * were already tenant-scoped server-side before this prompt, so this
 * page calls them directly. Managing an onboarding job's own lifecycle
 * (discover/publish/cancel/retry) is **not** re-implemented here either
 * -- the Pending Reviews/Golden Questions/Evaluation tabs are read-only
 * summaries that link to the existing `/db-onboarding` page for the
 * actual review workflow, the same precedent `SemanticReview.tsx`
 * already set for the identical reason.
 *
 * **Visibility is UX-only, two tiers**: viewing is available to
 * `admin`/`auditor`/`manager` (the same roles `identity.rbac
 * .Permission.ADMIN_DASHBOARD_READ` already grants); only `admin` can
 * act (assign/remove a role, trigger a schema refresh) -- mirroring
 * `USERS_ASSIGN_ROLES`/`ONBOARDING_MANAGE`'s own real, admin-only grant.
 * The real enforcement is entirely server-side regardless.
 */
export function TenantAdmin() {
  // Server-decided (Prompt 32): see `useCapabilities`. `manage_tenant_users` is
  // the role-assignment grant (USERS_ASSIGN_ROLES), which only the tenant admin
  // holds among the dashboard roles.
  const capabilities = useCapabilities()
  const canManage = Boolean(capabilities.manage_tenant_users)
  const canView = Boolean(capabilities.view_tenant_dashboard)

  if (!canView) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-[var(--muted-foreground)]">
        This page requires the admin, auditor, or manager role.
      </div>
    )
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <h1 className="text-xl font-bold">🏢 Tenant Admin</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Manage your own environment -- nothing shown here reaches another tenant or
          platform-wide configuration.
        </p>

        <Tabs defaultValue="profile">
          <TabsList className="mt-6 flex-wrap">
            <TabsTrigger value="profile">Profile</TabsTrigger>
            <TabsTrigger value="databases">Databases</TabsTrigger>
            <TabsTrigger value="users">Users</TabsTrigger>
            <TabsTrigger value="roles">Roles & Permissions</TabsTrigger>
            <TabsTrigger value="catalog">Semantic Catalog</TabsTrigger>
            <TabsTrigger value="reviews">Pending Reviews</TabsTrigger>
            <TabsTrigger value="golden">Golden Questions</TabsTrigger>
            <TabsTrigger value="evaluation">Evaluation</TabsTrigger>
            <TabsTrigger value="usage">AI Usage</TabsTrigger>
            <TabsTrigger value="recommendations">Recommendations</TabsTrigger>
            <TabsTrigger value="audit">Audit</TabsTrigger>
          </TabsList>

          <TabsContent value="profile">
            <ProfileSection />
          </TabsContent>
          <TabsContent value="databases">
            <DatabasesSection canManage={canManage} />
          </TabsContent>
          <TabsContent value="users">
            <UsersSection canManage={canManage} />
          </TabsContent>
          <TabsContent value="roles">
            <RolesSection />
          </TabsContent>
          <TabsContent value="catalog">
            <SemanticCatalogSection />
          </TabsContent>
          <TabsContent value="reviews">
            <PendingReviewsSection />
          </TabsContent>
          <TabsContent value="golden">
            <GoldenQuestionsSection />
          </TabsContent>
          <TabsContent value="evaluation">
            <EvaluationSection />
          </TabsContent>
          <TabsContent value="usage">
            <UsageSection />
          </TabsContent>
          <TabsContent value="recommendations">
            <RecommendationsSection />
          </TabsContent>
          <TabsContent value="audit">
            <AuditSection />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

function ProfileSection() {
  const profile = useTenantProfile()
  if (!profile.data) return null
  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle className="text-sm">{profile.data.name}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <div className="font-mono text-xs text-[var(--muted-foreground)]">{profile.data.id}</div>
        <div className="flex items-center gap-2">
          <Badge tone={profile.data.status === 'active' ? 'success' : 'danger'}>
            {profile.data.status}
          </Badge>
          <span className="text-[var(--muted-foreground)]">{profile.data.user_count} users</span>
        </div>
      </CardContent>
    </Card>
  )
}

function DatabasesSection({ canManage }: { canManage: boolean }) {
  const databases = useTenantDatabases()
  const refresh = useRefreshTenantDatabases()
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<SchemaRefreshResponse | null>(null)

  const handleRefresh = async () => {
    setError(null)
    try {
      setResult(await refresh.mutateAsync())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not refresh schema.')
    }
  }

  return (
    <div className="flex flex-col gap-3 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Your databases ({databases.data?.length ?? 0})</h2>
        {canManage && (
          <Button size="sm" disabled={refresh.isPending} onClick={handleRefresh}>
            {refresh.isPending ? 'Refreshing…' : 'Refresh schema'}
          </Button>
        )}
      </div>
      {error && <p className="text-sm text-[var(--danger)]">{error}</p>}
      {result && (
        <ul className="flex flex-col gap-1 text-sm" aria-live="polite">
          {result.databases.map((entry) => (
            <li key={entry.database} className={entry.error ? 'text-[var(--danger)]' : 'text-[var(--muted-foreground)]'}>
              {entry.database}: {entry.error ?? `${entry.table_count} table(s) indexed`}
            </li>
          ))}
        </ul>
      )}
      {databases.data?.map((database) => (
        <Card key={database.name}>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">{database.name}</CardTitle>
            <Badge tone={database.healthy ? 'success' : 'danger'}>
              {database.healthy ? 'reachable' : 'unreachable'}
            </Badge>
          </CardHeader>
          <CardContent className="text-sm text-[var(--muted-foreground)]">
            {database.db_type}
            {database.db_host ? ` · ${database.db_host}` : ''}
            {database.db_name ? ` / ${database.db_name}` : ''}
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

function UsersSection({ canManage }: { canManage: boolean }) {
  const users = useTenantUsers()
  return (
    <div className="flex flex-col gap-3 pt-4">
      <h2 className="text-sm font-semibold">Users ({users.data?.length ?? 0})</h2>
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-sm">
          <thead className="bg-[var(--muted)] text-xs uppercase text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2 font-medium">Email</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Roles</th>
              {canManage && <th className="px-3 py-2 font-medium">Manage</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {users.data?.map((user) => (
              <UserRow key={user.id} user={user} canManage={canManage} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function UserRow({ user, canManage }: { user: PlatformUserOut; canManage: boolean }) {
  const assignRole = useAssignTenantUserRole(user.id)
  const removeRole = useRemoveTenantUserRole(user.id)
  const [newRole, setNewRole] = useState('')

  const handleAssign = () => {
    if (!newRole) return
    void assignRole.mutateAsync({ role_name: newRole })
    setNewRole('')
  }

  return (
    <tr>
      <td className="px-3 py-2">
        <div>{user.email}</div>
        <div className="text-xs text-[var(--muted-foreground)]">{user.display_name}</div>
      </td>
      <td className="px-3 py-2">
        <Badge tone={user.status === 'active' ? 'success' : 'neutral'}>{user.status}</Badge>
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {user.roles.map((role) => (
            <Badge key={role} tone="neutral">
              {role}
              {canManage && (
                <button
                  className="ml-1 text-[var(--muted-foreground)] hover:text-[var(--danger)]"
                  onClick={() => removeRole.mutate(role)}
                  aria-label={`Remove ${role} role`}
                >
                  ×
                </button>
              )}
            </Badge>
          ))}
        </div>
      </td>
      {canManage && (
        <td className="px-3 py-2">
          <div className="flex gap-1.5">
            <input
              value={newRole}
              onChange={(event) => setNewRole(event.target.value)}
              placeholder="role name"
              className="w-28 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 py-1 text-xs"
            />
            <Button size="sm" variant="secondary" disabled={assignRole.isPending} onClick={handleAssign}>
              Add
            </Button>
          </div>
        </td>
      )}
    </tr>
  )
}

function RolesSection() {
  const roles = useTenantAssignableRoles()
  return (
    <div className="flex flex-col gap-3 pt-4">
      {roles.data?.map((role) => (
        <Card key={role.name}>
          <CardHeader>
            <CardTitle className="text-sm">{role.name}</CardTitle>
          </CardHeader>
          <CardContent>
            {role.description && (
              <p className="mb-2 text-sm text-[var(--muted-foreground)]">{role.description}</p>
            )}
            <div className="flex flex-wrap gap-1">
              {role.permissions.map((permission) => (
                <Badge key={permission} tone="neutral">
                  {permission}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

function SemanticCatalogSection() {
  const status = useTenantSemanticCatalogStatus()
  if (!status.data) return null
  return (
    <Card className="mt-4">
      <CardContent className="pt-4">
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Draft</dt>
            <dd className="text-lg font-semibold tabular-nums">{status.data.draft_count}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Reviewed</dt>
            <dd className="text-lg font-semibold tabular-nums">{status.data.reviewed_count}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Published</dt>
            <dd className="text-lg font-semibold tabular-nums">{status.data.published_count}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Superseded</dt>
            <dd className="text-lg font-semibold tabular-nums">{status.data.superseded_count}</dd>
          </div>
        </dl>
        <a href="/semantic-review" className="mt-3 inline-block text-xs font-medium text-[var(--accent)] hover:underline">
          Open SME Semantic Review →
        </a>
      </CardContent>
    </Card>
  )
}

function PendingReviewsSection() {
  const pending = useTenantPendingReviews()
  if (!pending.data) return null
  return (
    <Card className="mt-4">
      <CardContent className="flex flex-col gap-3 pt-4 text-sm">
        <p>
          <span className="text-lg font-semibold tabular-nums">{pending.data.total_pending}</span>{' '}
          item(s) pending review across onboarding and the semantic catalog.
        </p>
        {Object.keys(pending.data.onboarding_pending_by_type).length > 0 && (
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Object.entries(pending.data.onboarding_pending_by_type).map(([type, count]) => (
              <div key={type}>
                <dt className="text-xs text-[var(--muted-foreground)]">{type}</dt>
                <dd className="font-semibold tabular-nums">{count}</dd>
              </div>
            ))}
          </dl>
        )}
        <div className="flex gap-3 text-xs">
          <a href="/db-onboarding" className="font-medium text-[var(--accent)] hover:underline">
            Review in Database Onboarding →
          </a>
          <a href="/semantic-review" className="font-medium text-[var(--accent)] hover:underline">
            Review in SME Semantic Review →
          </a>
        </div>
      </CardContent>
    </Card>
  )
}

function GoldenQuestionsSection() {
  const golden = useTenantGoldenQuestions()
  return (
    <div className="flex flex-col gap-2 pt-4">
      {golden.data?.length === 0 && (
        <p className="text-sm text-[var(--muted-foreground)]">No golden questions yet.</p>
      )}
      {golden.data?.map((entry) => (
        <div
          key={entry.job_id}
          className="flex items-center justify-between rounded-md border border-[var(--border)] px-3 py-2 text-sm"
        >
          <span>{entry.database_label}</span>
          <Badge tone="neutral">{entry.question_count} questions</Badge>
        </div>
      ))}
    </div>
  )
}

function EvaluationSection() {
  const evaluation = useTenantEvaluationResults()
  return (
    <div className="flex flex-col gap-2 pt-4">
      {evaluation.data?.length === 0 && (
        <p className="text-sm text-[var(--muted-foreground)]">No evaluation runs yet.</p>
      )}
      {evaluation.data?.map((entry) => (
        <div
          key={entry.job_id}
          className="flex items-center justify-between rounded-md border border-[var(--border)] px-3 py-2 text-sm"
        >
          <span>{entry.database_label}</span>
          <Badge tone={entry.pass_count === entry.total_count ? 'success' : 'warning'}>
            {entry.pass_count} / {entry.total_count} passed
          </Badge>
        </div>
      ))}
    </div>
  )
}

function UsageSection() {
  const metrics = useTenantPerformanceMetrics()
  if (!metrics.data) return null
  const data = metrics.data
  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle className="text-sm">
          Recent AI usage ({data.window_requests} of {data.max_window_requests} requests)
        </CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">Mean latency</dt>
            <dd className="font-semibold tabular-nums">{data.requests.mean_ms} ms</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">P95 latency</dt>
            <dd className="font-semibold tabular-nums">{data.requests.p95_ms} ms</dd>
          </div>
          {Object.entries(data.status_counts).map(([statusName, count]) => (
            <div key={statusName}>
              <dt className="text-xs text-[var(--muted-foreground)]">{statusName}</dt>
              <dd className="font-semibold tabular-nums">{count}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  )
}

function RecommendationsSection() {
  const recommendations = useTenantRecommendations()
  const quality = useTenantRecommendationQualityMetrics()
  return (
    <div className="flex flex-col gap-4 pt-4">
      {quality.data && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Recommendation quality</CardTitle>
          </CardHeader>
          <CardContent className="flex gap-4 text-sm">
            <div>
              <div className="text-xs text-[var(--muted-foreground)]">Total</div>
              <div className="text-lg font-semibold tabular-nums">{quality.data.total}</div>
            </div>
            <div>
              <div className="text-xs text-[var(--muted-foreground)]">Acceptance rate</div>
              <div className="text-lg font-semibold tabular-nums">
                {quality.data.acceptance_rate !== null
                  ? `${(quality.data.acceptance_rate * 100).toFixed(0)}%`
                  : '—'}
              </div>
            </div>
          </CardContent>
        </Card>
      )}
      <div className="flex flex-col gap-2">
        {recommendations.data?.length === 0 && (
          <p className="text-sm text-[var(--muted-foreground)]">No recommendations yet.</p>
        )}
        {recommendations.data?.map((recommendation) => (
          <div
            key={recommendation.id}
            className="rounded-md border border-[var(--border)] px-3 py-2 text-sm"
          >
            <div className="flex items-center gap-2">
              <Badge tone="neutral">{recommendation.category ?? recommendation.kind}</Badge>
              <Badge tone={recommendation.status === 'accepted' ? 'success' : 'neutral'}>
                {recommendation.status}
              </Badge>
            </div>
            <p className="mt-1">{recommendation.claim_text}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

function AuditSection() {
  const audit = useTenantAuditEvents()
  return (
    <div className="flex flex-col gap-1.5 pt-4">
      <p className="text-xs text-[var(--muted-foreground)]">
        Security events scoped to your own tenant -- a bounded, in-memory, single-process view
        that resets on restart, not a durable log.
      </p>
      {audit.data?.map((event, index) => (
        <div
          key={`${event.timestamp}-${index}`}
          className="rounded-md border border-[var(--border)] px-3 py-2 text-sm"
        >
          <div className="flex items-center gap-2">
            <Badge tone={event.severity === 'warning' ? 'warning' : 'neutral'}>
              {event.severity}
            </Badge>
            <span className="font-mono text-xs">{event.event_type}</span>
          </div>
          <p className="mt-1 text-[var(--muted-foreground)]">{event.detail}</p>
        </div>
      ))}
    </div>
  )
}
