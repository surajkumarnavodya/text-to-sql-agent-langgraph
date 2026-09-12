import { ChevronRight } from 'lucide-react'
import type { ReactNode } from 'react'

/** A native <details>/<summary> expander -- fully accessible and
 * keyboard-operable with zero JS state, matching st.expander's per-turn,
 * ephemeral (non-persisted) collapse behavior. */
export function Expander({
  title,
  defaultOpen = false,
  children,
}: {
  title: ReactNode
  defaultOpen?: boolean
  children: ReactNode
}) {
  return (
    <details
      open={defaultOpen}
      className="group rounded-lg border border-[var(--border)] bg-[var(--card)]"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm font-medium marker:content-none">
        <ChevronRight className="h-4 w-4 shrink-0 transition-transform group-open:rotate-90" />
        {title}
      </summary>
      <div className="border-t border-[var(--border)] p-3">{children}</div>
    </details>
  )
}
