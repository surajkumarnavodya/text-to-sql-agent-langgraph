import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as recommendationApi from '@/lib/recommendationApi'
import { ApiError } from '@/lib/api'
import * as tenantAdminApi from '@/lib/tenantAdminApi'
import type { RecommendationRecordOut } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'
import * as navigationApi from '@/lib/navigationApi'
import { serveNavigation } from '@/test/navigationFixtures'
import { Recommendations } from './Recommendations'

vi.mock('@/lib/recommendationApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/recommendationApi')>()
  return {
    ...actual,
    listRecommendations: vi.fn(),
    listRecommendationEvents: vi.fn(),
    submitRecommendationVerdict: vi.fn(),
    resolveRecommendation: vi.fn(),
    expireRecommendation: vi.fn(),
    addRecommendationNote: vi.fn(),
    assignRecommendationOwner: vi.fn(),
  }
})

vi.mock('@/lib/tenantAdminApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/tenantAdminApi')>()
  return { ...actual, listTenantUsers: vi.fn() }
})

vi.mock('@/lib/navigationApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/navigationApi')>()
  return { ...actual, getNavigation: vi.fn(), reportAccessDenied: vi.fn() }
})

beforeEach(() => serveNavigation(vi.mocked(navigationApi.getNavigation), null))

const initialAuthState = useLocalAuthStore.getState()
const ME = 'me-user-id'

function setUser(roles: string[]) {
  useLocalAuthStore.setState({
    status: 'authenticated',
    user: {
      id: ME,
      email: 'reviewer@example.com',
      username: null,
      display_name: 'Riley Reviewer',
      needs_profile_completion: false,
      status: 'active',
      is_email_verified: true,
      roles,
      created_at: '2026-01-01T00:00:00Z',
      last_login_at: null,
    },
  } as never)
  serveNavigation(vi.mocked(navigationApi.getNavigation), roles)
}

function record(overrides: Partial<RecommendationRecordOut> = {}): RecommendationRecordOut {
  return {
    id: 'rec-1',
    tenant_id: 'tenant-a',
    database_id: 'hr',
    category: 'anomaly',
    kind: 'action',
    rule_or_model: 'recommendation.engine.AnomalyRule',
    claim_text: 'Investigate the 2024 spike in refunds.',
    rationale: 'It is far above the rolling baseline.',
    affected_entity: '2024',
    action: 'Review refund policy changes.',
    measurable_impact: null,
    confidence: 0.8,
    evidence: [
      {
        value: 'Refunds in 2024 were 4.2x the trailing average.',
        level: 'database_fact',
        grounded_in: [],
        source: 'analytics.engine',
      },
    ],
    limitations: ['Based on one period of history.'],
    engine_version: '1.0.0',
    evidence_version: 'ev1',
    status: 'generated',
    generated_at: '2026-10-03T09:00:00Z',
    source_question: 'why did refunds jump?',
    source_sql: 'SELECT 1',
    created_at: '2026-10-03T09:00:00Z',
    updated_at: '2026-10-03T09:00:00Z',
    owner_user_id: null,
    owner_display_name: null,
    ...overrides,
  }
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Recommendations />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
  useLocalAuthStore.setState(initialAuthState)
})

describe('Recommendations page -- evidence and honest labelling', () => {
  it('labels every recommendation as an AI estimate, never as a fact', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    const detail = await screen.findByTestId('recommendation-detail')
    expect(within(detail).getByText(/AI-generated suggestion, not a confirmed fact/)).toBeInTheDocument()
    expect(within(detail).getAllByText('AI estimate').length).toBeGreaterThan(0)
  })

  it('shows each evidence claim with its own truth level', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    expect(await screen.findByText('Refunds in 2024 were 4.2x the trailing average.')).toBeInTheDocument()
    expect(screen.getByText('Computed from data')).toBeInTheDocument()
  })

  it('warns when a recommendation has no recorded evidence', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record({ evidence: [] })])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    expect(
      await screen.findByText(/No supporting evidence is recorded for this item/),
    ).toBeInTheDocument()
  })

  it('says a measurable impact is not measured rather than inventing one', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record({ measurable_impact: null })])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    expect(await screen.findByText('Not measured by the engine for this item.')).toBeInTheDocument()
  })

  it('shows a real measurable impact when the engine supplied one', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([
      record({ measurable_impact: 'Affects 12.4% of rows' }),
    ])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    expect(await screen.findByText('Affects 12.4% of rows')).toBeInTheDocument()
  })
})

describe('Recommendations page -- lifecycle actions', () => {
  it('records an accept verdict with the typed reason', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    vi.mocked(recommendationApi.submitRecommendationVerdict).mockResolvedValue(record({ status: 'accepted' }))
    renderPage()
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText(/Reason \(optional/), 'Matches finance numbers.')
    await user.click(screen.getByRole('button', { name: 'Accept' }))

    await waitFor(() =>
      expect(recommendationApi.submitRecommendationVerdict).toHaveBeenCalledWith('rec-1', {
        status: 'accepted',
        reason: 'Matches finance numbers.',
      }),
    )
  })

  it('offers partially useful and mark incorrect as separate verdicts', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    expect(await screen.findByRole('button', { name: 'Partially useful' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mark incorrect' })).toBeInTheDocument()
  })

  it('offers resolve only once the record has been accepted', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record({ status: 'generated' })])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    await screen.findByTestId('recommendation-detail')
    expect(screen.queryByRole('button', { name: 'Mark resolved' })).not.toBeInTheDocument()
  })

  it('offers resolve on an accepted record', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record({ status: 'accepted' })])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    expect(await screen.findByRole('button', { name: 'Mark resolved' })).toBeInTheDocument()
  })

  it('shows the server refusal message when a transition is no longer valid', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    vi.mocked(recommendationApi.submitRecommendationVerdict).mockRejectedValue(
      new ApiError('Recommendation is no longer generated -- another decision was already recorded.', 409),
    )
    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Accept' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('another decision was already recorded')
  })

  it('adds a note through the note endpoint', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    vi.mocked(recommendationApi.addRecommendationNote).mockResolvedValue({} as never)
    renderPage()
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Note'), 'Checked with finance.')
    await user.click(screen.getByRole('button', { name: 'Add note' }))

    await waitFor(() =>
      expect(recommendationApi.addRecommendationNote).toHaveBeenCalledWith('rec-1', 'Checked with finance.'),
    )
  })

  it('assigns the record to the signed-in reviewer', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    vi.mocked(recommendationApi.assignRecommendationOwner).mockResolvedValue(record({ owner_user_id: ME }))
    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Assign to me' }))

    await waitFor(() => expect(recommendationApi.assignRecommendationOwner).toHaveBeenCalledWith('rec-1', ME))
  })

  it('does not show Expire to an analyst, who lacks the manage permission', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    await screen.findByTestId('recommendation-detail')
    expect(screen.queryByRole('button', { name: 'Expire' })).not.toBeInTheDocument()
  })

  it('shows Expire to an admin on a non-terminal record', async () => {
    setUser(['admin'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    vi.mocked(tenantAdminApi.listTenantUsers).mockResolvedValue([])
    renderPage()

    expect(await screen.findByRole('button', { name: 'Expire' })).toBeInTheDocument()
  })

  it('never shows decision controls on a terminal record', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record({ status: 'rejected' })])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([])
    renderPage()

    await screen.findByTestId('recommendation-detail')
    expect(screen.queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()
    expect(screen.getByText('No further decisions are available for this status.')).toBeInTheDocument()
  })
})

describe('Recommendations page -- roles, filters and history', () => {
  it('shows a plain user a clear role message instead of an error dump', async () => {
    setUser(['user'])
    vi.mocked(recommendationApi.listRecommendations).mockRejectedValue(new ApiError('Not permitted.', 403))
    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent('Your role cannot review recommendations')
  })

  it('passes the category filter through to the API', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([])
    renderPage()
    const user = userEvent.setup()

    await user.selectOptions(await screen.findByLabelText('Category'), 'security')

    await waitFor(() =>
      expect(recommendationApi.listRecommendations).toHaveBeenLastCalledWith(
        expect.objectContaining({ category: 'security' }),
      ),
    )
  })

  it('"Assigned to me" sends the signed-in user id, never a client-chosen tenant', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([])
    renderPage()
    const user = userEvent.setup()

    await user.selectOptions(await screen.findByLabelText('Owner'), 'mine')

    await waitFor(() =>
      expect(recommendationApi.listRecommendations).toHaveBeenLastCalledWith(
        expect.objectContaining({ ownerUserId: ME }),
      ),
    )
    const lastFilters = vi.mocked(recommendationApi.listRecommendations).mock.lastCall?.[0] ?? {}
    expect(lastFilters).not.toHaveProperty('tenantId')
  })

  it('renders the audit trail, distinguishing notes from status changes', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([
      {
        id: 'e1',
        recommendation_id: 'rec-1',
        from_status: null,
        to_status: 'generated',
        event_type: 'status_change',
        actor_user_id: null,
        actor_label: 'system:recommendation_engine',
        reason: null,
        recommendation_version: '1.0.0',
        evidence_version: 'ev1',
        created_at: '2026-10-03T09:00:00Z',
      },
      {
        id: 'e2',
        recommendation_id: 'rec-1',
        from_status: 'generated',
        to_status: 'generated',
        event_type: 'note',
        actor_user_id: ME,
        actor_label: null,
        reason: 'Checked with finance.',
        recommendation_version: '1.0.0',
        evidence_version: 'ev1',
        created_at: '2026-10-03T09:05:00Z',
      },
    ])
    renderPage()

    expect(await screen.findByText('Note added')).toBeInTheDocument()
    expect(screen.getByText('Checked with finance.')).toBeInTheDocument()
    expect(screen.getByText('Generated')).toBeInTheDocument()
  })

  it('a note authored by someone else is attributed generically, not by identity', async () => {
    setUser(['analyst'])
    vi.mocked(recommendationApi.listRecommendations).mockResolvedValue([record()])
    vi.mocked(recommendationApi.listRecommendationEvents).mockResolvedValue([
      {
        id: 'e3',
        recommendation_id: 'rec-1',
        from_status: 'generated',
        to_status: 'generated',
        event_type: 'note',
        actor_user_id: 'someone-else',
        actor_label: null,
        reason: 'Their note.',
        recommendation_version: '1.0.0',
        evidence_version: 'ev1',
        created_at: '2026-10-03T09:05:00Z',
      },
    ])
    renderPage()

    expect(await screen.findByText(/a reviewer/)).toBeInTheDocument()
  })
})
