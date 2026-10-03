import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as onboardingApi from '@/lib/onboardingApi'
import type { OnboardingJob, OnboardingReviewItem } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { DatabaseOnboarding } from './DatabaseOnboarding'

vi.mock('@/lib/onboardingApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/onboardingApi')>()
  return {
    ...actual,
    listOnboardingJobs: vi.fn(),
    getOnboardingJob: vi.fn(),
    createOnboardingJob: vi.fn(),
    runOnboardingDiscovery: vi.fn(),
    listOnboardingReviewItems: vi.fn(),
    decideOnboardingReviewItem: vi.fn(),
    publishOnboardingJob: vi.fn(),
    cancelOnboardingJob: vi.fn(),
    retryOnboardingJob: vi.fn(),
    listOnboardingArtifacts: vi.fn(),
  }
})

const initialAuthState = useLocalAuthStore.getState()

function makeJob(overrides: Partial<OnboardingJob> = {}): OnboardingJob {
  return {
    id: 'job-1',
    database_label: 'Acme Warehouse',
    db_type: 'mssql',
    db_host: 'db.internal',
    db_port: 1433,
    db_name: 'warehouse',
    db_user: 'svc',
    db_schema: null,
    status: 'pending',
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

function makeReviewItem(overrides: Partial<OnboardingReviewItem> = {}): OnboardingReviewItem {
  return {
    id: 'item-1',
    item_type: 'pii_classification',
    table_name: 'customers',
    column_name: 'ssn',
    subject: 'customers.ssn -> ssn',
    payload: {},
    confidence: 0.9,
    is_ambiguous: false,
    decision: 'pending',
    decided_at: null,
    decision_notes: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <DatabaseOnboarding />
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

describe('DatabaseOnboarding', () => {
  beforeEach(() => {
    vi.mocked(onboardingApi.listOnboardingArtifacts).mockResolvedValue([])
  })

  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
    vi.clearAllMocks()
  })

  it('renders the job list and selecting a job shows its status', async () => {
    setUserRole(['admin'])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([makeJob()])
    vi.mocked(onboardingApi.getOnboardingJob).mockResolvedValue(makeJob())
    vi.mocked(onboardingApi.listOnboardingReviewItems).mockResolvedValue([])

    renderPage()

    const jobButton = await screen.findByRole('button', { name: /acme warehouse/i })
    await userEvent.click(jobButton)

    expect(await screen.findByRole('heading', { name: /acme warehouse/i })).toBeInTheDocument()
  })

  it('shows "+ New job" for an admin but not for a plain user', async () => {
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])

    setUserRole(['admin'])
    const { unmount } = renderPage()
    expect(await screen.findByRole('button', { name: /new job/i })).toBeInTheDocument()
    unmount()

    setUserRole(['user'])
    renderPage()
    await waitFor(() => expect(onboardingApi.listOnboardingJobs).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /new job/i })).not.toBeInTheDocument()
  })

  it('creates a job with the entered connection details and selects it', async () => {
    setUserRole(['admin'])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])
    const createdJob = makeJob({ id: 'job-new', database_label: 'New DB' })
    vi.mocked(onboardingApi.createOnboardingJob).mockResolvedValue(createdJob)
    vi.mocked(onboardingApi.getOnboardingJob).mockResolvedValue(createdJob)
    vi.mocked(onboardingApi.listOnboardingReviewItems).mockResolvedValue([])

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /new job/i }))

    await userEvent.type(screen.getByLabelText(/database label/i), 'New DB')
    await userEvent.type(screen.getByLabelText(/host/i), 'db.example.com')
    await userEvent.type(screen.getByLabelText(/username/i), 'reader')
    await userEvent.type(screen.getByLabelText(/password/i), 'secret123')

    await userEvent.click(screen.getByRole('button', { name: /test connection & create job/i }))

    await waitFor(() =>
      expect(onboardingApi.createOnboardingJob).toHaveBeenCalledWith(
        expect.objectContaining({
          database_label: 'New DB',
          db_type: 'mssql',
          db_host: 'db.example.com',
          db_user: 'reader',
          db_password: 'secret123',
        }),
        expect.anything(),
      ),
    )
    expect(await screen.findByRole('heading', { name: /new db/i })).toBeInTheDocument()
  })

  it('shows a create-job API error without crashing', async () => {
    setUserRole(['admin'])
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([])
    vi.mocked(onboardingApi.createOnboardingJob).mockRejectedValue(new Error('connection refused'))

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /new job/i }))
    await userEvent.type(screen.getByLabelText(/database label/i), 'New DB')
    await userEvent.click(screen.getByRole('button', { name: /test connection & create job/i }))

    expect(await screen.findByText(/could not create the onboarding job/i)).toBeInTheDocument()
  })

  it('renders review items grouped by type and lets an admin confirm one', async () => {
    setUserRole(['admin'])
    const job = makeJob({ status: 'awaiting_review', discovery_summary: { table_count: 2 } })
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([job])
    vi.mocked(onboardingApi.getOnboardingJob).mockResolvedValue(job)
    const piiItem = makeReviewItem({ id: 'pii-1' })
    vi.mocked(onboardingApi.listOnboardingReviewItems).mockResolvedValue([piiItem])
    vi.mocked(onboardingApi.decideOnboardingReviewItem).mockResolvedValue({
      ...piiItem,
      decision: 'confirmed',
    })

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /acme warehouse/i }))

    const piiTab = await screen.findByRole('tab', { name: /pii candidates \(1\)/i })
    expect(piiTab).toBeInTheDocument()
    await userEvent.click(piiTab)

    const row = (await screen.findByText('customers.ssn -> ssn')).closest('tr')
    expect(row).not.toBeNull()

    await userEvent.click(within(row as HTMLElement).getByRole('button', { name: /confirm/i }))

    await waitFor(() =>
      expect(onboardingApi.decideOnboardingReviewItem).toHaveBeenCalledWith(
        job.id,
        'pii-1',
        { decision: 'confirmed' },
      ),
    )
  })

  it('disables Publish while review items are still pending', async () => {
    setUserRole(['admin'])
    const job = makeJob({ status: 'awaiting_review', discovery_summary: { table_count: 1 } })
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([job])
    vi.mocked(onboardingApi.getOnboardingJob).mockResolvedValue(job)
    vi.mocked(onboardingApi.listOnboardingReviewItems).mockResolvedValue([makeReviewItem()])

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /acme warehouse/i }))

    const publishButton = await screen.findByRole('button', { name: /^publish$/i })
    expect(publishButton).toBeDisabled()
  })

  it('enables Publish once every review item is decided', async () => {
    setUserRole(['admin'])
    const job = makeJob({ status: 'awaiting_review', discovery_summary: { table_count: 1 } })
    vi.mocked(onboardingApi.listOnboardingJobs).mockResolvedValue([job])
    vi.mocked(onboardingApi.getOnboardingJob).mockResolvedValue(job)
    vi.mocked(onboardingApi.listOnboardingReviewItems).mockResolvedValue([
      makeReviewItem({ decision: 'confirmed' }),
    ])

    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /acme warehouse/i }))

    const publishButton = await screen.findByRole('button', { name: /^publish$/i })
    expect(publishButton).not.toBeDisabled()
  })
})
