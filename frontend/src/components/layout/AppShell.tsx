import {
  BookOpen,
  Building2,
  Database,
  ImageIcon,
  Lightbulb,
  MessageSquare,
  Plug,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink, Outlet } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useNavigation } from '@/hooks/queries'
import { useSettingsStore } from '@/store/settingsStore'
import { MobileNav } from './MobileNav'
import { ScreenMenu, type ScreenMenuGroup } from './ScreenMenu'
import { SettingsDialog } from './SettingsDialog'
import { Sidebar } from './Sidebar'
import { SidebarToggle } from './SidebarToggle'
import { UserMenu } from './UserMenu'

const SIDEBAR_ID = 'app-sidebar'

/** How each screen the server may list is drawn: an icon and a translated label.
 * Presentation only. Whether a screen appears at all is the server's decision
 * (`GET /navigation`). A screen with no entry here is simply not drawn. */
const SCREEN_PRESENTATION: Record<string, { icon: typeof MessageSquare; labelKey: string }> = {
  chat: { icon: MessageSquare, labelKey: 'nav.chat' },
  knowledge_sources: { icon: BookOpen, labelKey: 'nav.knowledgeSources' },
  media_search: { icon: ImageIcon, labelKey: 'nav.mediaSearch' },
  recommendations: { icon: Lightbulb, labelKey: 'nav.recommendations' },
  db_onboarding: { icon: Plug, labelKey: 'nav.dbOnboarding' },
  semantic_review: { icon: ShieldCheck, labelKey: 'nav.semanticReview' },
  tenant_admin: { icon: Building2, labelKey: 'nav.tenantAdmin' },
  platform_admin: { icon: ShieldAlert, labelKey: 'nav.platformAdmin' },
}

/** The one screen kept as a top-level header tab. Every other screen the
 * server lists goes into the header's Menu dropdown (ScreenMenu). */
const PRIMARY_SCREEN_ID = 'chat'

/** Menu sections, in display order, keyed by the server's `group` field. */
const MENU_GROUPS: { key: 'workspace' | 'review' | 'administration'; labelKey: string }[] = [
  { key: 'workspace', labelKey: 'nav.groupWorkspace' },
  { key: 'review', labelKey: 'nav.groupReview' },
  { key: 'administration', labelKey: 'nav.groupAdministration' },
]

/** Full-viewport application shell: a persistent left conversation-history
 * sidebar (>=lg viewports, collapsible to an icon rail) + main workspace.
 * Below `lg`, the sidebar is replaced by a hamburger-triggered slide-in
 * drawer (MobileNav) rendering the exact same Sidebar content, so there is
 * one history implementation, not two.
 *
 * Action ownership (see docs/navigation-and-actions.md for the full table
 * and docs/ui-production-audit.md for what this replaced): this header
 * owns exactly one account-level control, `UserMenu` -- Settings, Theme,
 * and Sign out all live inside it now, not scattered across the header and
 * the sidebar footer as two independent copies each. `SettingsDialog`'s
 * open/closed state is owned here, as the single instance in the whole
 * app -- `UserMenu`'s "Settings" item just calls `setIsSettingsOpen(true)`,
 * it does not own or mount its own dialog. */
export function AppShell() {
  const { t } = useTranslation()
  const [isSettingsOpen, setIsSettingsOpen] = useState(false)
  // Role-based navigation (Prompt 32): the screens this caller may open come
  // from the server (`GET /navigation`, `security/navigation.py`), never from a
  // role name read in the browser. While that loads, no review or admin tab is
  // shown rather than guessed at.
  const navigation = useNavigation()
  const screens = navigation.data?.items ?? []
  // Only screens the server listed AND this file knows how to draw. Anything
  // the server doesn't list is never present here, so it can't appear in the
  // header or the menu.
  const drawableScreens = screens.flatMap((screen) => {
    const presentation = SCREEN_PRESENTATION[screen.id]
    if (!presentation) return []
    return [{ id: screen.id, path: screen.path, group: screen.group, icon: presentation.icon, label: t(presentation.labelKey) }]
  })
  const primaryScreens = drawableScreens.filter((screen) => screen.id === PRIMARY_SCREEN_ID)
  const menuGroups: ScreenMenuGroup[] = MENU_GROUPS.map(({ key, labelKey }) => ({
    key,
    labelKey,
    items: drawableScreens.filter((screen) => screen.id !== PRIMARY_SCREEN_ID && screen.group === key),
  })).filter((group) => group.items.length > 0)
  const sidebarCollapsed = useSettingsStore((state) => state.sidebarCollapsed)
  // Passed to both UserMenu (attaches it to the avatar button) and
  // SettingsDialog (restores focus there on close) -- see
  // SettingsDialog's own docstring for why this is necessary rather than
  // relying on Radix Dialog's automatic focus restoration.
  const userMenuTriggerRef = useRef<HTMLButtonElement>(null)

  return (
    <div className="h-screen w-screen overflow-hidden bg-[var(--background)] text-[var(--foreground)]">
      {/* `inert` while Settings is open: removes the entire app shell (sidebar,
          header, chat) from the accessibility tree and from tab/pointer
          interaction, on top of (not instead of) the opaque backdrop above --
          the backdrop handles *visual* occlusion, this handles keyboard/AT
          reachability. SettingsDialog itself renders through a Radix Portal
          (see ui/dialog.tsx), so it's not a descendant of this div and is
          never affected by its own inert flag. */}
      <div className="flex h-full" inert={isSettingsOpen || undefined}>
        <aside
          id={SIDEBAR_ID}
          className={cn(
            'hidden shrink-0 border-r border-[var(--border)] bg-[var(--sidebar)] text-[var(--sidebar-foreground)] transition-[width] duration-[var(--duration-base)] ease-out lg:flex lg:flex-col',
            sidebarCollapsed ? 'lg:w-14' : 'lg:w-72',
          )}
        >
          <Sidebar collapsed={sidebarCollapsed} />
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-14 shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--header)] px-4">
            <MobileNav />
            <SidebarToggle controls={SIDEBAR_ID} className="hidden lg:inline-flex" />
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-md bg-[var(--accent)] text-[var(--accent-foreground)]">
                <Database className="h-4 w-4" />
              </span>
              <span className="text-sm font-semibold">{t('app.title')}</span>
              <span className="hidden rounded-full bg-[var(--muted)] px-2 py-0.5 text-[11px] font-medium text-[var(--muted-foreground)] sm:inline">
                {t('header.envIndicator')}
              </span>
            </div>

              <nav aria-label="Primary navigation" className="flex min-w-0 flex-1 items-center gap-1">
                {primaryScreens.map((screen) => (
                  <NavTab key={screen.id} to={screen.path} icon={screen.icon} label={screen.label} />
                ))}
                {menuGroups.length > 0 && <ScreenMenu groups={menuGroups} />}
              </nav>

            <div className="ml-auto flex items-center gap-1.5">
              <UserMenu onOpenSettings={() => setIsSettingsOpen(true)} triggerRef={userMenuTriggerRef} />
            </div>
          </header>

          {/* min-h-0 is load-bearing here: without it, a flex child defaults
              to min-height:auto and refuses to shrink below its content
              size, which breaks the single-inner-scrollbar layout below --
              the page would grow the whole window instead of scrolling
              internally. Each routed page owns exactly one scroll region at
              full width (edge-to-edge, scrollbar flush against the browser
              edge) with its content centered inside via its own max-w
              wrapper. */}
          <main className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
            <Outlet />
          </main>
        </div>
      </div>

      <SettingsDialog
        open={isSettingsOpen}
        onOpenChange={setIsSettingsOpen}
        triggerRef={userMenuTriggerRef}
      />
    </div>
  )
}

function NavTab({ to, icon: Icon, label }: { to: string; icon: typeof MessageSquare; label: string }) {
  return (
    <NavLink
      to={to}
      end
      title={label}
      aria-label={label}
      className={({ isActive }) =>
        cn(
          'flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm font-medium transition-colors sm:px-3',
          isActive
            ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
            : 'text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]',
        )
      }
    >
      <Icon className="h-4 w-4" />
      {/* Icon-only below sm to keep the header from overflowing on narrow
          viewports -- the NavLink's own aria-label/title above still give
          every state (including screen readers and a hover tooltip) the
          full name even when the text is visually hidden. */}
      <span className="hidden sm:inline">{label}</span>
    </NavLink>
  )
}
