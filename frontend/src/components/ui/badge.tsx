import type { HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'accent'

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: 'bg-[var(--muted)] text-[var(--muted-foreground)]',
  success: 'bg-[var(--success)]/15 text-[var(--success)]',
  warning: 'bg-[var(--warning)]/15 text-[var(--warning)]',
  danger: 'bg-[var(--danger)]/15 text-[var(--danger)]',
  accent: 'bg-[var(--accent-soft)] text-[var(--accent)]',
}

export function Badge({
  tone = 'neutral',
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium',
        TONE_CLASSES[tone],
        className,
      )}
      {...props}
    />
  )
}
