import { BookOpen, Database, MessageSquare, Settings } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink, Outlet } from 'react-router-dom'
import { ThinkingOverlay } from '@/components/chat/ThinkingOverlay'
import { ThemeToggle } from '@/components/settings/ThemeToggle'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { HistoryDrawer } from './HistoryDrawer'

/** Full-viewport application shell. The history drawer has exactly one
 * control -- the gear button below -- per the redesign spec: no separate
 * sidebar toggle, hamburger, or collapse button exists anywhere else, and
 * the drawer itself renders no close control of its own (see
 * HistoryDrawer). Initial state is always closed. */
export function AppShell() {
  const { t } = useTranslation()
  const [isHistoryOpen, setIsHistoryOpen] = useState(false)

  return (
    <div className="h-screen w-screen overflow-hidden bg-[var(--background)] text-[var(--foreground)]">
      <div className="flex h-full flex-col">
        <header className="flex h-14 shrink-0 items-center gap-4 border-b border-[var(--border)] bg-[var(--header)] px-4">
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
          </nav>

          <div className="ml-auto flex items-center gap-1.5">
            <ThemeToggle />
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setIsHistoryOpen((current) => !current)}
              aria-label={isHistoryOpen ? t('header.closeHistory') : t('header.openHistory')}
              aria-expanded={isHistoryOpen}
              title={isHistoryOpen ? t('header.closeHistory') : t('header.openHistory')}
            >
              <Settings
                className={cn(
                  'h-4 w-4 transition-transform duration-300 motion-reduce:transition-none',
                  isHistoryOpen && 'rotate-90',
                )}
              />
            </Button>
          </div>
        </header>

        {/* min-h-0 is load-bearing here: without it, a flex child defaults
            to min-height:auto and refuses to shrink below its content
            size, which breaks the single-inner-scrollbar layout below --
            the page would grow the whole window instead of scrolling
            internally. Each routed page owns exactly one scroll region at
            full width (edge-to-edge, scrollbar flush against the browser
            edge) with its content centered inside via its own max-w
            wrapper. The right padding shift (md:pr-*) is what "reserves a
            controlled portion of the screen" for the drawer on desktop
            without covering the workspace -- on mobile the drawer is a
            full overlay instead, so no shift happens there. */}
        {/* `relative` scopes ThinkingOverlay's `absolute inset-0` to just
            this content pane -- header stays visible/usable the whole time
            a question is in flight, only the answer area itself takes
            over while waiting. */}
        <main
          className={cn(
            'relative min-h-0 min-w-0 flex-1 overflow-hidden transition-[padding] duration-300 motion-reduce:transition-none',
            isHistoryOpen && 'md:pr-96',
          )}
        >
          <Outlet />
          <ThinkingOverlay />
        </main>
      </div>

      <HistoryDrawer isOpen={isHistoryOpen} onClose={() => setIsHistoryOpen(false)} />
    </div>
  )
}

function NavTab({ to, icon: Icon, label }: { to: string; icon: typeof MessageSquare; label: string }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        cn(
          'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
          isActive
            ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
            : 'text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]',
        )
      }
    >
      <Icon className="h-4 w-4" />
      {label}
    </NavLink>
  )
}
