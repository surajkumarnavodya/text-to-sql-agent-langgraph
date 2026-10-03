import { AlertTriangle } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { TruthLevelBadge } from '@/components/analytics/TruthLevelBadge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import {
  useAddRecommendationNote,
  useAssignRecommendationOwner,
  useExpireRecommendation,
  useRecommendationEvents,
  useResolveRecommendation,
  useSubmitRecommendationVerdict,
  useTenantUsers,
} from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import {
  canExpire,
  canRecordVerdict,
  canResolve,
  CATEGORY_LABELS,
  describeEvent,
  formatConfidence,
  formatWhen,
  ownerLabel,
  STATUS_LABELS,
  statusTone,
} from '@/lib/recommendationDisplay'
import type { RecommendationRecordOut, RecommendationVerdict } from '@/lib/types'

/** Only the one-line bar above these sections is a claim the platform makes
 * about itself. Everything below is either a database fact, a reviewer's own
 * judgment, or an explicitly labelled estimate. */
const AI_ESTIMATE_NOTICE =
  'This is an AI-generated suggestion, not a confirmed fact. Check the evidence below before acting on it.'

const VERDICTS: { value: RecommendationVerdict; label: string }[] = [
  { value: 'accepted', label: 'Accept' },
  { value: 'partially_useful', label: 'Partially useful' },
  { value: 'rejected', label: 'Reject' },
  { value: 'incorrect', label: 'Mark incorrect' },
]

/** The detail panel for one recommendation -- claim, provenance, evidence,
 * governed actions, ownership, and the append-only audit trail (Prompt 31).
 *
 * Every action here is a real API call. Buttons are shown only where the
 * lifecycle allows them, but the server still checks each request. Button
 * visibility is UX, never a security boundary. */
export function RecommendationDetail({
  record,
  currentUserId,
  canReview,
  canManage,
}: {
  record: RecommendationRecordOut
  currentUserId: string | null
  canReview: boolean
  canManage: boolean
}) {
  const events = useRecommendationEvents(record.id)
  const verdict = useSubmitRecommendationVerdict()
  const resolve = useResolveRecommendation()
  const expire = useExpireRecommendation()
  const note = useAddRecommendationNote()
  const assign = useAssignRecommendationOwner()

  const [reason, setReason] = useState('')
  const [noteText, setNoteText] = useState('')
  const [error, setError] = useState<string | null>(null)

  const busy = verdict.isPending || resolve.isPending || expire.isPending || note.isPending || assign.isPending

  const run = async (action: () => Promise<unknown>, onDone?: () => void) => {
    setError(null)
    try {
      await action()
      onDone?.()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That action could not be completed.')
    }
  }

  const ownedByMe = currentUserId !== null && record.owner_user_id === currentUserId

  return (
    <div className="flex flex-col gap-4" data-testid="recommendation-detail">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{record.claim_text}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="flex items-start gap-2 rounded-md bg-[var(--muted)] p-3 text-sm">
            <Badge tone="warning" data-truth-level="ai_inference" className="shrink-0">
              AI estimate
            </Badge>
            <span>{AI_ESTIMATE_NOTICE}</span>
          </p>

          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <Meta label="Status">
              <Badge tone={statusTone(record.status)}>{STATUS_LABELS[record.status]}</Badge>
            </Meta>
            <Meta label="Category">
              {record.category
                ? CATEGORY_LABELS[record.category as keyof typeof CATEGORY_LABELS] ?? record.category
                : 'Uncategorized'}
            </Meta>
            <Meta label="Confidence">
              {formatConfidence(record.confidence)}
              <span className="ml-1 text-xs text-[var(--muted-foreground)]">(rule-computed score, not a probability)</span>
            </Meta>
            <Meta label="Rule or model">{record.rule_or_model ?? 'Not recorded'}</Meta>
            <Meta label="Affected entity">{record.affected_entity ?? 'Not specified'}</Meta>
            <Meta label="Database">{record.database_id}</Meta>
            <Meta label="Recommended action">{record.action ?? 'Review and decide'}</Meta>
            <Meta label="Measurable impact">
              {record.measurable_impact ?? 'Not measured by the engine for this item.'}
            </Meta>
            <Meta label="Created">{formatWhen(record.created_at)}</Meta>
            <Meta label="Owner">{ownerLabel(record)}</Meta>
          </dl>

          {record.rationale && (
            <p className="text-sm text-[var(--muted-foreground)]">
              <span className="font-medium text-[var(--foreground)]">Why: </span>
              {record.rationale}
            </p>
          )}
          {record.source_question && (
            <p className="text-xs text-[var(--muted-foreground)]">
              Raised by the question: <span className="italic">&ldquo;{record.source_question}&rdquo;</span>
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Evidence</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {record.evidence.length === 0 ? (
            <p className="flex items-center gap-2 text-sm text-[var(--warning)]">
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
              No supporting evidence is recorded for this item. Treat it as unverified.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {record.evidence.map((item, index) => (
                <li key={`${index}-${item.value}`} className="rounded-md border border-[var(--border)] p-3 text-sm">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <TruthLevelBadge level={item.level} />
                    {item.source && <span className="text-xs text-[var(--muted-foreground)]">{item.source}</span>}
                  </div>
                  {/* Plain text only: evidence values can carry database content,
                   * which is untrusted and must never be rendered as markup. */}
                  <p>{item.value}</p>
                </li>
              ))}
            </ul>
          )}
          {record.limitations.length > 0 && (
            <div>
              <p className="text-xs font-medium text-[var(--muted-foreground)]">Limitations</p>
              <ul className="mt-1 list-disc pl-5 text-sm text-[var(--muted-foreground)]">
                {record.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            </div>
          )}
        </CardContent>
      </Card>

      {canReview && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Decide</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="rec-reason">
              Reason (optional, recorded with the decision)
            </label>
            <textarea
              id="rec-reason"
              value={reason}
              maxLength={4000}
              onChange={(event) => setReason(event.target.value)}
              className="min-h-16 w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm"
            />
            <div className="flex flex-wrap gap-2">
              {canRecordVerdict(record.status) &&
                VERDICTS.map((option) => (
                  <Button
                    key={option.value}
                    size="sm"
                    variant={option.value === 'accepted' ? 'primary' : 'secondary'}
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () =>
                          verdict.mutateAsync({
                            recordId: record.id,
                            status: option.value,
                            reason: reason.trim() || null,
                          }),
                        () => setReason(''),
                      )
                    }
                  >
                    {option.label}
                  </Button>
                ))}
              {canResolve(record.status) && (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      () => resolve.mutateAsync({ recordId: record.id, reason: reason.trim() || null }),
                      () => setReason(''),
                    )
                  }
                >
                  Mark resolved
                </Button>
              )}
              {canManage && canExpire(record.status) && (
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      () => expire.mutateAsync({ recordId: record.id, reason: reason.trim() || null }),
                      () => setReason(''),
                    )
                  }
                >
                  Expire
                </Button>
              )}
            </div>
            {!canRecordVerdict(record.status) && !canResolve(record.status) && !canManage && (
              <p className="text-xs text-[var(--muted-foreground)]">No further decisions are available for this status.</p>
            )}
          </CardContent>
        </Card>
      )}

      {canReview && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Owner</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            <p>
              Currently: <span className="font-medium">{ownerLabel(record)}</span>
            </p>
            <div className="flex flex-wrap gap-2">
              {!ownedByMe && currentUserId && (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => void run(() => assign.mutateAsync({ recordId: record.id, ownerUserId: currentUserId }))}
                >
                  Assign to me
                </Button>
              )}
              {record.owner_user_id && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void run(() => assign.mutateAsync({ recordId: record.id, ownerUserId: null }))}
                >
                  Unassign
                </Button>
              )}
            </div>
            {canManage && (
              <OwnerPicker
                key={record.id}
                disabled={busy}
                onAssign={(ownerUserId) => void run(() => assign.mutateAsync({ recordId: record.id, ownerUserId }))}
              />
            )}
          </CardContent>
        </Card>
      )}

      {canReview && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Add a note</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <textarea
              aria-label="Note"
              value={noteText}
              maxLength={4000}
              onChange={(event) => setNoteText(event.target.value)}
              className="min-h-16 w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm"
            />
            <div>
              <Button
                size="sm"
                disabled={busy || noteText.trim().length === 0}
                onClick={() =>
                  void run(
                    () => note.mutateAsync({ recordId: record.id, note: noteText.trim() }),
                    () => setNoteText(''),
                  )
                }
              >
                Add note
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {error && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          {error}
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">History</CardTitle>
        </CardHeader>
        <CardContent>
          {events.isLoading && <p className="text-sm text-[var(--muted-foreground)]">Loading history…</p>}
          {events.isError && <p className="text-sm text-[var(--danger)]">Could not load history.</p>}
          {events.data && (
            <ol className="flex flex-col gap-2 text-sm">
              {events.data.map((event) => (
                <li key={event.id} className="flex flex-col gap-0.5 border-l-2 border-[var(--border)] pl-3">
                  <span className="font-medium">{describeEvent(event)}</span>
                  <span className="text-xs text-[var(--muted-foreground)]">
                    {formatWhen(event.created_at)} · {event.actor_user_id === currentUserId ? 'you' : 'a reviewer'}
                  </span>
                  {event.reason && <span className="whitespace-pre-wrap">{event.reason}</span>}
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs font-medium text-[var(--muted-foreground)]">{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

/** Admin-only owner picker. Lists the tenant's own reviewers -- the same set the
 * server accepts as an owner, so an option here is never refused. The user list
 * is only fetched for an admin, because the tenant-admin route is admin-gated. */
function OwnerPicker({ disabled, onAssign }: { disabled: boolean; onAssign: (ownerUserId: string) => void }) {
  const users = useTenantUsers()
  const reviewers = (users.data ?? []).filter(
    (user) => user.status === 'active' && (user.roles.includes('analyst') || user.roles.includes('admin')),
  )
  const [choice, setChoice] = useState('')

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="rec-owner-pick">
        Assign to a reviewer
      </label>
      <Select id="rec-owner-pick" value={choice} onChange={(event) => setChoice(event.target.value)}>
        <option value="">Choose a reviewer</option>
        {reviewers.map((user) => (
          // An admin already sees every tenant user's email on the tenant-admin
          // Users tab, so falling back to it here reveals nothing new, and it
          // keeps two unnamed reviewers distinguishable.
          <option key={user.id} value={user.id}>
            {user.display_name ?? user.email}
          </option>
        ))}
      </Select>
      <Button size="sm" disabled={disabled || choice === ''} onClick={() => onAssign(choice)}>
        Assign
      </Button>
    </div>
  )
}
