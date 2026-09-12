import { BookOpen, MessageSquare, PanelLeft } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink, Outlet } from 'react-router-dom'
import { ThinkingOverlay } from '@/components/chat/ThinkingOverlay'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useSettingsStore } from '@/store/settingsStore'
import { Sidebar } from './Sidebar'

export function AppShell() {
  const { t } = useTranslation()
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false)
  const sidebarCollapsed = useSettingsStore((state) => state.sidebarCollapsed)
  const toggleSidebar = useSettingsStore((state) => state.toggleSidebar)

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--background)] text-[var(--foreground)]">
      {/* Desktop sidebar -- fully hidden (not just narrowed) when
          collapsed, ChatGPT-style; the header button below is what brings
          it back. */}
      {!sidebarCollapsed && (
        <div className="hidden md:block">
          <Sidebar />
        </div>
      )}

      {/* Mobile off-canvas sidebar */}
      {mobileSidebarOpen && (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setMobileSidebarOpen(false)}
            aria-hidden="true"
          />
          <div className="relative z-50">
            <Sidebar onClose={() => setMobileSidebarOpen(false)} />
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-[var(--border)] bg-[var(--header)] px-4">
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            onClick={() => setMobileSidebarOpen(true)}
            aria-label="Open sidebar"
          >
            <PanelLeft className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="hidden md:flex"
            onClick={toggleSidebar}
            aria-label={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
            aria-expanded={!sidebarCollapsed}
            title={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'}
          >
            <PanelLeft className="h-4 w-4" />
          </Button>
          <nav className="flex gap-1">
            <NavTab to="/" icon={MessageSquare} label={t('nav.chat')} />
            <NavTab to="/knowledge-sources" icon={BookOpen} label={t('nav.knowledgeSources')} />
          </nav>
        </header>
        {/* min-h-0 is load-bearing here: without it, a flex child defaults
            to min-height:auto and refuses to shrink below its content
            size, which breaks the single-inner-scrollbar layout below --
            the page would grow the whole window instead of scrolling
            internally. Each routed page owns exactly one scroll region at
            full width (edge-to-edge, scrollbar flush against the browser
            edge) with its content centered inside via its own max-w
            wrapper -- the ChatGPT layout pattern. */}
        {/* `relative` scopes ThinkingOverlay's `absolute inset-0` to just
            this content pane -- header and sidebar stay visible/usable the
            whole time a question is in flight, only the answer area itself
            takes over while waiting. */}
        <main className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
          <Outlet />
          <ThinkingOverlay />
        </main>
      </div>
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
