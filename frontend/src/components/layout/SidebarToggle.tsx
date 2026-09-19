import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { useSettingsStore } from '@/store/settingsStore'

/** The one control that toggles the desktop sidebar between its full and
 * icon-rail (collapsed) states -- `sidebarCollapsed` in `settingsStore` is
 * the single source of truth it reads and writes. Rendered exactly once,
 * in the header (`AppShell.tsx`), regardless of collapsed/expanded state --
 * deliberately NOT also duplicated inside the sidebar itself, so there is
 * never more than one toggle visible at a time. A stable, always-in-the-
 * same-place control is also easier to find again once the sidebar it
 * would otherwise live inside has shrunk to an icon rail. See
 * docs/navigation-and-actions.md for the full ownership reasoning.
 *
 * Tooltip mechanism: native `title` + `aria-label`, matching every other
 * icon-only control already in this codebase (no separate Tooltip
 * component/dependency exists or is needed here). */
export function SidebarToggle({ controls, className }: { controls: string; className?: string }) {
  const { t } = useTranslation()
  const collapsed = useSettingsStore((state) => state.sidebarCollapsed)
  const toggle = useSettingsStore((state) => state.toggleSidebarCollapsed)
  const label = t(collapsed ? 'sidebar.expandSidebar' : 'sidebar.collapseSidebar')

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggle}
      aria-label={label}
      aria-expanded={!collapsed}
      aria-controls={controls}
      title={label}
      className={className}
    >
      {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
    </Button>
  )
}
