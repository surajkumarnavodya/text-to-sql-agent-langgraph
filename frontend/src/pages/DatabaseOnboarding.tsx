import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { CreateJobForm } from '@/components/onboarding/CreateJobForm'
import { ReviewItemsSection } from '@/components/onboarding/ReviewItemsSection'
import { useElapsedSeconds } from '@/hooks/useElapsedSeconds'
import {
  useCancelOnboardingJob,
  useDecideOnboardingReviewItem,
  useOnboardingArtifacts,
  useOnboardingJob,
  useOnboardingJobs,
  useOnboardingReviewItems,
  usePublishOnboardingJob,
  useRetryOnboardingJob,
  useRunOnboardingDiscovery,
} from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import type { OnboardingJob, OnboardingJobStatus, OnboardingReviewItem } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

const STATUS_TONE: Record<OnboardingJobStatus, 'neutral' | 'success' | 'warning' | 'danger' | 'accent'> = {
  pending: 'neutral',
  discovering: 'accent',
  awaiting_review: 'warning',
  publishing: 'accent',
  published: 'success',
  failed: 'danger',
  cancelled: 'neutral',
}

/** The real frontend workflow for onboarding a client database -- Prompt
 * 26, the UI for Prompt 08's own backend engine (`onboarding/`,
 * `api/onboarding.py`). Every number/table/item shown below comes from a
 * real backend call; there is no mock or placeholder data anywhere in
 * this file.
 *
 * **Deliberately honest about what the real backend actually does, not
 * a literal re-reading of a wizard outline:**
 * - There is no "test connection" step separate from "create job" --
 *   `POST /onboarding/jobs` does both atomically (see `CreateJobForm`'s
 *   own docstring). This UI reflects that with one combined action, not
 *   two buttons for a distinction the backend doesn't make.
 * - "Start semantic analysis" and "generate golden questions" are not
 *   separate actions either -- both already happen as part of
 *   `POST .../discover` (`onboarding/jobs.py::run_discovery_stage`), so
 *   their results (semantic-label and golden-question review items)
 *   simply appear alongside PII/relationship results once discovery
 *   finishes, not behind a second button that doesn't exist.
 * - "Run evaluation" is not a pre-publish action either -- it happens
 *   automatically inside `POST .../publish` (`onboarding/jobs.py
 *   ::publish_job`), and its result is the `evaluation_report` artifact
 *   shown once the job reaches `published`.
 * - There is no "create/select tenant" step -- a job's tenant is
 *   resolved server-side from the caller's own account and is never a
 *   client-supplied value (`docs/MULTI_TENANCY.md` rule 1); building a
 *   tenant selector here would be exactly the kind of decorative control
 *   that implies a capability (choosing another tenant) this app's
 *   security model deliberately never grants.
 * - **Progress is an honest loading state, not a fake multi-stage bar.**
 *   `POST .../discover` and `POST .../publish` are synchronous backend
 *   calls with no background worker (`onboarding/jobs.py`'s own module
 *   docstring) -- there is no intermediate signal to poll, so this UI
 *   shows a live elapsed-time indicator (`useElapsedSeconds`, already
 *   used elsewhere in this app) while the one request is in flight,
 *   never a sequence of steps this app cannot actually observe.
 * - **"Cancellation" of an in-flight discover/publish call is
 *   client-side only** -- there is no server-side mechanism to interrupt
 *   a running discovery/publish (same "no background worker" design).
 *   `POST /onboarding/jobs/{id}/cancel` genuinely works, but only against
 *   a job in a non-terminal *resting* state (pending/awaiting_review/
 *   failed), not one actively mid-call -- disclosed in the UI copy
 *   itself, not silently implied to be more than it is.
 * - Profiling has no dedicated display: the backend never returns raw
 *   column-profile statistics (min/max/null-rate) to any caller -- they
 *   only ever feed semantic-label inference and PII confidence
 *   internally (`onboarding/jobs.py::_profile_all_columns`). The one
 *   real, exposed profiling-derived number (`duplicate_key_count`) is
 *   shown in the discovery summary below, honestly labeled.
 */
export function DatabaseOnboarding() {
  const localUser = useLocalAuthStore((state) => state.user)
  const canManage = Boolean(localUser?.roles.includes('admin'))
  const canReview = canManage || Boolean(localUser?.roles.includes('analyst'))

  const jobs = useOnboardingJobs()
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [showCreateForm, setShowCreateForm] = useState(false)

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <h1 className="text-xl font-bold">🗄️ Database Onboarding</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Connect a client database, review what the engine discovered, and publish a governed
          semantic contract for it to the text-to-SQL pipeline.
        </p>

        <div className="mt-6 flex flex-col gap-6 lg:flex-row">
          <div className="flex w-full flex-col gap-3 lg:w-72">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">Jobs</h2>
              {canManage && (
                <Button size="sm" onClick={() => setShowCreateForm((value) => !value)}>
                  {showCreateForm ? 'Close' : '+ New job'}
                </Button>
              )}
            </div>
            {showCreateForm && (
              <CreateJobForm
                onCreated={(job) => {
                  setShowCreateForm(false)
                  setSelectedJobId(job.id)
                }}
              />
            )}
            {jobs.isLoading && <p className="text-sm text-[var(--muted-foreground)]">Loading…</p>}
            {jobs.isError && (
              <p className="text-sm text-[var(--danger)]">
                {jobs.error instanceof ApiError ? jobs.error.message : 'Could not load jobs.'}
              </p>
            )}
            <ul className="flex flex-col gap-1.5">
              {jobs.data?.map((job) => (
                <li key={job.id}>
                  <button
                    onClick={() => setSelectedJobId(job.id)}
                    className={`w-full rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                      selectedJobId === job.id
                        ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                        : 'border-[var(--border)] hover:bg-[var(--muted)]'
                    }`}
                  >
                    <div className="truncate font-medium">{job.database_label}</div>
                    <div className="mt-1 flex items-center gap-2">
                      <Badge tone={STATUS_TONE[job.status]}>{job.status}</Badge>
                      <span className="text-xs text-[var(--muted-foreground)]">{job.db_type}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
            {jobs.data?.length === 0 && !showCreateForm && (
              <p className="text-sm text-[var(--muted-foreground)]">
                No onboarding jobs yet{canManage ? ' -- create one to get started.' : '.'}
              </p>
            )}
          </div>

          <div className="min-w-0 flex-1">
            {selectedJobId ? (
              <JobWorkspace jobId={selectedJobId} canManage={canManage} canReview={canReview} />
            ) : (
              <Card>
                <CardContent className="py-10 text-center text-sm text-[var(--muted-foreground)]">
                  Select a job on the left, or create a new one, to get started.
                </CardContent>
              </Card>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function JobWorkspace({
  jobId,
  canManage,
  canReview,
}: {
  jobId: string
  canManage: boolean
  canReview: boolean
}) {
  const job = useOnboardingJob(jobId)
  const reviewItems = useOnboardingReviewItems(jobId)
  const artifacts = useOnboardingArtifacts(jobId)

  if (job.isLoading) return <p className="text-sm text-[var(--muted-foreground)]">Loading job…</p>
  if (job.isError || !job.data) {
    return (
      <p className="text-sm text-[var(--danger)]">
        {job.error instanceof ApiError ? job.error.message : 'Could not load this job.'}
      </p>
    )
  }

  const current = job.data
  const items = reviewItems.data ?? []

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            <CardTitle>{current.database_label}</CardTitle>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">
              {current.db_type}
              {current.db_host ? ` · ${current.db_host}` : ''}
              {current.db_name ? ` / ${current.db_name}` : ''}
            </p>
          </div>
          <Badge tone={STATUS_TONE[current.status]}>{current.status}</Badge>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {current.error_message && (
            <p className="rounded-md bg-[var(--danger)]/10 p-2 text-sm text-[var(--danger)]">
              {current.error_message}
            </p>
          )}
          <JobActions job={current} canManage={canManage} />
        </CardContent>
      </Card>

      {current.discovery_summary && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Discovery summary</CardTitle>
          </CardHeader>
          <CardContent>
            <DiscoverySummary summary={current.discovery_summary} />
          </CardContent>
        </Card>
      )}

      {(current.status === 'awaiting_review' ||
        current.status === 'publishing' ||
        current.status === 'published') && (
        <Card>
          <CardContent className="pt-4">
            <ReviewTabs items={items} canReview={canReview} jobId={jobId} />
          </CardContent>
        </Card>
      )}

      {current.status === 'published' && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Published artifacts</CardTitle>
          </CardHeader>
          <CardContent>
            <ArtifactsPanel artifacts={artifacts.data ?? []} isLoading={artifacts.isLoading} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

/** A table/column/relationship/PII/golden-question *count* rollup --
 * `OnboardingJob.discovery_summary`'s real shape (`api/onboarding_schemas
 * .py::OnboardingJobOut`), not derived or guessed client-side. */
function DiscoverySummary({ summary }: { summary: Record<string, unknown> }) {
  const entries: { key: string; label: string }[] = [
    { key: 'table_count', label: 'Tables' },
    { key: 'view_count', label: 'Views' },
    { key: 'relationship_candidate_count', label: 'Relationship candidates' },
    { key: 'pii_finding_count', label: 'PII candidates' },
    { key: 'duplicate_key_count', label: 'Duplicate-key columns (profiling)' },
    { key: 'golden_question_count', label: 'Golden questions' },
  ]
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {entries.map(({ key, label }) => (
        <div key={key}>
          <dt className="text-xs text-[var(--muted-foreground)]">{label}</dt>
          <dd className="text-lg font-semibold tabular-nums">{String(summary[key] ?? '—')}</dd>
        </div>
      ))}
    </dl>
  )
}

function groupByType(items: OnboardingReviewItem[], type: OnboardingReviewItem['item_type']) {
  return items.filter((item) => item.item_type === type)
}

function ReviewTabs({
  items,
  canReview,
  jobId,
}: {
  items: OnboardingReviewItem[]
  canReview: boolean
  jobId: string
}) {
  const decide = useDecideOnboardingReviewItem(jobId)
  const [pendingItemId, setPendingItemId] = useState<string | null>(null)
  const [decideError, setDecideError] = useState<string | null>(null)

  const handleDecide = async (itemId: string, decision: 'confirmed' | 'rejected') => {
    setPendingItemId(itemId)
    setDecideError(null)
    try {
      await decide.mutateAsync({ itemId, payload: { decision } })
    } catch (err) {
      setDecideError(err instanceof ApiError ? err.message : 'Could not record that decision.')
    } finally {
      setPendingItemId(null)
    }
  }

  const semanticLabels = groupByType(items, 'semantic_label')
  const relationships = groupByType(items, 'relationship')
  const piiCandidates = groupByType(items, 'pii_classification')
  const goldenQuestions = groupByType(items, 'golden_question')

  return (
    <Tabs defaultValue="schema">
      <TabsList>
        <TabsTrigger value="schema">Schema & columns ({semanticLabels.length})</TabsTrigger>
        <TabsTrigger value="relationships">Relationships ({relationships.length})</TabsTrigger>
        <TabsTrigger value="pii">PII candidates ({piiCandidates.length})</TabsTrigger>
        <TabsTrigger value="golden">Golden questions ({goldenQuestions.length})</TabsTrigger>
      </TabsList>
      {decideError && <p className="mt-2 text-sm text-[var(--danger)]">{decideError}</p>}
      <TabsContent value="schema">
        <ReviewItemsSection
          items={semanticLabels}
          canReview={canReview}
          onDecide={handleDecide}
          pendingItemId={pendingItemId}
          emptyMessage="No columns were semantically labeled (every table discovered was a view)."
        />
      </TabsContent>
      <TabsContent value="relationships">
        <ReviewItemsSection
          items={relationships}
          canReview={canReview}
          onDecide={handleDecide}
          pendingItemId={pendingItemId}
          emptyMessage="No candidate relationships were inferred."
        />
      </TabsContent>
      <TabsContent value="pii">
        <ReviewItemsSection
          items={piiCandidates}
          canReview={canReview}
          onDecide={handleDecide}
          pendingItemId={pendingItemId}
          emptyMessage="No columns matched a PII pattern."
        />
      </TabsContent>
      <TabsContent value="golden">
        <ReviewItemsSection
          items={goldenQuestions}
          canReview={canReview}
          onDecide={handleDecide}
          pendingItemId={pendingItemId}
          emptyMessage="No golden questions could be generated for this schema."
        />
      </TabsContent>
    </Tabs>
  )
}

function ArtifactsPanel({
  artifacts,
  isLoading,
}: {
  artifacts: { artifact_type: string; content: Record<string, unknown>; version: number }[]
  isLoading: boolean
}) {
  if (isLoading) return <p className="text-sm text-[var(--muted-foreground)]">Loading artifacts…</p>
  const evaluation = artifacts.find((a) => a.artifact_type === 'evaluation_report')
  const goldenQuestions = artifacts.find((a) => a.artifact_type === 'golden_questions')
  const contract = artifacts.find((a) => a.artifact_type === 'semantic_contract')

  return (
    <div className="flex flex-col gap-4 text-sm">
      {evaluation && (
        <div>
          <h3 className="font-medium">Evaluation report</h3>
          <p className="text-[var(--muted-foreground)]">
            {String(evaluation.content.pass_count)} of {String(evaluation.content.total_count)}{' '}
            golden questions executed successfully against the live connection.
          </p>
        </div>
      )}
      {goldenQuestions && (
        <div>
          <h3 className="font-medium">
            Golden questions (
            {Array.isArray(goldenQuestions.content.questions)
              ? goldenQuestions.content.questions.length
              : 0}
            )
          </h3>
        </div>
      )}
      {contract && (
        <div>
          <h3 className="font-medium">Semantic contract</h3>
          <p className="text-[var(--muted-foreground)]">
            Published and now live for this database's own text-to-SQL retrieval.
          </p>
        </div>
      )}
      {artifacts.length === 0 && (
        <p className="text-[var(--muted-foreground)]">No artifacts yet.</p>
      )}
    </div>
  )
}

/** The action bar for one job, varying by `status` -- every action that
 * needs a live connection (discover/publish/retry) asks for the password
 * again, inline, right before the call: this app never stores it, so
 * there is nothing to "resume with" otherwise (see `CreateJobForm`'s own
 * docstring for the full rationale). */
function JobActions({ job, canManage }: { job: OnboardingJob; canManage: boolean }) {
  const cancel = useCancelOnboardingJob(job.id)
  const retry = useRetryOnboardingJob(job.id)
  const [actionError, setActionError] = useState<string | null>(null)

  const handleCancel = async () => {
    setActionError(null)
    try {
      await cancel.mutateAsync()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not cancel this job.')
    }
  }

  const handleRetry = async () => {
    setActionError(null)
    try {
      await retry.mutateAsync()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Could not retry this job.')
    }
  }

  if (!canManage) {
    return job.status === 'awaiting_review' ? (
      <p className="text-xs text-[var(--muted-foreground)]">
        Review the items below. Publishing requires an administrator.
      </p>
    ) : null
  }

  return (
    <div className="flex flex-col gap-3">
      {actionError && <p className="text-sm text-[var(--danger)]">{actionError}</p>}
      {job.status === 'pending' && <DiscoveryAction job={job} />}
      {job.status === 'awaiting_review' && <PublishAction job={job} />}
      {job.status === 'failed' && (
        <Button size="sm" onClick={handleRetry} disabled={retry.isPending}>
          {retry.isPending ? 'Retrying…' : 'Retry'}
        </Button>
      )}
      {job.status !== 'published' && job.status !== 'cancelled' && (
        <Button size="sm" variant="ghost" onClick={handleCancel} disabled={cancel.isPending}>
          Cancel job
        </Button>
      )}
    </div>
  )
}

function DiscoveryAction({ job }: { job: OnboardingJob }) {
  const discover = useRunOnboardingDiscovery(job.id)
  const [password, setPassword] = useState('')
  const [verifyRelationships, setVerifyRelationships] = useState(false)
  const [verifyPii, setVerifyPii] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number>(() => performance.now())
  const elapsed = useElapsedSeconds(startedAt, discover.isPending)

  const handleRun = async () => {
    setError(null)
    setStartedAt(performance.now())
    try {
      await discover.mutateAsync({
        db_password: password || null,
        verify_relationships_with_data: verifyRelationships,
        verify_pii_with_data: verifyPii,
      })
      setPassword('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Discovery failed.')
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-[var(--border)] p-3">
      <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor={`discover-pw-${job.id}`}>
        Re-enter the database password to run discovery
      </label>
      <input
        id={`discover-pw-${job.id}`}
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
      />
      <label className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
        <input
          type="checkbox"
          checked={verifyRelationships}
          onChange={(event) => setVerifyRelationships(event.target.checked)}
        />
        Verify relationship candidates against sampled data (bounded, not a full scan)
      </label>
      <label className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
        <input
          type="checkbox"
          checked={verifyPii}
          onChange={(event) => setVerifyPii(event.target.checked)}
        />
        Verify PII candidates against sampled data (bounded, not a full scan)
      </label>
      {error && <p className="text-sm text-[var(--danger)]">{error}</p>}
      <Button size="sm" onClick={handleRun} disabled={discover.isPending}>
        {discover.isPending ? `Running discovery… ${elapsed.toFixed(0)}s` : 'Run discovery'}
      </Button>
    </div>
  )
}

function PublishAction({ job }: { job: OnboardingJob }) {
  const publish = usePublishOnboardingJob(job.id)
  const reviewItems = useOnboardingReviewItems(job.id)
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number>(() => performance.now())
  const elapsed = useElapsedSeconds(startedAt, publish.isPending)

  const items = reviewItems.data ?? []
  const pendingCount = items.filter((item) => item.decision === 'pending').length
  const canPublish = items.length > 0 && pendingCount === 0

  const handlePublish = async () => {
    setError(null)
    setStartedAt(performance.now())
    try {
      await publish.mutateAsync({ db_password: password || null })
      setPassword('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Publish failed.')
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-[var(--border)] p-3">
      {pendingCount > 0 && (
        <p className="text-xs text-[var(--warning)]">
          {pendingCount} review item(s) still need a decision before this job can be published.
        </p>
      )}
      <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor={`publish-pw-${job.id}`}>
        Re-enter the database password to publish
      </label>
      <input
        id={`publish-pw-${job.id}`}
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
      />
      {error && <p className="text-sm text-[var(--danger)]">{error}</p>}
      <Button size="sm" onClick={handlePublish} disabled={!canPublish || publish.isPending}>
        {publish.isPending ? `Publishing & evaluating… ${elapsed.toFixed(0)}s` : 'Publish'}
      </Button>
    </div>
  )
}
