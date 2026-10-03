import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as onboardingApi from '@/lib/onboardingApi'
import * as catalogApi from '@/lib/semanticCatalogApi'
import type { CatalogEntryOut, OnboardingJob } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { SemanticReview } from './SemanticReview'

vi.mock('@/lib/semanticCatalogApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/semanticCatalogApi')>()
  return {
    ...actual,
    listCatalogEntries: vi.fn(),
    getCatalogEntry: vi.fn(),
    getCatalogEntryVersions: vi.fn(),
    createCatalogEntry: vi.fn(),
    updateCatalogEntry: vi.fn(),
    reviewCatalogEntry: vi.fn(),
    requestCatalogEntryChanges: vi.fn(),
    publishCatalogEntry: vi.fn(),
  }
})

vi.mock('@/lib/onboardingApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/onboardingApi')>()
  return {
    ...actual,
    listOnboardingJobs: vi.fn(),
    listOnboardingReviewItems: vi.fn(),
  }
})

const initialAuthState = useLocalAuthStore.getState()

function makeEntry(overrides: Partial<CatalogEntryOut> = {}): CatalogEntryOut {
  return {
    id: 'entry-1',
    tenant_id: 'default',
    database_id: 'db1',
    concept_type: 'metric',
    concept_key: 'clv',
    business_name: 'Customer Lifetime Value',
    technical_name: null,
    description: 'Total historical revenue per customer',
    grain: 'one row per customer',
    keys: [],
    relationships: [],
    domain: 'Sales',
    synonyms: [],
    business_rules: [],
    examples: [],
    evidence: [],
    confidence: 0.9,
    status: 'draft',
    owner: 'alice',
    version: 1,
    supersedes_id: null,
    truth_level: 'ai_inference',
    reviewed_at: null,
    review_notes: null,
    reviewed_by_user_id: null,
    reviewed_by_display_name: null,
    published_by_user_id: null,
    published_by_display_name: null,
    published_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    approved_expression: 'SUM(Amount)',
    source_tables: ['FactSales'],
    filters: [],
    dimensions: [],
    aggregation: 'SUM',
    conflicting_entry_ids: [],
    conflicting_entry_names: [],
    ...overrides,
  }
}

function makeJob(overrides: Partial<OnboardingJob> = {}): OnboardingJob {
  return {
    id: 'job-1',
    database_label: 'Acme Warehouse',
    db_type: 'mssql',
    db_host: null,
    db_port: null,
    db_name: null,
    db_user: null,
    db_schema: null,
    status: 'awaiting_review',
    current_stage: null,
    error_message: null,
    retry_count: 0,
    discovery_summary: null,
    version: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SemanticReview />
    </QueryClientProvider>,
  )
}

function setUserRole(roles: string[]) {
  useLocalAuthStore.setState({
    status: 'authenticated',
    user: {
      id: 'u1',
      email: 'admin@example.com',
      username: null,
      display_name: 'Admin',
      needs_profile_completion: false,
      status: 'active',
      is_email_verified: true,
      roles,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    },
  } as never)
}

describe('SemanticReview', () => {
  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
    vi.clearAllMocks()
  })

  it('renders the entry list and selecting one shows its detail', async () => {
    setUserRole(['admin'])
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([makeEntry()])
    vi.mocked(catalogApi.getCatalogEntryVersions).mockResolvedValue([])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    renderPage()

    const listButton = await screen.findByRole('button', { name: /customer lifetime value/i })
    await userEvent.click(listButton)

    expect(
      await screen.findByRole('heading', { name: /customer lifetime value/i }),
    ).toBeInTheDocument()
    // The governed-metric card's own fields are present.
    expect(screen.getByText('SUM(Amount)')).toBeInTheDocument()
    expect(screen.getByText(/affected questions: not tracked/i)).toBeInTheDocument()
  })

  it('sorts a conflicting entry ahead of a plain one', async () => {
    setUserRole(['admin'])
    const plain = makeEntry({ id: 'plain', business_name: 'Plain Metric', confidence: 0.95 })
    const conflicting = makeEntry({
      id: 'conflict',
      business_name: 'Conflicting Metric',
      confidence: 0.95,
      conflicting_entry_ids: ['plain'],
      conflicting_entry_names: ['Plain Metric'],
    })
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([plain, conflicting])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    renderPage()

    const buttons = await screen.findAllByRole('button', { name: /metric/i })
    expect(buttons[0]).toHaveTextContent('Conflicting Metric')
  })

  it('shows "+ New concept" for an admin but not for an analyst', async () => {
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    setUserRole(['admin'])
    const { unmount } = renderPage()
    expect(await screen.findByRole('button', { name: /new concept/i })).toBeInTheDocument()
    unmount()

    setUserRole(['analyst'])
    renderPage()
    await waitFor(() => expect(catalogApi.listCatalogEntries).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /new concept/i })).not.toBeInTheDocument()
  })

  it('a draft entry can be approved to reviewed', async () => {
    setUserRole(['analyst'])
    const entry = makeEntry({ status: 'draft' })
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([entry])
    vi.mocked(catalogApi.getCatalogEntryVersions).mockResolvedValue([])
    vi.mocked(catalogApi.reviewCatalogEntry).mockResolvedValue({ ...entry, status: 'reviewed' })
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /customer lifetime value/i }))
    await userEvent.click(await screen.findByRole('button', { name: /confirm \(approve/i }))

    await waitFor(() =>
      expect(catalogApi.reviewCatalogEntry).toHaveBeenCalledWith('entry-1', { notes: null }),
    )
  })

  it('a reviewed entry can be published by an admin but not by an analyst alone', async () => {
    const entry = makeEntry({ status: 'reviewed' })
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([entry])
    vi.mocked(catalogApi.getCatalogEntryVersions).mockResolvedValue([])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    setUserRole(['analyst'])
    const { unmount } = renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /customer lifetime value/i }))
    expect(screen.queryByRole('button', { name: /confirm \(publish/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /request clarification/i })).toBeInTheDocument()
    unmount()

    setUserRole(['admin'])
    vi.mocked(catalogApi.publishCatalogEntry).mockResolvedValue({ ...entry, status: 'published' })
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /customer lifetime value/i }))
    await userEvent.click(await screen.findByRole('button', { name: /confirm \(publish/i }))

    await waitFor(() => expect(catalogApi.publishCatalogEntry).toHaveBeenCalledWith('entry-1'))
  })

  it('request clarification sends a reviewed entry back to draft', async () => {
    setUserRole(['analyst'])
    const entry = makeEntry({ status: 'reviewed' })
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([entry])
    vi.mocked(catalogApi.getCatalogEntryVersions).mockResolvedValue([])
    vi.mocked(catalogApi.requestCatalogEntryChanges).mockResolvedValue({ ...entry, status: 'draft' })
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /customer lifetime value/i }))
    await userEvent.click(await screen.findByRole('button', { name: /request clarification/i }))

    await waitFor(() =>
      expect(catalogApi.requestCatalogEntryChanges).toHaveBeenCalledWith('entry-1', { notes: null }),
    )
  })

  it('defer moves to the next item without calling any API', async () => {
    setUserRole(['analyst'])
    const first = makeEntry({ id: 'a', business_name: 'A Metric' })
    const second = makeEntry({ id: 'b', business_name: 'B Metric' })
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([first, second])
    vi.mocked(catalogApi.getCatalogEntryVersions).mockResolvedValue([])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /a metric/i }))
    expect(await screen.findByRole('heading', { name: /a metric/i })).toBeInTheDocument()

    await userEvent.click(await screen.findByRole('button', { name: /defer/i }))

    expect(await screen.findByRole('heading', { name: /b metric/i })).toBeInTheDocument()
    expect(catalogApi.reviewCatalogEntry).not.toHaveBeenCalled()
    expect(catalogApi.updateCatalogEntry).not.toHaveBeenCalled()
  })

  it('shows onboarding jobs awaiting review with their pending-item count and a link', async () => {
    setUserRole(['analyst'])
    vi.mocked(catalogApi.listCatalogEntries).mockResolvedValue([])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([makeJob()])
    vi.mocked(onboardingApi.listOnboardingReviewItems).mockResolvedValue([
      { id: 'i1', item_type: 'pii_classification', table_name: null, column_name: null, subject: 's', payload: {}, confidence: 0.9, is_ambiguous: false, decision: 'pending', decided_at: null, decision_notes: null, created_at: '2026-01-01T00:00:00Z' },
    ])

    renderPage()

    expect(await screen.findByText('Acme Warehouse')).toBeInTheDocument()
    expect(await screen.findByText(/1 pending/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /open database onboarding/i })).toBeInTheDocument()
  })
})
