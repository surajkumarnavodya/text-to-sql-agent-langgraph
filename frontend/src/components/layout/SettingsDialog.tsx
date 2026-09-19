import type { RefObject } from 'react'
import { useTranslation } from 'react-i18next'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { HistorySettingsSection } from './HistorySettingsSection'

/** Application settings, previously folded into the bottom of the combined
 * history+settings drawer -- now its own dialog, reachable only from the
 * header's `UserMenu` "Settings" item (one instance, one location -- see
 * docs/navigation-and-actions.md). `HistorySettingsSection` itself is
 * unmodified; only where it's rendered changed.
 *
 * `triggerRef` (points at `UserMenu`'s avatar button) is used in
 * `onCloseAutoFocus` to explicitly restore focus there on close. Radix
 * Dialog's own automatic focus-restoration only works reliably when
 * opened directly from the element focused at mount time -- here it's
 * opened from a `DropdownMenuItem` that unmounts (closing the whole menu)
 * the instant it's selected, so nothing meaningful is focused by the time
 * this Dialog's `open` prop flips true. Verified via a real headless-
 * browser run before this fix: closing Settings left `document
 * .activeElement` on `<body>`, not the account-menu button -- see
 * docs/functional-ui-audit.md. */
export function SettingsDialog({
  open,
  onOpenChange,
  triggerRef,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  triggerRef: RefObject<HTMLButtonElement | null>
}) {
  const { t } = useTranslation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-md p-0"
        onCloseAutoFocus={(event) => {
          event.preventDefault()
          triggerRef.current?.focus()
        }}
      >
        <div className="border-b border-[var(--border)] px-6 py-4">
          <DialogTitle>{t('sidebar.settingsTitle')}</DialogTitle>
        </div>
        <div className="max-h-[70vh] overflow-y-auto">
          <HistorySettingsSection />
        </div>
      </DialogContent>
    </Dialog>
  )
}
