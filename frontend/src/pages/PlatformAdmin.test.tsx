import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import * as platformAdminApi from '@/lib/platformAdminApi'
import type {
  AuditLogOut,
  ConfigStatusOut,
  DatabaseStatusOut,
  HealthResponse,
  ModelsResponse,
  PerformanceMetricsResponse,
  PlatformOnboardingJobOut,
  PlatformUserOut,
  RoleOut,
  SecurityEventOut,
  SemanticReviewQueueOut,
  TenantOut,
} from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { PlatformAdmin } from './PlatformAdmin'

vi.mock('@/lib/platformAdminApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/platformAdminApi')>()
  return {
    ...actual,
    listTenants: vi.fn(),
    createTenant: vi.fn(),
    setTenantStatus: vi.fn(),
    listPlatformUsers: vi.fn(),
    assignUserRole: vi.fn(),
    removeUserRole: vi.fn(),
    listRoles: vi.fn(),
    listPlatformDatabases: vi.fn(),
    getSemanticReviewQueue: vi.fn(),
    listPlatformJobs: vi.fn(),
    getPlatformMetrics: vi.fn(),
    listSecurityEvents: vi.fn(),
    listAuditLogs: vi.fn(),
    getConfigStatus: vi.fn(),
  }
})

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, getHealth: vi.fn(), getAvailableModels: vi.fn() }
})

const initialAuthState = useLocalAuthStore.getState()

function setUserRole(roles: string[]) {
  useLocalAuthStore.setState({
    status: 'authenticated',
    user: {
      id: 'u1',
      email: 'platform@example.com',
      username: null,
      display_name: 'Platform',
      needs_profile_completion: false,
      status: 'active',
      is_email_verified: true,
      roles,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    },
  } as never)
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PlatformAdmin />
    </QueryClientProvider>,
  )
}

const emptyHealth: HealthResponse = {
  status: 'ok',
  databases: [],
  ollama: { ok: true, detail: 'Reachable.' },
  voice_enabled: false,
  media_search_enabled: false,
  local_auth_enabled: true,
  google_signin_enabled: false,
  google_client_id: null,
  vision_enabled: false,
  vision_provider: null,
  vision_model: null,
  vision_model_available: null,
  ocr_enabled: false,
}

const emptyModels: ModelsResponse = {
  provider: 'ollama',
  default_model: 'llama3.1:8b',
  selection_enabled: false,
  models: [],
}

const emptyMetrics: PerformanceMetricsResponse = {
  started_at: '2026-01-01T00:00:00Z',
  window_requests: 0,
  max_window_requests: 500,
  requests: { count: 0, mean_ms: 0, p50_ms: 0, p95_ms: 0, max_ms: 0 },
  stages: [],
  status_counts: {},
  tenant_id: null,
  result_cache_hits: 0,
  result_cache_misses: 0,
  database_concurrency_rejections: 0,
}

const emptyQueue: SemanticReviewQueueOut = {
  catalog_draft_count: 0,
  catalog_reviewed_count: 0,
  catalog_published_count: 0,
  onboarding_pending_by_type: {},
  total_pending: 0,
}

const emptyConfig: ConfigStatusOut = { flags: { enable_voice_mode: true, enable_web_search: false } }

function mockAllSections() {
  vi.mocked(platformAdminApi.listTenants).mockResolvedValue([])
  vi.mocked(platformAdminApi.listPlatformUsers).mockResolvedValue([])
  vi.mocked(platformAdminApi.listRoles).mockResolvedValue([])
  vi.mocked(platformAdminApi.listPlatformDatabases).mockResolvedValue([])
  vi.mocked(platformAdminApi.getSemanticReviewQueue).mockResolvedValue(emptyQueue)
  vi.mocked(platformAdminApi.listPlatformJobs).mockResolvedValue([])
  vi.mocked(platformAdminApi.getPlatformMetrics).mockResolvedValue(emptyMetrics)
  vi.mocked(platformAdminApi.listSecurityEvents).mockResolvedValue([])
  vi.mocked(platformAdminApi.listAuditLogs).mockResolvedValue([])
  vi.mocked(platformAdminApi.getConfigStatus).mockResolvedValue(emptyConfig)
  vi.mocked(api.getHealth).mockResolvedValue(emptyHealth)
  vi.mocked(api.getAvailableModels).mockResolvedValue(emptyModels)
}

describe('PlatformAdmin', () => {
  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
    vi.clearAllMocks()
  })

  it('shows a not-authorized message for a tenant admin, not the dashboard', () => {
    setUserRole(['admin'])
    mockAllSections()
    renderPage()
    expect(screen.getByText(/requires the platform_admin role/i)).toBeInTheDocument()
    expect(screen.queryByText(/platform admin/i)).not.toBeInTheDocument()
  })

  it('renders the dashboard for the platform_admin role', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    renderPage()
    expect(await screen.findByRole('tab', { name: /tenants/i })).toBeInTheDocument()
  })

  it('lists tenants and lets a platform admin suspend one', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const tenant: TenantOut = {
      id: 'tenant-a',
      name: 'Tenant A',
      status: 'active',
      user_count: 3,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }
    vi.mocked(platformAdminApi.listTenants).mockResolvedValue([tenant])
    vi.mocked(platformAdminApi.setTenantStatus).mockResolvedValue({
      ...tenant,
      status: 'suspended',
    })

    renderPage()
    expect(await screen.findByText('Tenant A')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /suspend/i }))

    await waitFor(() =>
      expect(platformAdminApi.setTenantStatus).toHaveBeenCalledWith('tenant-a', {
        status: 'suspended',
      }),
    )
  })

  it('shows users across tenants and can assign a role', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const user: PlatformUserOut = {
      id: 'user-1',
      email: 'someone@tenant-b.example.com',
      display_name: 'Someone',
      tenant_id: 'tenant-b',
      status: 'active',
      roles: ['user'],
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    }
    vi.mocked(platformAdminApi.listPlatformUsers).mockResolvedValue([user])
    vi.mocked(platformAdminApi.assignUserRole).mockResolvedValue({
      ...user,
      roles: ['user', 'analyst'],
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /^users$/i }))
    expect(await screen.findByText('someone@tenant-b.example.com')).toBeInTheDocument()
    expect(screen.getByText('tenant-b')).toBeInTheDocument()

    await userEvent.type(screen.getByPlaceholderText(/role name/i), 'analyst')
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }))

    await waitFor(() =>
      expect(platformAdminApi.assignUserRole).toHaveBeenCalledWith('user-1', {
        role_name: 'analyst',
      }),
    )
  })

  it('lists roles with their granted permissions', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const role: RoleOut = {
      name: 'platform_admin',
      description: 'Operates the platform-wide admin dashboard.',
      is_system_role: true,
      permissions: ['platform.admin', 'users.read'],
    }
    vi.mocked(platformAdminApi.listRoles).mockResolvedValue([role])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /roles & permissions/i }))
    expect(await screen.findByText('platform_admin')).toBeInTheDocument()
    expect(screen.getByText('platform.admin')).toBeInTheDocument()
  })

  it('shows database health without any credential field', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const database: DatabaseStatusOut = {
      name: 'default',
      db_type: 'postgresql',
      db_host: 'db.internal',
      db_port: 5432,
      db_name: 'warehouse',
      db_schema: null,
      tenant_ids: [],
      healthy: true,
      detail: 'Connected.',
    }
    vi.mocked(platformAdminApi.listPlatformDatabases).mockResolvedValue([database])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /databases/i }))
    expect(await screen.findByText('default')).toBeInTheDocument()
    expect(screen.getByText('reachable')).toBeInTheDocument()
    expect(screen.queryByText(/password/i)).not.toBeInTheDocument()
  })

  it('shows the semantic review queue counts', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    vi.mocked(platformAdminApi.getSemanticReviewQueue).mockResolvedValue({
      catalog_draft_count: 2,
      catalog_reviewed_count: 1,
      catalog_published_count: 5,
      onboarding_pending_by_type: { pii_classification: 3 },
      total_pending: 6,
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /semantic review/i }))
    expect(await screen.findByText('2')).toBeInTheDocument()
    expect(screen.getByText('pii_classification')).toBeInTheDocument()
  })

  it('lists jobs across tenants', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const job: PlatformOnboardingJobOut = {
      id: 'job-1',
      tenant_id: 'tenant-b',
      database_label: 'Acme Warehouse',
      status: 'awaiting_review',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }
    vi.mocked(platformAdminApi.listPlatformJobs).mockResolvedValue([job])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /^jobs$/i }))
    expect(await screen.findByText('Acme Warehouse')).toBeInTheDocument()
    expect(screen.getByText('tenant-b')).toBeInTheDocument()
  })

  it('reuses the existing health/models hooks for the Health & Models tab', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    vi.mocked(api.getHealth).mockResolvedValue({
      ...emptyHealth,
      databases: [
        {
          name: 'default',
          connection: { ok: true, detail: 'ok' },
          schema_index: { ok: true, detail: '12 table(s) indexed.' },
        },
      ],
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /health & models/i }))
    expect(await screen.findByText('12 table(s) indexed.')).toBeInTheDocument()
    expect(api.getHealth).toHaveBeenCalled()
    expect(api.getAvailableModels).toHaveBeenCalled()
  })

  it('shows merged usage metrics with no tenant scoping', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    vi.mocked(platformAdminApi.getPlatformMetrics).mockResolvedValue({
      ...emptyMetrics,
      window_requests: 42,
      status_counts: { succeeded: 40, failed: 2 },
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /usage & latency/i }))
    expect(await screen.findByText(/42 of 500/i)).toBeInTheDocument()
    expect(screen.getByText(/failed: 2/i)).toBeInTheDocument()
  })

  it('shows security events and can filter by severity', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const event: SecurityEventOut = {
      timestamp: '2026-01-01T00:00:00Z',
      event_type: 'rate_limit_tripped',
      severity: 'warning',
      detail: 'Too many requests.',
      correlation_id: 'abc',
      tenant_id: 'tenant-a',
      context: {},
    }
    vi.mocked(platformAdminApi.listSecurityEvents).mockResolvedValue([event])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /security events/i }))
    expect(await screen.findByText('rate_limit_tripped')).toBeInTheDocument()
    expect(screen.getByText('Too many requests.')).toBeInTheDocument()
  })

  it('shows audit log entries from the new AuditLog table', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()
    const entry: AuditLogOut = {
      id: 'audit-1',
      actor_user_id: 'u1',
      subject_user_id: null,
      action: 'tenant_status_changed',
      resource_type: 'tenant',
      resource_id: 'tenant-c',
      outcome: 'success',
      created_at: '2026-01-01T00:00:00Z',
      metadata: { status: 'suspended' },
    }
    vi.mocked(platformAdminApi.listAuditLogs).mockResolvedValue([entry])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /audit logs/i }))
    expect(await screen.findByText('tenant_status_changed')).toBeInTheDocument()
    expect(screen.getByText('tenant/tenant-c')).toBeInTheDocument()
  })

  it('shows only boolean config flags', async () => {
    setUserRole(['platform_admin'])
    mockAllSections()

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /configuration/i }))
    expect(await screen.findByText('enable_voice_mode')).toBeInTheDocument()
    expect(screen.getByText('enable_web_search')).toBeInTheDocument()
  })
})
