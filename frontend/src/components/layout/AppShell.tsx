import {
  BookOpen,
  Building2,
  Database,
  ImageIcon,
  MessageSquare,
  Plug,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink, Outlet } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { useSettingsStore } from '@/store/settingsStore'
import { MobileNav } from './MobileNav'
import { SettingsDialog } from './SettingsDialog'
import { Sidebar } from './Sidebar'
import { SidebarToggle } from './SidebarToggle'
import { UserMenu } from './UserMenu'

const SIDEBAR_ID = 'app-sidebar'

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
  // Database Onboarding and SME Semantic Review are the two nav tabs
  // gated on role, rather than always visible like the others -- UX only
  // (the real enforcement is server-side, see each page's own
  // docstring), chosen here because unlike Chat/Knowledge Sources/Media
  // Search, these pages are entirely non-functional for an account with
  // neither role (they have no "read-only" use for a plain user), so
  // showing a tab that leads nowhere useful would be worse UX than
  // omitting it. Both share the identical admin/analyst role gate
  // (ONBOARDING_MANAGE/ONBOARDING_REVIEW and CATALOG_MANAGE/
  // CATALOG_REVIEW sit at the same two RBAC tiers -- see
  // `identity/rbac.py`), so one boolean serves both.
  // Selects the already-stable `user` object itself, not a derived array --
  // a selector returning a freshly-allocated `[]` fallback on every call
  // (an earlier version of this line did exactly that) breaks Zustand's
  // snapshot-equality check and triggers React's "Maximum update depth
  // exceeded" infinite-loop guard, a real regression this app's own test
  // suite caught (AppShell.test.tsx).
  const localUser = useLocalAuthStore((state) => state.user)
  const canSeeReviewTabs = Boolean(
    localUser?.roles.includes('admin') || localUser?.roles.includes('analyst'),
  )
  // Platform Admin (Prompt 28) is gated on a *different* dimension than
  // the two tabs above -- `platform_admin`, never satisfied merely by
  // holding the tenant-scoped `admin` role (see `identity.rbac
  // .Permission.PLATFORM_ADMIN`'s own docstring for the full "platform-
  // admin versus tenant-admin" rationale). Kept as its own boolean, not
  // folded into `canSeeReviewTabs`, specifically so a tenant's own admin
  // never sees a tab that would 403 for them.
  const canSeePlatformAdmin = Boolean(localUser?.roles.includes('platform_admin'))
  // Tenant Admin (Prompt 29) introduces no new permission/role at all --
  // it's visible to the same roles `identity.rbac
  // .Permission.ADMIN_DASHBOARD_READ` already grants (admin/auditor/
  // manager), a different set from `canSeeReviewTabs`'s admin/analyst
  // (an analyst has no business-administration capability; an
  // auditor/manager has no onboarding/catalog-review capability).
  const canSeeTenantAdmin = Boolean(
    localUser?.roles.includes('admin') ||
      localUser?.roles.includes('auditor') ||
      localUser?.roles.includes('manager'),
  )
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

            <nav className="flex gap-1">
              <NavTab to="/" icon={MessageSquare} label={t('nav.chat')} />
              <NavTab to="/knowledge-sources" icon={BookOpen} label={t('nav.knowledgeSources')} />
              <NavTab to="/media-search" icon={ImageIcon} label={t('nav.mediaSearch')} />
              {canSeeReviewTabs && (
                <NavTab to="/db-onboarding" icon={Plug} label={t('nav.dbOnboarding')} />
              )}
              {canSeeReviewTabs && (
                <NavTab to="/semantic-review" icon={ShieldCheck} label={t('nav.semanticReview')} />
              )}
              {canSeePlatformAdmin && (
                <NavTab to="/platform-admin" icon={ShieldAlert} label={t('nav.platformAdmin')} />
              )}
              {canSeeTenantAdmin && (
                <NavTab to="/tenant-admin" icon={Building2} label={t('nav.tenantAdmin')} />
              )}
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
          'flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm font-medium transition-colors sm:px-3',
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
