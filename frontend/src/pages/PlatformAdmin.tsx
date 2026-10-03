import { type FormEvent, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  useAssignUserRole,
  useAuditLogs,
  useAvailableModels,
  useConfigStatus,
  useCreateTenant,
  useHealth,
  usePlatformDatabases,
  usePlatformJobs,
  usePlatformMetrics,
  usePlatformRoles,
  usePlatformTenants,
  usePlatformUsers,
  useRemoveUserRole,
  useSecurityEvents,
  useSemanticReviewQueue,
  useSetTenantStatus,
} from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import type { PlatformUserOut, TenantOut } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

/** The global platform-admin dashboard -- Prompt 28, gated on
 * `identity.rbac.Permission.PLATFORM_ADMIN` (the `platform_admin` role),
 * a genuinely different, cross-tenant dimension from the tenant-scoped
 * `admin`/`analyst` gate `DatabaseOnboarding.tsx`/`SemanticReview.tsx`
 * use. A tenant's own admin account sees a 403 from every route this
 * page calls -- this is not a UI-only restriction layered on top of
 * something those accounts could already reach.
 *
 * **Reuses rather than duplicates**: the Health and Models sections call
 * the exact same `useHealth`/`useAvailableModels` hooks every other page
 * in this app already uses -- both of those routes are already public/
 * unscoped, so there is no platform-admin-specific backend route for
 * either. Every other section calls a real, new `/platform-admin/*`
 * route (`api/platform_admin.py`), each of which itself reuses an
 * existing repository function or module (`identity/repositories
 * /tenants.py`'s full CRUD existed since Prompt 20 and was never exposed
 * until this page; `observability.metrics.get_default_metrics()
 * .snapshot(tenant_id=None)` likewise).
 *
 * **Known, disclosed limitations**: "Audit Logs" only ever shows this
 * prompt's own new mutating actions (tenant status changes, role
 * assignment) -- most of this application's other mutating routes still
 * only emit a `security.audit_log` line, not a persisted `AuditLog` row
 * (see `identity/repositories/audit.py`'s own docstring). "Security
 * Events" is a bounded, in-memory, single-process ring buffer that
 * resets on restart (the same disclosed limit `observability.metrics
 * .PerformanceMetrics` already carries) -- it is an operational "what
 * just happened" view, not a durable SIEM.
 */
export function PlatformAdmin() {
  // UX-only: the nav tab that links here is already hidden for anyone
  // without this role (AppShell.tsx), but a direct URL visit should get
  // an honest message rather than a page full of 403s. The real
  // enforcement is entirely server-side -- every route in
  // `api/platform_admin.py` independently checks
  // `identity.rbac.Permission.PLATFORM_ADMIN`.
  const isPlatformAdmin = Boolean(
    useLocalAuthStore((state) => state.user?.roles.includes('platform_admin')),
  )
  if (!isPlatformAdmin) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-[var(--muted-foreground)]">
        This page requires the platform_admin role.
      </div>
    )
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <h1 className="text-xl font-bold">🛡️ Platform Admin</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Operate the platform across every tenant using real data -- no direct database access
          required.
        </p>

        <Tabs defaultValue="tenants">
          <TabsList className="mt-6 flex-wrap">
            <TabsTrigger value="tenants">Tenants</TabsTrigger>
            <TabsTrigger value="users">Users</TabsTrigger>
            <TabsTrigger value="roles">Roles & Permissions</TabsTrigger>
            <TabsTrigger value="databases">Databases</TabsTrigger>
            <TabsTrigger value="review-queue">Semantic Review</TabsTrigger>
            <TabsTrigger value="jobs">Jobs</TabsTrigger>
            <TabsTrigger value="health">Health & Models</TabsTrigger>
            <TabsTrigger value="usage">Usage & Latency</TabsTrigger>
            <TabsTrigger value="security">Security Events</TabsTrigger>
            <TabsTrigger value="audit">Audit Logs</TabsTrigger>
            <TabsTrigger value="config">Configuration</TabsTrigger>
          </TabsList>

          <TabsContent value="tenants">
            <TenantsSection />
          </TabsContent>
          <TabsContent value="users">
            <UsersSection />
          </TabsContent>
          <TabsContent value="roles">
            <RolesSection />
          </TabsContent>
          <TabsContent value="databases">
            <DatabasesSection />
          </TabsContent>
          <TabsContent value="review-queue">
            <SemanticReviewQueueSection />
          </TabsContent>
          <TabsContent value="jobs">
            <JobsSection />
          </TabsContent>
          <TabsContent value="health">
            <HealthAndModelsSection />
          </TabsContent>
          <TabsContent value="usage">
            <UsageSection />
          </TabsContent>
          <TabsContent value="security">
            <SecurityEventsSection />
          </TabsContent>
          <TabsContent value="audit">
            <AuditLogsSection />
          </TabsContent>
          <TabsContent value="config">
            <ConfigStatusSection />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

function TenantsSection() {
  const tenants = usePlatformTenants()
  const createTenant = useCreateTenant()
  const [showForm, setShowForm] = useState(false)
  const [tenantId, setTenantId] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      await createTenant.mutateAsync({ tenant_id: tenantId, name })
      setTenantId('')
      setName('')
      setShowForm(false)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create tenant.')
    }
  }

  return (
    <div className="flex flex-col gap-3 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Tenants ({tenants.data?.length ?? 0})</h2>
        <Button size="sm" onClick={() => setShowForm((v) => !v)}>
          {showForm ? 'Close' : '+ New tenant'}
        </Button>
      </div>
      {showForm && (
        <form
          onSubmit={handleCreate}
          className="flex flex-wrap items-end gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-3"
        >
          <input
            required
            value={tenantId}
            onChange={(event) => setTenantId(event.target.value)}
            placeholder="tenant id (slug)"
            className="rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
          <input
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Display name"
            className="rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
          <Button type="submit" size="sm" disabled={createTenant.isPending}>
            Create
          </Button>
        </form>
      )}
      {error && <p className="text-sm text-[var(--danger)]">{error}</p>}
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-sm">
          <thead className="bg-[var(--muted)] text-xs uppercase text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2 font-medium">Tenant</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Users</th>
              <th className="px-3 py-2 font-medium">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {tenants.data?.map((tenant) => <TenantRow key={tenant.id} tenant={tenant} />)}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function TenantRow({ tenant }: { tenant: TenantOut }) {
  const setStatus = useSetTenantStatus(tenant.id)
  const handleToggle = () => {
    void setStatus.mutateAsync({ status: tenant.status === 'active' ? 'suspended' : 'active' })
  }
  return (
    <tr>
      <td className="px-3 py-2">
        <div className="font-medium">{tenant.name}</div>
        <div className="font-mono text-xs text-[var(--muted-foreground)]">{tenant.id}</div>
      </td>
      <td className="px-3 py-2">
        <Badge tone={tenant.status === 'active' ? 'success' : 'danger'}>{tenant.status}</Badge>
      </td>
      <td className="px-3 py-2 tabular-nums">{tenant.user_count}</td>
      <td className="px-3 py-2">
        <Button size="sm" variant="secondary" disabled={setStatus.isPending} onClick={handleToggle}>
          {tenant.status === 'active' ? 'Suspend' : 'Re-activate'}
        </Button>
      </td>
    </tr>
  )
}

function UsersSection() {
  const [tenantId, setTenantId] = useState('')
  const users = usePlatformUsers({ tenantId: tenantId || undefined })

  return (
    <div className="flex flex-col gap-3 pt-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Users ({users.data?.length ?? 0})</h2>
        <input
          value={tenantId}
          onChange={(event) => setTenantId(event.target.value)}
          placeholder="filter by tenant id"
          className="ml-auto rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
      </div>
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-sm">
          <thead className="bg-[var(--muted)] text-xs uppercase text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2 font-medium">Email</th>
              <th className="px-3 py-2 font-medium">Tenant</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Roles</th>
              <th className="px-3 py-2 font-medium">Manage</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {users.data?.map((user) => <UserRow key={user.id} user={user} />)}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function UserRow({ user }: { user: PlatformUserOut }) {
  const assignRole = useAssignUserRole(user.id)
  const removeRole = useRemoveUserRole(user.id)
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
      <td className="px-3 py-2 font-mono text-xs">{user.tenant_id}</td>
      <td className="px-3 py-2">
        <Badge tone={user.status === 'active' ? 'success' : 'neutral'}>{user.status}</Badge>
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {user.roles.map((role) => (
            <Badge key={role} tone="neutral">
              {role}
              <button
                className="ml-1 text-[var(--muted-foreground)] hover:text-[var(--danger)]"
                onClick={() => removeRole.mutate(role)}
                aria-label={`Remove ${role} role`}
              >
                ×
              </button>
            </Badge>
          ))}
        </div>
      </td>
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
    </tr>
  )
}

function RolesSection() {
  const roles = usePlatformRoles()
  return (
    <div className="flex flex-col gap-3 pt-4">
      {roles.data?.map((role) => (
        <Card key={role.name}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              {role.name}
              {role.is_system_role && <Badge tone="neutral">system</Badge>}
            </CardTitle>
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

function DatabasesSection() {
  const databases = usePlatformDatabases()
  return (
    <div className="flex flex-col gap-3 pt-4">
      {databases.data?.map((database) => (
        <Card key={database.name}>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm">{database.name}</CardTitle>
            <Badge tone={database.healthy ? 'success' : 'danger'}>
              {database.healthy ? 'reachable' : 'unreachable'}
            </Badge>
          </CardHeader>
          <CardContent className="text-sm">
            <p className="text-[var(--muted-foreground)]">
              {database.db_type}
              {database.db_host ? ` · ${database.db_host}` : ''}
              {database.db_name ? ` / ${database.db_name}` : ''}
            </p>
            {database.tenant_ids.length > 0 && (
              <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                Bound to tenants: {database.tenant_ids.join(', ')}
              </p>
            )}
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">{database.detail}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

function SemanticReviewQueueSection() {
  const queue = useSemanticReviewQueue()
  if (!queue.data) return null
  const data = queue.data
  return (
    <div className="flex flex-col gap-4 pt-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Governed semantic catalog</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-3 gap-3 text-sm">
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">Draft</dt>
              <dd className="text-lg font-semibold tabular-nums">{data.catalog_draft_count}</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">Reviewed</dt>
              <dd className="text-lg font-semibold tabular-nums">{data.catalog_reviewed_count}</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">Published</dt>
              <dd className="text-lg font-semibold tabular-nums">{data.catalog_published_count}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Onboarding review items pending, by type</CardTitle>
        </CardHeader>
        <CardContent>
          {Object.keys(data.onboarding_pending_by_type).length === 0 ? (
            <p className="text-sm text-[var(--muted-foreground)]">Nothing pending.</p>
          ) : (
            <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
              {Object.entries(data.onboarding_pending_by_type).map(([type, count]) => (
                <div key={type}>
                  <dt className="text-xs text-[var(--muted-foreground)]">{type}</dt>
                  <dd className="text-lg font-semibold tabular-nums">{count}</dd>
                </div>
              ))}
            </dl>
          )}
          <p className="mt-3 text-xs text-[var(--muted-foreground)]">
            Review items are decided per-job on the Database Onboarding page; governed-catalog
            entries are decided on SME Semantic Review -- this is a cross-tenant count rollup, not
            a second place to act on them.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

function JobsSection() {
  const [status, setStatus] = useState('')
  const jobs = usePlatformJobs(status || undefined)
  return (
    <div className="flex flex-col gap-3 pt-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Onboarding jobs ({jobs.data?.length ?? 0})</h2>
        <Select
          className="ml-auto"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
        >
          <option value="">All statuses</option>
          <option value="pending">pending</option>
          <option value="discovering">discovering</option>
          <option value="awaiting_review">awaiting_review</option>
          <option value="publishing">publishing</option>
          <option value="published">published</option>
          <option value="failed">failed</option>
          <option value="cancelled">cancelled</option>
        </Select>
      </div>
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-sm">
          <thead className="bg-[var(--muted)] text-xs uppercase text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2 font-medium">Database</th>
              <th className="px-3 py-2 font-medium">Tenant</th>
              <th className="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {jobs.data?.map((job) => (
              <tr key={job.id}>
                <td className="px-3 py-2">{job.database_label}</td>
                <td className="px-3 py-2 font-mono text-xs">{job.tenant_id}</td>
                <td className="px-3 py-2">
                  <Badge tone="neutral">{job.status}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function HealthAndModelsSection() {
  const health = useHealth()
  const models = useAvailableModels()
  return (
    <div className="flex flex-col gap-4 pt-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Ollama</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          <Badge tone={health.data?.ollama.ok ? 'success' : 'danger'}>
            {health.data?.ollama.ok ? 'reachable' : 'unreachable'}
          </Badge>
          <p className="mt-1 text-[var(--muted-foreground)]">{health.data?.ollama.detail}</p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Databases (schema index)</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          {health.data?.databases.map((database) => (
            <div key={database.name} className="flex items-center justify-between">
              <span>{database.name}</span>
              <Badge tone={database.schema_index.ok ? 'success' : 'warning'}>
                {database.schema_index.detail}
              </Badge>
            </div>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Models ({models.data?.models.length ?? 0})</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-1 text-sm">
          {models.data?.models.map((model) => (
            <div key={model.id} className="flex items-center gap-2">
              <span>{model.display_name}</span>
              {model.is_default && <Badge tone="accent">default</Badge>}
              <Badge tone={model.installed ? 'success' : 'neutral'}>
                {model.installed ? 'installed' : 'not installed'}
              </Badge>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}

function UsageSection() {
  const metrics = usePlatformMetrics()
  if (!metrics.data) return null
  const data = metrics.data
  return (
    <div className="flex flex-col gap-4 pt-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">
            Merged request latency ({data.window_requests} of {data.max_window_requests} recent
            requests, across every tenant)
          </CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">Mean</dt>
              <dd className="font-semibold tabular-nums">{data.requests.mean_ms} ms</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">P50</dt>
              <dd className="font-semibold tabular-nums">{data.requests.p50_ms} ms</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">P95</dt>
              <dd className="font-semibold tabular-nums">{data.requests.p95_ms} ms</dd>
            </div>
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">Max</dt>
              <dd className="font-semibold tabular-nums">{data.requests.max_ms} ms</dd>
            </div>
          </dl>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Status mix</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {Object.entries(data.status_counts).map(([statusName, count]) => (
            <Badge key={statusName} tone={statusName === 'failed' ? 'danger' : 'neutral'}>
              {statusName}: {count}
            </Badge>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Per-stage timing</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-[var(--muted-foreground)]">
              <tr>
                <th className="pr-3 font-medium">Stage</th>
                <th className="pr-3 font-medium">Count</th>
                <th className="pr-3 font-medium">Mean</th>
                <th className="pr-3 font-medium">P95</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {data.stages.map((stage) => (
                <tr key={stage.stage}>
                  <td className="py-1 pr-3">{stage.stage}</td>
                  <td className="py-1 pr-3 tabular-nums">{stage.count}</td>
                  <td className="py-1 pr-3 tabular-nums">{stage.mean_ms} ms</td>
                  <td className="py-1 pr-3 tabular-nums">{stage.p95_ms} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  )
}

function SecurityEventsSection() {
  const [severity, setSeverity] = useState('')
  const events = useSecurityEvents({ severity: severity || undefined, limit: 100 })
  return (
    <div className="flex flex-col gap-3 pt-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Security events ({events.data?.length ?? 0})</h2>
        <Select className="ml-auto" value={severity} onChange={(event) => setSeverity(event.target.value)}>
          <option value="">All severities</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="critical">critical</option>
        </Select>
      </div>
      <p className="text-xs text-[var(--muted-foreground)]">
        A bounded, in-memory, single-process view of the most recent events -- resets on restart,
        not a durable log.
      </p>
      <div className="flex flex-col gap-1.5">
        {events.data?.map((event, index) => (
          <div
            key={`${event.timestamp}-${index}`}
            className="rounded-md border border-[var(--border)] px-3 py-2 text-sm"
          >
            <div className="flex items-center gap-2">
              <Badge
                tone={
                  event.severity === 'critical'
                    ? 'danger'
                    : event.severity === 'warning'
                      ? 'warning'
                      : 'neutral'
                }
              >
                {event.severity}
              </Badge>
              <span className="font-mono text-xs">{event.event_type}</span>
              <span className="ml-auto text-xs text-[var(--muted-foreground)]">
                {event.tenant_id ?? '-'}
              </span>
            </div>
            <p className="mt-1 text-[var(--muted-foreground)]">{event.detail}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

function AuditLogsSection() {
  const audit = useAuditLogs()
  return (
    <div className="flex flex-col gap-3 pt-4">
      <h2 className="text-sm font-semibold">Audit logs ({audit.data?.length ?? 0})</h2>
      <p className="text-xs text-[var(--muted-foreground)]">
        Only this dashboard's own actions (tenant status changes, role assignment) are recorded
        here -- a disclosed, narrower scope than every mutation in this application.
      </p>
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-sm">
          <thead className="bg-[var(--muted)] text-xs uppercase text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2 font-medium">Action</th>
              <th className="px-3 py-2 font-medium">Resource</th>
              <th className="px-3 py-2 font-medium">Outcome</th>
              <th className="px-3 py-2 font-medium">When</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {audit.data?.map((event) => (
              <tr key={event.id}>
                <td className="px-3 py-2">{event.action}</td>
                <td className="px-3 py-2 font-mono text-xs">
                  {event.resource_type}/{event.resource_id}
                </td>
                <td className="px-3 py-2">
                  <Badge tone={event.outcome === 'success' ? 'success' : 'danger'}>
                    {event.outcome}
                  </Badge>
                </td>
                <td className="px-3 py-2 text-xs text-[var(--muted-foreground)]">
                  {new Date(event.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ConfigStatusSection() {
  const config = useConfigStatus()
  if (!config.data) return null
  return (
    <div className="pt-4">
      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {Object.entries(config.data.flags).map(([flag, value]) => (
          <div
            key={flag}
            className="flex items-center justify-between rounded-md border border-[var(--border)] px-3 py-1.5 text-sm"
          >
            <span className="font-mono text-xs">{flag}</span>
            <Badge tone={value ? 'success' : 'neutral'}>{value ? 'on' : 'off'}</Badge>
          </div>
        ))}
      </dl>
    </div>
  )
}
