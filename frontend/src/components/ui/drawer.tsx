import * as DialogPrimitive from '@radix-ui/react-dialog'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

// A sliding side panel built on the same @radix-ui/react-dialog primitive
// as ui/dialog.tsx -- real focus trapping, Escape-to-close, aria-modal,
// and backdrop-click-to-close for free, just positioned/animated as a
// side panel instead of a centered modal. Used for the mobile navigation
// drawer (MobileNav) today; any future "slide-over" panel can reuse it.
export const Drawer = DialogPrimitive.Root
export const DrawerTrigger = DialogPrimitive.Trigger
export const DrawerClose = DialogPrimitive.Close
export const DrawerTitle = DialogPrimitive.Title
export const DrawerDescription = DialogPrimitive.Description

export function DrawerContent({
  className,
  children,
  side = 'left',
  ...props
}: ComponentProps<typeof DialogPrimitive.Content> & { side?: 'left' | 'right' }) {
  return (
    <DialogPrimitive.Portal>
      {/* Fully opaque, same rationale as ui/dialog.tsx's overlay -- a
          40%-opacity backdrop lets page content show through around the
          panel. The drawer's own panel background (--sidebar) was already
          fully opaque in both themes, so only the overlay needed this. */}
      <DialogPrimitive.Overlay
        className="fixed inset-0 bg-[var(--modal-backdrop)]"
        style={{ zIndex: 'var(--z-drawer)' }}
      />
      <DialogPrimitive.Content
        className={cn(
          'fixed inset-y-0 flex w-full max-w-xs flex-col overflow-hidden',
          'bg-[var(--sidebar)] text-[var(--sidebar-foreground)] shadow-2xl outline-none',
          side === 'left' ? 'left-0 border-r border-[var(--border)]' : 'right-0 border-l border-[var(--border)]',
          className,
        )}
        style={{ zIndex: 'var(--z-drawer)' }}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}
