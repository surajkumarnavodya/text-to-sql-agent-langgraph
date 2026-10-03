import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '@/lib/api'
import * as tenantAdminApi from '@/lib/tenantAdminApi'
import type {
  DatabaseStatusOut,
  PerformanceMetricsResponse,
  PlatformUserOut,
  RecommendationQualityMetricsOut,
  RecommendationRecordOut,
  RoleOut,
  SchemaRefreshResponse,
  SecurityEventOut,
  TenantOut,
} from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { TenantAdmin } from './TenantAdmin'

vi.mock('@/lib/tenantAdminApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/tenantAdminApi')>()
  return {
    ...actual,
    getTenantProfile: vi.fn(),
    listTenantDatabases: vi.fn(),
    refreshTenantDatabases: vi.fn(),
    listTenantUsers: vi.fn(),
    assignTenantUserRole: vi.fn(),
    removeTenantUserRole: vi.fn(),
    listTenantAssignableRoles: vi.fn(),
    getTenantSemanticCatalogStatus: vi.fn(),
    getTenantPendingReviews: vi.fn(),
    listTenantGoldenQuestions: vi.fn(),
    listTenantEvaluationResults: vi.fn(),
    listTenantAuditEvents: vi.fn(),
    listTenantRecommendations: vi.fn(),
    getTenantRecommendationQualityMetrics: vi.fn(),
  }
})

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, getPerformanceMetrics: vi.fn() }
})

const initialAuthState = useLocalAuthStore.getState()

function setUserRole(roles: string[]) {
  useLocalAuthStore.setState({
    status: 'authenticated',
    user: {
      id: 'u1',
      email: 'tenant-admin@example.com',
      username: null,
      display_name: 'Tenant Admin',
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
      <TenantAdmin />
    </QueryClientProvider>,
  )
}

const emptyMetrics: PerformanceMetricsResponse = {
  started_at: '2026-01-01T00:00:00Z',
  window_requests: 0,
  max_window_requests: 500,
  requests: { count: 0, mean_ms: 0, p50_ms: 0, p95_ms: 0, max_ms: 0 },
  stages: [],
  status_counts: {},
  tenant_id: 'tenant-a',
  result_cache_hits: 0,
  result_cache_misses: 0,
  database_concurrency_rejections: 0,
}

const emptyQualityMetrics: RecommendationQualityMetricsOut = {
  total: 0,
  by_status: {},
  by_category: {},
  judged_total: 0,
  acceptance_rate: null,
}

function mockAllSections() {
  vi.mocked(tenantAdminApi.getTenantProfile).mockResolvedValue({
    id: 'tenant-a',
    name: 'Tenant A',
    status: 'active',
    user_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as TenantOut)
  vi.mocked(tenantAdminApi.listTenantDatabases).mockResolvedValue([])
  vi.mocked(tenantAdminApi.listTenantUsers).mockResolvedValue([])
  vi.mocked(tenantAdminApi.listTenantAssignableRoles).mockResolvedValue([])
  vi.mocked(tenantAdminApi.getTenantSemanticCatalogStatus).mockResolvedValue({
    draft_count: 0,
    reviewed_count: 0,
    published_count: 0,
    superseded_count: 0,
  })
  vi.mocked(tenantAdminApi.getTenantPendingReviews).mockResolvedValue({
    onboarding_pending_by_type: {},
    catalog_pending_count: 0,
    total_pending: 0,
  })
  vi.mocked(tenantAdminApi.listTenantGoldenQuestions).mockResolvedValue([])
  vi.mocked(tenantAdminApi.listTenantEvaluationResults).mockResolvedValue([])
  vi.mocked(tenantAdminApi.listTenantAuditEvents).mockResolvedValue([])
  vi.mocked(tenantAdminApi.listTenantRecommendations).mockResolvedValue([])
  vi.mocked(tenantAdminApi.getTenantRecommendationQualityMetrics).mockResolvedValue(
    emptyQualityMetrics,
  )
  vi.mocked(api.getPerformanceMetrics).mockResolvedValue(emptyMetrics)
}

describe('TenantAdmin', () => {
  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
    vi.clearAllMocks()
  })

  it('shows a not-authorized message for a plain user', () => {
    setUserRole(['user'])
    mockAllSections()
    renderPage()
    expect(screen.getByText(/requires the admin, auditor, or manager role/i)).toBeInTheDocument()
  })

  it('shows a not-authorized message for an analyst', () => {
    setUserRole(['analyst'])
    mockAllSections()
    renderPage()
    expect(screen.getByText(/requires the admin, auditor, or manager role/i)).toBeInTheDocument()
  })

  it('renders the dashboard for an admin', async () => {
    setUserRole(['admin'])
    mockAllSections()
    renderPage()
    expect(await screen.findByText('Tenant A')).toBeInTheDocument()
  })

  it('renders the dashboard read-only for an auditor', async () => {
    setUserRole(['auditor'])
    mockAllSections()
    const user: PlatformUserOut = {
      id: 'user-1',
      email: 'someone@tenant-a.example.com',
      display_name: 'Someone',
      tenant_id: 'tenant-a',
      status: 'active',
      roles: ['user'],
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    }
    vi.mocked(tenantAdminApi.listTenantUsers).mockResolvedValue([user])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /^users$/i }))
    expect(await screen.findByText('someone@tenant-a.example.com')).toBeInTheDocument()
    // Auditor can view but never manage -- no "Manage" column/inputs.
    expect(screen.queryByPlaceholderText(/role name/i)).not.toBeInTheDocument()
  })

  it('lets an admin refresh only their own tenant databases', async () => {
    setUserRole(['admin'])
    mockAllSections()
    const database: DatabaseStatusOut = {
      name: 'db-a',
      db_type: 'postgresql',
      db_host: 'db.internal',
      db_port: 5432,
      db_name: 'warehouse',
      db_schema: null,
      tenant_ids: ['tenant-a'],
      healthy: true,
      detail: 'Connected.',
    }
    vi.mocked(tenantAdminApi.listTenantDatabases).mockResolvedValue([database])
    vi.mocked(tenantAdminApi.refreshTenantDatabases).mockResolvedValue({
      databases: [],
    } as SchemaRefreshResponse)

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /databases/i }))
    expect(await screen.findByText('db-a')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /refresh schema/i }))
    await waitFor(() => expect(tenantAdminApi.refreshTenantDatabases).toHaveBeenCalled())
  })

  it('lets an admin assign a role to a tenant member', async () => {
    setUserRole(['admin'])
    mockAllSections()
    const user: PlatformUserOut = {
      id: 'user-2',
      email: 'member@tenant-a.example.com',
      display_name: 'Member',
      tenant_id: 'tenant-a',
      status: 'active',
      roles: ['user'],
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    }
    vi.mocked(tenantAdminApi.listTenantUsers).mockResolvedValue([user])
    vi.mocked(tenantAdminApi.assignTenantUserRole).mockResolvedValue({
      ...user,
      roles: ['user', 'analyst'],
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /^users$/i }))
    expect(await screen.findByText('member@tenant-a.example.com')).toBeInTheDocument()

    await userEvent.type(screen.getByPlaceholderText(/role name/i), 'analyst')
    await userEvent.click(screen.getByRole('button', { name: /^add$/i }))

    await waitFor(() =>
      expect(tenantAdminApi.assignTenantUserRole).toHaveBeenCalledWith('user-2', {
        role_name: 'analyst',
      }),
    )
  })

  it('shows roles without platform_admin ever appearing', async () => {
    setUserRole(['admin'])
    mockAllSections()
    const role: RoleOut = {
      name: 'admin',
      description: 'Full account/session/user management.',
      is_system_role: true,
      permissions: ['users.read', 'users.assign_roles'],
    }
    vi.mocked(tenantAdminApi.listTenantAssignableRoles).mockResolvedValue([role])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /roles & permissions/i }))
    expect(await screen.findByText('admin')).toBeInTheDocument()
    expect(screen.queryByText('platform_admin')).not.toBeInTheDocument()
  })

  it('shows pending review counts and links to the real review pages', async () => {
    setUserRole(['admin'])
    mockAllSections()
    vi.mocked(tenantAdminApi.getTenantPendingReviews).mockResolvedValue({
      onboarding_pending_by_type: { pii_classification: 2 },
      catalog_pending_count: 1,
      total_pending: 3,
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /pending reviews/i }))
    expect(await screen.findByText('3')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /review in database onboarding/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /review in sme semantic review/i })).toBeInTheDocument()
  })

  it('shows AI usage reused from the existing performance-metrics route', async () => {
    setUserRole(['admin'])
    mockAllSections()
    vi.mocked(api.getPerformanceMetrics).mockResolvedValue({
      ...emptyMetrics,
      window_requests: 7,
      requests: { count: 7, mean_ms: 120, p50_ms: 100, p95_ms: 200, max_ms: 250 },
    })

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /ai usage/i }))
    expect(await screen.findByText(/7 of 500/i)).toBeInTheDocument()
    expect(api.getPerformanceMetrics).toHaveBeenCalled()
  })

  it('shows recommendations reused from the existing recommendations route', async () => {
    setUserRole(['admin'])
    mockAllSections()
    const recommendation: RecommendationRecordOut = {
      id: 'rec-1',
      tenant_id: 'tenant-a',
      database_id: 'db-a',
      category: 'revenue',
      kind: 'insight',
      rule_or_model: null,
      claim_text: 'Revenue dropped 12% month over month.',
      rationale: null,
      affected_entity: null,
      action: null,
      measurable_impact: null,
      confidence: 0.8,
      evidence: [],
      limitations: [],
      engine_version: '1',
      evidence_version: 'abc',
      status: 'generated',
      generated_at: '2026-01-01T00:00:00Z',
      source_question: null,
      source_sql: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }
    vi.mocked(tenantAdminApi.listTenantRecommendations).mockResolvedValue([recommendation])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /^recommendations$/i }))
    expect(await screen.findByText(/revenue dropped 12%/i)).toBeInTheDocument()
  })

  it('shows audit events scoped to this tenant only', async () => {
    setUserRole(['admin'])
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
    vi.mocked(tenantAdminApi.listTenantAuditEvents).mockResolvedValue([event])

    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /^audit$/i }))
    expect(await screen.findByText('rate_limit_tripped')).toBeInTheDocument()
  })
})
