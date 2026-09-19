import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

// A thin wrapper around @radix-ui/react-dialog (already an existing
// dependency, previously unused as a standalone component) -- gets real
// focus trapping, Escape-to-close, aria-modal/aria-labelledby wiring, and
// scroll-lock for free from Radix rather than hand-rolling any of it.
export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogClose = DialogPrimitive.Close

export function DialogContent({
  className,
  children,
  showCloseButton = true,
  ...props
}: ComponentProps<typeof DialogPrimitive.Content> & { showCloseButton?: boolean }) {
  return (
    <DialogPrimitive.Portal>
      {/* Fully opaque -- deliberately NOT bg-black/40 or --card. --card carries
          a translucent "glass" alpha channel in dark mode (see index.css),
          and a 40%-opacity backdrop lets page content show through around
          the panel. A modal must occlude the entire page behind it. */}
      <DialogPrimitive.Overlay
        className="fixed inset-0 bg-[var(--modal-backdrop)] transition-opacity duration-[var(--duration-base)]"
        style={{ zIndex: 'var(--z-modal)' }}
      />
      <DialogPrimitive.Content
        className={cn(
          'fixed left-1/2 top-1/2 w-full max-w-lg -translate-x-1/2 -translate-y-1/2',
          'rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--modal-surface)] text-[var(--modal-surface-foreground)] p-6 shadow-xl outline-none',
          'max-h-[calc(100vh-2rem)] overflow-y-auto',
          className,
        )}
        style={{ zIndex: 'var(--z-modal)' }}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close
            aria-label="Close dialog"
            className={cn(
              'absolute right-4 top-4 rounded-md p-1 text-[var(--muted-foreground)] transition-colors',
              'hover:bg-[var(--muted)] hover:text-[var(--foreground)]',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]',
            )}
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}

export function DialogHeader({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('mb-4 flex flex-col gap-1', className)} {...props} />
}

export function DialogTitle({ className, ...props }: ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn('text-base font-semibold text-[var(--foreground)]', className)}
      {...props}
    />
  )
}

export function DialogDescription({
  className,
  ...props
}: ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn('text-sm text-[var(--muted-foreground)]', className)}
      {...props}
    />
  )
}

export function DialogFooter({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div className={cn('mt-6 flex flex-wrap justify-end gap-2', className)} {...props} />
  )
}
