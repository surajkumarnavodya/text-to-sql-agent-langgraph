import { Minus, Plus } from 'lucide-react'
import type { ReactNode } from 'react'
import { useSettingsStore } from '@/store/settingsStore'

/** Reactively selects whether one sidebar section is collapsed.
 *
 * Deliberately NOT `useSettingsStore((s) => s.isSectionCollapsed)` called
 * inline by a consumer -- that selects the *function itself*, a reference
 * that never changes, so Zustand never re-renders the consumer when
 * `collapsedSections` actually changes (the +/- toggle would silently stop
 * updating the UI, even though the underlying state was toggling
 * correctly). Selecting the boolean directly, per id, is what makes both
 * SectionHeader's icon and SectionBody's show/hide react to a toggle.
 */
function useSectionCollapsed(id: string): boolean {
  return useSettingsStore((state) => Boolean(state.collapsedSections[id]))
}

/** A collapsible sidebar section with an explicit +/- control -- the
 * whole header row is also clickable/keyboard-operable, but the icon
 * itself is what gives "clear visual feedback" of the current state,
 * per the UI-modernization request. Collapse state persists per-section
 * (useSettingsStore.collapsedSections), across reloads. */
export function SectionHeader({
  id,
  title,
  children,
}: {
  id: string
  title: ReactNode
  children?: ReactNode
}) {
  const collapsed = useSectionCollapsed(id)
  const toggle = useSettingsStore((state) => state.toggleSection)

  return (
    <button
      type="button"
      onClick={() => toggle(id)}
      aria-expanded={!collapsed}
      aria-controls={`section-${id}`}
      className="flex w-full items-center justify-between rounded-md px-1 py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)] transition-colors hover:text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
    >
      <span className="flex items-center gap-2">
        {title}
        {children}
      </span>
      <span
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--card)] text-[var(--foreground)] transition-transform"
        aria-hidden="true"
      >
        {collapsed ? <Plus className="h-3 w-3" /> : <Minus className="h-3 w-3" />}
      </span>
    </button>
  )
}

export function SectionBody({ id, children }: { id: string; children: ReactNode }) {
  const collapsed = useSectionCollapsed(id)
  if (collapsed) return null
  return (
    <div id={`section-${id}`} className="mt-2 flex flex-col gap-2">
      {children}
    </div>
  )
}
