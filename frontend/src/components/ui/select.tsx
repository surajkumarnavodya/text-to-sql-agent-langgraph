import type { SelectHTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

/** A plain native <select>, styled -- deliberately not a Radix Select:
 * native selects are fully accessible and keyboard-operable out of the
 * box, and every picker here (font, language, sensitivity category) is a
 * short, simple list with no need for rich item content. */
export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        'h-9 rounded-md border border-[var(--border)] bg-[var(--input)] px-2.5 text-sm text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
        className,
      )}
      {...props}
    />
  )
}
