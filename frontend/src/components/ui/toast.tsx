import { X } from 'lucide-react'
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

export type ToastVariant = 'success' | 'error' | 'info'

export interface ToastInput {
  title: string
  description?: string
  variant?: ToastVariant
  /** Milliseconds before auto-dismiss; 0 disables auto-dismiss. Default 5000. */
  durationMs?: number
}

interface ToastItem extends ToastInput {
  id: string
}

interface ToastContextValue {
  toast: (input: ToastInput) => string
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

/**
 * Shared error/success/info notification -- replaces the ad hoc inline
 * colored text each component previously rolled on its own (see
 * docs/frontend-ui-audit.md "Existing UI Limitations" #9). Wrap the app
 * once (in main.tsx or AppShell) with <ToastProvider>; call useToast()
 * from anywhere beneath it.
 *
 * Accessible by construction: an error toast renders with role="alert"
 * (interrupts a screen reader immediately, matching its urgency), a
 * success/info toast with role="status" inside a shared
 * aria-live="polite" region (announced without interrupting). Every
 * toast is also individually dismissible via a labeled close button --
 * auto-dismiss alone is never the only way to clear one, so a screen
 * reader or motor-impaired user isn't racing a timer.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const nextId = useRef(0)

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const toast = useCallback(
    (input: ToastInput) => {
      const id = `toast-${nextId.current++}`
      setToasts((current) => [...current, { ...input, id }])
      const duration = input.durationMs ?? 5000
      if (duration > 0) {
        window.setTimeout(() => dismiss(id), duration)
      }
      return id
    },
    [dismiss],
  )

  return (
    <ToastContext.Provider value={{ toast, dismiss }}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 flex w-full max-w-sm flex-col gap-2"
        style={{ zIndex: 'var(--z-toast)' }}
      >
        {toasts.map((item) => (
          <ToastCard key={item.id} item={item} onDismiss={() => dismiss(item.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

const VARIANT_CLASSES: Record<ToastVariant, string> = {
  success: 'border-[var(--success)]/40 bg-[color-mix(in_srgb,var(--success)_10%,var(--popover-surface))]',
  error: 'border-[var(--danger)]/40 bg-[color-mix(in_srgb,var(--danger)_10%,var(--popover-surface))]',
  info: 'border-[var(--border)] bg-[var(--popover-surface)]',
}

function ToastCard({ item, onDismiss }: { item: ToastItem; onDismiss: () => void }) {
  const variant = item.variant ?? 'info'
  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      aria-live={variant === 'error' ? 'assertive' : 'polite'}
      className={cn(
        'pointer-events-auto rounded-[var(--radius-md)] border p-3 shadow-lg backdrop-blur-sm',
        'flex items-start gap-2 text-sm text-[var(--foreground)]',
        VARIANT_CLASSES[variant],
      )}
    >
      <div className="flex-1">
        <p className="font-medium">{item.title}</p>
        {item.description && (
          <p className="mt-0.5 text-[var(--muted-foreground)]">{item.description}</p>
        )}
      </div>
      <button
        type="button"
        aria-label="Dismiss notification"
        onClick={onDismiss}
        className={cn(
          'rounded p-0.5 text-[var(--muted-foreground)] transition-colors hover:text-[var(--foreground)]',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]',
        )}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) {
    throw new Error('useToast must be used within a <ToastProvider>')
  }
  return ctx
}
