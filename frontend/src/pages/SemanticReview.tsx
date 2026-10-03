import { useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Expander } from '@/components/ui/expander'
import { Select } from '@/components/ui/select'
import {
  useCatalogEntries,
  useCatalogEntryVersions,
  useCreateCatalogEntry,
  useOnboardingJobs,
  useOnboardingReviewItems,
  usePublishCatalogEntry,
  useRequestCatalogEntryChanges,
  useReviewCatalogEntry,
  useUpdateCatalogEntry,
} from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import type { CatalogConceptType, CatalogEntryOut, CatalogStatus } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

const STATUS_TONE: Record<CatalogStatus, 'neutral' | 'success' | 'warning' | 'danger' | 'accent'> = {
  draft: 'neutral',
  reviewed: 'warning',
  published: 'success',
  superseded: 'danger',
}

/** The SME Semantic Review Dashboard -- Prompt 27, the first real
 * frontend surface for the tenant-aware semantic catalog
 * (`semantic/catalog.py`, `api/semantic_catalog.py`, Prompt 09/10), which
 * had zero frontend before this page.
 *
 * **Two real backend review systems exist here, never bridged into one**
 * (confirmed by inspection -- nothing in `onboarding/` ever creates a
 * `SemanticCatalogEntry` row): this page is the *governed catalog*'s own
 * review surface (entities/metrics/dimensions/domains -- what the prompt's
 * own "Revenue metric: source, proposed expression, evidence, confidence,
 * version, reviewer" example is actually about), plus a compact summary
 * of onboarding jobs still awaiting SME review, reusing the *existing*
 * `ReviewItemsSection` component and `DatabaseOnboarding` page for the
 * actual PII/relationship/semantic-label/golden-question review
 * interaction rather than re-implementing that table a second time here
 * (master-contract rule 3: never duplicate existing functionality).
 *
 * **The prompt's five actions (Confirm, Reject, Edit, Request
 * Clarification, Defer) don't map one-to-one onto the catalog's real
 * transitions** (`identity.repositories.semantic_catalog
 * .VALID_STATUS_TRANSITIONS`: draft->reviewed, reviewed->draft,
 * reviewed->published; edit only on draft) -- honestly disclosed rather
 * than faked:
 * - **Confirm** is context-sensitive: "Approve" (draft -> reviewed) or
 *   "Publish" (reviewed -> published), whichever transition the entry's
 *   current status actually allows.
 * - **Reject has no backend equivalent for a catalog entry at all** --
 *   there is no "rejected" status. The closest real action is
 *   **Request Changes** (reviewed -> draft, with notes), shown only for
 *   a reviewed entry; a draft entry can only be approved, edited, or
 *   deferred.
 * - **Edit** is `PATCH`, and only works on a `draft` entry (the one row
 *   this table ever allows an in-place edit on).
 * - **Request Clarification** is the real `request-changes` route.
 * - **Defer** makes no API call at all -- it just advances the reviewer
 *   to the next item in the priority-sorted queue, leaving this entry's
 *   real state completely unchanged.
 *
 * Metric-specific fields (source tables, approved expression, evidence,
 * confidence, version, reviewer) come straight from
 * `CatalogEntryOut`/`identity.models.SemanticCatalogEntry`. **"Affected
 * questions" (named in the prompt) is not tracked anywhere in this
 * backend** -- disclosed in the UI rather than fabricated.
 *
 * Prioritization (low confidence / conflicting / high-impact / ambiguous)
 * is a disclosed, deterministic client-side heuristic
 * (`priorityScore` below), not a hidden model -- the same transparency
 * `onboarding/semantic_inference.py`'s own ambiguity flag already uses.
 */
export function SemanticReview() {
  const localUser = useLocalAuthStore((state) => state.user)
  const canManage = Boolean(localUser?.roles.includes('admin'))
  const canReview = canManage || Boolean(localUser?.roles.includes('analyst'))

  const [databaseId, setDatabaseId] = useState('')
  const [conceptType, setConceptType] = useState<'' | CatalogConceptType>('')
  const [statusFilter, setStatusFilter] = useState<'' | CatalogStatus>('')
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null)
  const [showCreateForm, setShowCreateForm] = useState(false)

  const entries = useCatalogEntries({
    databaseId: databaseId || undefined,
    conceptType: conceptType || undefined,
    status: statusFilter || undefined,
    includeConflicts: true,
  })

  const sorted = useMemo(() => {
    const list = entries.data ?? []
    return [...list].sort((a, b) => priorityScore(b) - priorityScore(a))
  }, [entries.data])

  const selected = sorted.find((entry) => entry.id === selectedEntryId) ?? null

  const handleDefer = () => {
    if (!selected) return
    const index = sorted.findIndex((entry) => entry.id === selected.id)
    const next = sorted[(index + 1) % sorted.length]
    setSelectedEntryId(next ? next.id : null)
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <h1 className="text-xl font-bold">🔎 SME Semantic Review</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Review AI-inferred business meaning and confirm it into the governed semantic catalog --
          confirmed definitions are what the text-to-SQL pipeline actually uses, never a draft.
        </p>

        <div className="mt-6 flex flex-col gap-6 lg:flex-row">
          <div className="flex w-full flex-col gap-3 lg:w-80">
            <div className="flex flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-3">
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="sr-db">
                Database
              </label>
              <input
                id="sr-db"
                value={databaseId}
                onChange={(event) => setDatabaseId(event.target.value)}
                placeholder="all databases"
                className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
              />
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="sr-type">
                Concept type
              </label>
              <Select
                id="sr-type"
                value={conceptType}
                onChange={(event) => setConceptType(event.target.value as '' | CatalogConceptType)}
              >
                <option value="">All types</option>
                <option value="entity">Entity</option>
                <option value="metric">Metric</option>
                <option value="dimension">Dimension</option>
                <option value="domain">Domain</option>
              </Select>
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="sr-status">
                Status
              </label>
              <Select
                id="sr-status"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value as '' | CatalogStatus)}
              >
                <option value="">All statuses</option>
                <option value="draft">Draft</option>
                <option value="reviewed">Reviewed</option>
                <option value="published">Published</option>
                <option value="superseded">Superseded</option>
              </Select>
            </div>

            {canManage && (
              <Button size="sm" variant="secondary" onClick={() => setShowCreateForm((v) => !v)}>
                {showCreateForm ? 'Close' : '+ New concept'}
              </Button>
            )}
            {showCreateForm && (
              <CreateEntryForm
                defaultDatabaseId={databaseId}
                onCreated={(entry) => {
                  setShowCreateForm(false)
                  setSelectedEntryId(entry.id)
                }}
              />
            )}

            {entries.isLoading && <p className="text-sm text-[var(--muted-foreground)]">Loading…</p>}
            {entries.isError && (
              <p className="text-sm text-[var(--danger)]">
                {entries.error instanceof ApiError ? entries.error.message : 'Could not load entries.'}
              </p>
            )}
            <ul className="flex flex-col gap-1.5">
              {sorted.map((entry) => (
                <li key={entry.id}>
                  <button
                    onClick={() => setSelectedEntryId(entry.id)}
                    className={`w-full rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                      selectedEntryId === entry.id
                        ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                        : 'border-[var(--border)] hover:bg-[var(--muted)]'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium">{entry.business_name}</span>
                      <span className="text-xs text-[var(--muted-foreground)]">v{entry.version}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <Badge tone={STATUS_TONE[entry.status]}>{entry.status}</Badge>
                      <span className="text-xs text-[var(--muted-foreground)]">{entry.concept_type}</span>
                      {entry.confidence < 0.7 && <Badge tone="warning">low confidence</Badge>}
                      {entry.conflicting_entry_ids.length > 0 && <Badge tone="danger">conflict</Badge>}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
            {sorted.length === 0 && !entries.isLoading && (
              <p className="text-sm text-[var(--muted-foreground)]">
                No semantic-catalog entries match these filters
                {canManage ? ' -- create one to get started.' : '.'}
              </p>
            )}
          </div>

          <div className="min-w-0 flex-1">
            {selected ? (
              <EntryDetail
                entry={selected}
                canManage={canManage}
                canReview={canReview}
                onDefer={handleDefer}
              />
            ) : (
              <Card>
                <CardContent className="py-10 text-center text-sm text-[var(--muted-foreground)]">
                  Select an entry on the left to review it.
                </CardContent>
              </Card>
            )}

            <div className="mt-6">
              <OnboardingReviewSummary canReview={canReview} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

/** A deterministic, disclosed priority heuristic -- never a hidden
 * model. Higher score sorts first: a conflict is the strongest signal
 * (two published claims can't both be right), low confidence and metric
 * ("high-impact" -- a metric definition feeds every KPI question that
 * uses it) are secondary signals. */
function priorityScore(entry: CatalogEntryOut): number {
  let score = 0
  if (entry.conflicting_entry_ids.length > 0) score += 4
  if (entry.confidence < 0.7) score += 2
  if (entry.concept_type === 'metric') score += 1
  return score
}

function EntryDetail({
  entry,
  canManage,
  canReview,
  onDefer,
}: {
  entry: CatalogEntryOut
  canManage: boolean
  canReview: boolean
  onDefer: () => void
}) {
  const [editing, setEditing] = useState(false)
  const versions = useCatalogEntryVersions(
    editing
      ? null
      : { conceptKey: entry.concept_key, databaseId: entry.database_id, conceptType: entry.concept_type },
  )

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            <CardTitle>{entry.business_name}</CardTitle>
            <p className="mt-1 text-xs text-[var(--muted-foreground)]">
              {entry.concept_type} · {entry.database_id} · v{entry.version}
            </p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <Badge tone={STATUS_TONE[entry.status]}>{entry.status}</Badge>
            <span className="text-[11px] text-[var(--muted-foreground)]">{entry.truth_level}</span>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {entry.conflicting_entry_names.length > 0 && (
            <p className="rounded-md bg-[var(--danger)]/10 p-2 text-[var(--danger)]">
              Shares a business name/synonym with {entry.conflicting_entry_names.length} other published
              entr{entry.conflicting_entry_names.length === 1 ? 'y' : 'ies'}:{' '}
              {entry.conflicting_entry_names.join(', ')}. Neither claim is auto-resolved -- review both.
            </p>
          )}
          {entry.description && <p>{entry.description}</p>}
          <Fields
            fields={[
              ['Grain', entry.grain],
              ['Domain', entry.domain],
              ['Owner', entry.owner],
              ['Technical name', entry.technical_name],
            ]}
          />
          {entry.keys.length > 0 && <TagRow label="Keys" values={entry.keys} />}
          {entry.synonyms.length > 0 && <TagRow label="Synonyms" values={entry.synonyms} />}
          {entry.business_rules.length > 0 && <TagRow label="Business rules" values={entry.business_rules} />}
          {entry.examples.length > 0 && <TagRow label="Examples" values={entry.examples} />}
        </CardContent>
      </Card>

      {entry.concept_type === 'metric' && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Governed metric</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <Fields
              fields={[
                ['Source tables', entry.source_tables.join(', ') || null],
                ['Aggregation', entry.aggregation],
                ['Filters', entry.filters.join(', ') || null],
                ['Dimensions', entry.dimensions.join(', ') || null],
                ['Confidence', `${(entry.confidence * 100).toFixed(0)}%`],
              ]}
            />
            {entry.approved_expression && (
              <div>
                <div className="text-xs text-[var(--muted-foreground)]">Proposed expression</div>
                <code className="mt-1 block rounded-md bg-[var(--muted)] px-2 py-1.5 text-xs">
                  {entry.approved_expression}
                </code>
              </div>
            )}
            {entry.evidence.length > 0 && (
              <div>
                <div className="text-xs text-[var(--muted-foreground)]">Evidence</div>
                <pre className="mt-1 overflow-x-auto rounded-md bg-[var(--muted)] px-2 py-1.5 text-xs">
                  {JSON.stringify(entry.evidence, null, 2)}
                </pre>
              </div>
            )}
            <p className="text-xs text-[var(--muted-foreground)]">
              Affected questions: not tracked -- this backend has no mechanism linking a metric
              definition back to the natural-language questions that use it.
            </p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Review history</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          <Fields
            fields={[
              [
                'Reviewed',
                entry.reviewed_at
                  ? `${new Date(entry.reviewed_at).toLocaleString()} by ${entry.reviewed_by_display_name ?? 'an account since removed'}`
                  : null,
              ],
              ['Review notes', entry.review_notes],
              [
                'Published',
                entry.published_at
                  ? `${new Date(entry.published_at).toLocaleString()} by ${entry.published_by_display_name ?? 'an account since removed'}`
                  : null,
              ],
            ]}
          />
          <Expander title="Version history">
            {versions.data && versions.data.length > 0 ? (
              <ul className="flex flex-col gap-1 text-xs">
                {versions.data.map((version) => (
                  <li key={version.id} className="flex items-center gap-2">
                    <Badge tone={STATUS_TONE[version.status]}>v{version.version}</Badge>
                    <span>{version.status}</span>
                    <span className="text-[var(--muted-foreground)]">
                      {new Date(version.created_at).toLocaleDateString()}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-[var(--muted-foreground)]">No other versions yet.</p>
            )}
          </Expander>
        </CardContent>
      </Card>

      {editing ? (
        <EditEntryCard entry={entry} onClose={() => setEditing(false)} />
      ) : (
        <ReviewActions
          entry={entry}
          canManage={canManage}
          canReview={canReview}
          onEdit={() => setEditing(true)}
          onDefer={onDefer}
        />
      )}
    </div>
  )
}

function ReviewActions({
  entry,
  canManage,
  canReview,
  onEdit,
  onDefer,
}: {
  entry: CatalogEntryOut
  canManage: boolean
  canReview: boolean
  onEdit: () => void
  onDefer: () => void
}) {
  const review = useReviewCatalogEntry(entry.id)
  const requestChanges = useRequestCatalogEntryChanges(entry.id)
  const publish = usePublishCatalogEntry(entry.id)
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)

  if (!canReview) {
    return (
      <p className="text-xs text-[var(--muted-foreground)]">
        Reviewing semantic-catalog entries requires an analyst or admin account.
      </p>
    )
  }

  const run = async (action: () => Promise<unknown>) => {
    setError(null)
    try {
      await action()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'That action could not be completed.')
    }
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 pt-4 text-sm">
        {error && <p className="text-[var(--danger)]">{error}</p>}
        <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor={`sr-notes-${entry.id}`}>
          Notes (optional, recorded on the decision)
        </label>
        <textarea
          id={`sr-notes-${entry.id}`}
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          rows={2}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
        <div className="flex flex-wrap gap-2">
          {entry.status === 'draft' && (
            <>
              <Button
                size="sm"
                disabled={review.isPending}
                onClick={() => run(() => review.mutateAsync({ notes: notes || null }))}
              >
                Confirm (approve to reviewed)
              </Button>
              <Button size="sm" variant="secondary" onClick={onEdit}>
                Edit
              </Button>
            </>
          )}
          {entry.status === 'reviewed' && (
            <>
              {canManage && (
                <Button
                  size="sm"
                  disabled={publish.isPending}
                  onClick={() => run(() => publish.mutateAsync())}
                >
                  Confirm (publish)
                </Button>
              )}
              <Button
                size="sm"
                variant="secondary"
                disabled={requestChanges.isPending}
                onClick={() => run(() => requestChanges.mutateAsync({ notes: notes || null }))}
              >
                Request clarification
              </Button>
            </>
          )}
          <Button size="sm" variant="ghost" onClick={onDefer}>
            Defer
          </Button>
        </div>
        <p className="text-xs text-[var(--muted-foreground)]">
          {entry.status === 'draft'
            ? 'There is no "Reject" state for a new concept -- approve it, edit it, or defer the decision.'
            : entry.status === 'reviewed'
              ? '"Request clarification" is the closest equivalent to reject here -- it sends the entry back to draft with your notes.'
              : 'This entry is read-only in its current status.'}
          {' '}"Defer" makes no change -- it just moves you to the next item in the queue.
        </p>
      </CardContent>
    </Card>
  )
}

function EditEntryCard({ entry, onClose }: { entry: CatalogEntryOut; onClose: () => void }) {
  const update = useUpdateCatalogEntry(entry.id)
  const [description, setDescription] = useState(entry.description)
  const [grain, setGrain] = useState(entry.grain ?? '')
  const [confidence, setConfidence] = useState(String(entry.confidence))
  const [approvedExpression, setApprovedExpression] = useState(entry.approved_expression ?? '')
  const [error, setError] = useState<string | null>(null)

  const handleSave = async () => {
    setError(null)
    try {
      await update.mutateAsync({
        description,
        grain: grain || null,
        confidence: Number(confidence),
        approved_expression: approvedExpression || null,
      })
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save this edit.')
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Edit draft</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <Field label="Description">
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={2}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
        <Field label="Grain">
          <input
            value={grain}
            onChange={(event) => setGrain(event.target.value)}
            className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
        {entry.concept_type === 'metric' && (
          <Field label="Proposed expression">
            <input
              value={approvedExpression}
              onChange={(event) => setApprovedExpression(event.target.value)}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 font-mono text-sm"
            />
          </Field>
        )}
        <Field label="Confidence (0-1)">
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={confidence}
            onChange={(event) => setConfidence(event.target.value)}
            className="w-32 rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
          />
        </Field>
        {error && <p className="text-[var(--danger)]">{error}</p>}
        <div className="flex gap-2">
          <Button size="sm" disabled={update.isPending} onClick={handleSave}>
            Save
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function CreateEntryForm({
  defaultDatabaseId,
  onCreated,
}: {
  defaultDatabaseId: string
  onCreated: (entry: CatalogEntryOut) => void
}) {
  const create = useCreateCatalogEntry()
  const [databaseId, setDatabaseId] = useState(defaultDatabaseId)
  const [conceptType, setConceptType] = useState<CatalogConceptType>('entity')
  const [conceptKey, setConceptKey] = useState('')
  const [businessName, setBusinessName] = useState('')
  const [description, setDescription] = useState('')
  const [approvedExpression, setApprovedExpression] = useState('')
  const [sourceTables, setSourceTables] = useState('')
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      const entry = await create.mutateAsync({
        database_id: databaseId,
        concept_type: conceptType,
        concept_key: conceptKey,
        business_name: businessName,
        description,
        confidence: 1,
        approved_expression: conceptType === 'metric' ? approvedExpression || null : null,
        source_tables:
          conceptType === 'metric'
            ? sourceTables.split(',').map((s) => s.trim()).filter(Boolean)
            : [],
      })
      onCreated(entry)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create this concept.')
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm"
    >
      <Field label="Database ID">
        <input
          required
          value={databaseId}
          onChange={(event) => setDatabaseId(event.target.value)}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
      </Field>
      <Field label="Concept type">
        <Select value={conceptType} onChange={(event) => setConceptType(event.target.value as CatalogConceptType)}>
          <option value="entity">Entity</option>
          <option value="metric">Metric</option>
          <option value="dimension">Dimension</option>
          <option value="domain">Domain</option>
        </Select>
      </Field>
      <Field label="Concept key (stable id)">
        <input
          required
          value={conceptKey}
          onChange={(event) => setConceptKey(event.target.value)}
          placeholder="e.g. customer_lifetime_value"
          className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
      </Field>
      <Field label="Business name">
        <input
          required
          value={businessName}
          onChange={(event) => setBusinessName(event.target.value)}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
      </Field>
      <Field label="Description">
        <textarea
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          rows={2}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
        />
      </Field>
      {conceptType === 'metric' && (
        <>
          <Field label="Proposed expression">
            <input
              value={approvedExpression}
              onChange={(event) => setApprovedExpression(event.target.value)}
              placeholder="SUM(Amount)"
              className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 font-mono text-sm"
            />
          </Field>
          <Field label="Source tables (comma-separated)">
            <input
              value={sourceTables}
              onChange={(event) => setSourceTables(event.target.value)}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
            />
          </Field>
        </>
      )}
      {error && <p className="text-[var(--danger)]">{error}</p>}
      <Button type="submit" size="sm" disabled={create.isPending}>
        {create.isPending ? 'Creating…' : 'Create draft'}
      </Button>
    </form>
  )
}

/** A compact cross-reference into the *other* real review system
 * (onboarding review items -- PII/relationship/semantic-label/golden-
 * question) that this dashboard deliberately does not re-implement.
 * Each job's own pending-item count comes from a real, already-
 * established hook (`useOnboardingReviewItems`); there is no deep link
 * into a specific job on `/db-onboarding` yet (that page's own job
 * selection is local component state, not a URL param) -- a disclosed,
 * minor limitation, not a broken link. */
function OnboardingReviewSummary({ canReview }: { canReview: boolean }) {
  const jobs = useOnboardingJobs()
  const awaitingReview = (jobs.data ?? []).filter((job) => job.status === 'awaiting_review')

  if (!canReview) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Onboarding review items</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <p className="text-xs text-[var(--muted-foreground)]">
          PII classifications, relationship candidates, semantic labels, and golden questions are
          reviewed per onboarding job on the Database Onboarding page, not duplicated here.
        </p>
        {awaitingReview.length === 0 ? (
          <p className="text-[var(--muted-foreground)]">No onboarding jobs are currently awaiting review.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {awaitingReview.map((job) => (
              <JobReviewRow key={job.id} jobId={job.id} label={job.database_label} />
            ))}
          </ul>
        )}
        <a href="/db-onboarding" className="text-xs font-medium text-[var(--accent)] hover:underline">
          Open Database Onboarding →
        </a>
      </CardContent>
    </Card>
  )
}

function JobReviewRow({ jobId, label }: { jobId: string; label: string }) {
  const items = useOnboardingReviewItems(jobId)
  const pending = (items.data ?? []).filter((item) => item.decision === 'pending').length
  return (
    <li className="flex items-center justify-between rounded-md border border-[var(--border)] px-3 py-2">
      <span className="truncate">{label}</span>
      <Badge tone={pending > 0 ? 'warning' : 'success'}>{pending} pending</Badge>
    </li>
  )
}

function Fields({ fields }: { fields: [string, string | null | undefined][] }) {
  const present = fields.filter(([, value]) => Boolean(value))
  if (present.length === 0) return null
  return (
    <dl className="grid grid-cols-2 gap-2">
      {present.map(([label, value]) => (
        <div key={label}>
          <dt className="text-xs text-[var(--muted-foreground)]">{label}</dt>
          <dd className="text-sm">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function TagRow({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <div className="text-xs text-[var(--muted-foreground)]">{label}</div>
      <div className="mt-1 flex flex-wrap gap-1">
        {values.map((value) => (
          <Badge key={value} tone="neutral">
            {value}
          </Badge>
        ))}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-[var(--muted-foreground)]">{label}</span>
      {children}
    </label>
  )
}

