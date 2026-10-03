import { useState } from 'react'
import { RecommendationDetail } from '@/components/recommendations/RecommendationDetail'
import { RecommendationList } from '@/components/recommendations/RecommendationList'
import { Select } from '@/components/ui/select'
import {
  useCapabilities,
  useRecommendations,
} from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import {
  CATEGORY_LABELS,
  type OwnershipFilter,
  STATUS_LABELS,
} from '@/lib/recommendationDisplay'
import type { RecommendationCategory, RecommendationStatus } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

/** The Recommendation & Action Dashboard -- Prompt 31. Built on the governed
 * recommendation APIs from Prompts 18 and 31 (`api/recommendation_governance.py`):
 * a filterable list, and a detail view with the claim, its evidence, the
 * action buttons the lifecycle allows, ownership, notes and the full audit
 * trail.
 *
 * Nothing on this page changes the live SQL pipeline or `recommendation.engine`'s
 * own rules. A reviewer's verdict is recorded and measured, not used to retune
 * the engine (master-contract rule: feedback must not silently change
 * production rules). */
export function Recommendations() {
  const localUser = useLocalAuthStore((state) => state.user)
  const currentUserId = localUser?.id ?? null
  // Server-decided (Prompt 32): see `useCapabilities`. Never a role-name check.
  const capabilities = useCapabilities()
  const canManage = Boolean(capabilities.manage_recommendations)
  const canReview = canManage || Boolean(capabilities.review_recommendations)

  const [databaseId, setDatabaseId] = useState('')
  const [category, setCategory] = useState<'' | RecommendationCategory>('')
  const [status, setStatus] = useState<'' | RecommendationStatus>('')
  const [ownership, setOwnership] = useState<OwnershipFilter>('anyone')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const filters = {
    databaseId: databaseId.trim() || undefined,
    category: category || undefined,
    status: status || undefined,
    ownerUserId: ownership === 'mine' && currentUserId ? currentUserId : undefined,
    unassigned: ownership === 'unassigned',
  }
  const records = useRecommendations(filters)
  // Falls back to the first record so a reviewer lands on something useful
  // rather than an empty panel. A selection that the current filters no longer
  // include is ignored, not carried over silently.
  const activeId = selectedId ?? records.data?.[0]?.id ?? null
  const selected = records.data?.find((record) => record.id === activeId) ?? null

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <h1 className="text-xl font-bold">🧭 Recommendations &amp; actions</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Suggestions the platform generated from your data. Each one shows the evidence it rests on, and stays an
          estimate until a reviewer decides what to do with it.
        </p>

        <div className="mt-6 flex flex-col gap-6 lg:flex-row">
          <div className="flex w-full flex-col gap-3 lg:w-96">
            <div className="grid grid-cols-1 gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 sm:grid-cols-2 lg:grid-cols-1">
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="rec-db">
                Database
              </label>
              <input
                id="rec-db"
                value={databaseId}
                onChange={(event) => setDatabaseId(event.target.value)}
                placeholder="all databases"
                className="w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-1.5 text-sm"
              />
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="rec-category">
                Category
              </label>
              <Select
                id="rec-category"
                value={category}
                onChange={(event) => setCategory(event.target.value as '' | RecommendationCategory)}
              >
                <option value="">All categories</option>
                {(Object.keys(CATEGORY_LABELS) as RecommendationCategory[]).map((value) => (
                  <option key={value} value={value}>
                    {CATEGORY_LABELS[value]}
                  </option>
                ))}
              </Select>
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="rec-status">
                Status
              </label>
              <Select
                id="rec-status"
                value={status}
                onChange={(event) => setStatus(event.target.value as '' | RecommendationStatus)}
              >
                <option value="">All statuses</option>
                {(Object.keys(STATUS_LABELS) as RecommendationStatus[]).map((value) => (
                  <option key={value} value={value}>
                    {STATUS_LABELS[value]}
                  </option>
                ))}
              </Select>
              <label className="text-xs font-medium text-[var(--muted-foreground)]" htmlFor="rec-owner">
                Owner
              </label>
              <Select
                id="rec-owner"
                value={ownership}
                onChange={(event) => setOwnership(event.target.value as OwnershipFilter)}
              >
                <option value="anyone">Anyone</option>
                <option value="mine">Assigned to me</option>
                <option value="unassigned">Unassigned</option>
              </Select>
            </div>

            {records.isLoading && <p className="text-sm text-[var(--muted-foreground)]">Loading recommendations…</p>}
            {records.isError && (
              <p role="alert" className="text-sm text-[var(--danger)]">
                {records.error instanceof ApiError && records.error.status === 403
                  ? 'Your role cannot review recommendations. Ask an administrator for the analyst role.'
                  : 'Recommendations could not be loaded.'}
              </p>
            )}
            {records.data && (
              <RecommendationList
                records={records.data}
                selectedId={selected?.id ?? null}
                onSelect={setSelectedId}
              />
            )}
          </div>

          <div className="min-w-0 flex-1">
            {selected ? (
              <RecommendationDetail
                key={selected.id}
                record={selected}
                currentUserId={currentUserId}
                canReview={canReview}
                canManage={canManage}
              />
            ) : (
              <p className="rounded-lg border border-dashed border-[var(--border)] p-6 text-sm text-[var(--muted-foreground)]">
                Select a recommendation to see its evidence, decide on it, and review its history.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
