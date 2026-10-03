import type { ReactNode } from 'react'
import type { TruthLevel } from '@/lib/types'
import { TruthLevelBadge } from './TruthLevelBadge'

/** Shared chrome for every analytics panel: one labelled region, one heading,
 * and an optional truth-level badge so the panel's claim level is visible
 * before its content is read. Panels never render their own region/heading
 * markup -- a single shell keeps accessibility and styling identical across
 * the whole dashboard. */
export function AnalyticsPanel({
  id,
  title,
  truthLevel,
  children,
}: {
  id: string
  title: string
  truthLevel?: TruthLevel
  children: ReactNode
}) {
  const headingId = `${id}-heading`
  return (
    <section
      aria-labelledby={headingId}
      data-testid={id}
      className="flex flex-col gap-3 rounded-lg border border-[var(--border)] bg-[var(--card)] p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={headingId} className="text-sm font-semibold">
          {title}
        </h3>
        {truthLevel && <TruthLevelBadge level={truthLevel} />}
      </div>
      {children}
    </section>
  )
}
