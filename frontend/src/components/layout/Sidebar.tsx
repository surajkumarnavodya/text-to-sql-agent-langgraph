import { CheckCircle2, Download, RefreshCw, Trash2, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { AccentColorPicker } from '@/components/settings/AccentColorPicker'
import { FontPicker } from '@/components/settings/FontPicker'
import { LanguageSelector } from '@/components/settings/LanguageSelector'
import { ThemeToggle } from '@/components/settings/ThemeToggle'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { useHealth, useRefreshSchema, useSchemaTables } from '@/hooks/queries'
import { usePwaInstall } from '@/hooks/usePwaInstall'
import { statusLabel } from '@/lib/history'
import { useChatStore } from '@/store/chatStore'
import { SectionBody, SectionHeader } from './SectionHeader'

export function Sidebar({ onClose }: { onClose?: () => void }) {
  const { t } = useTranslation()
  const health = useHealth()
  const schemaTables = useSchemaTables()
  const refreshSchemaMutation = useRefreshSchema()
  const { canInstall, promptInstall } = usePwaInstall()

  const enableInsight = useChatStore((state) => state.enableInsight)
  const setEnableInsight = useChatStore((state) => state.setEnableInsight)
  const queryHistory = useChatStore((state) => state.queryHistory)
  const rerunEntry = useChatStore((state) => state.rerunEntry)
  const clearHistory = useChatStore((state) => state.clearHistory)
  const lastRoutedDatabase = queryHistory.at(-1)?.finalState.database

  // Every turn is always fully rendered on the Chat page now (see
  // TurnCard) -- "View" just scrolls that turn back into view rather than
  // swapping some single global "current" state.
  const scrollToEntry = (entryId: string) => {
    document.getElementById(`turn-${entryId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const databases = health.data?.databases ?? []
  const isMultiDb = databases.length > 1
  const tablesByDatabase = new Map<string, string[]>()
  for (const table of schemaTables.data?.tables ?? []) {
    const list = tablesByDatabase.get(table.database) ?? []
    list.push(table.table_name)
    tablesByDatabase.set(table.database, list)
  }

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col gap-4 overflow-y-auto border-r border-[var(--border)] bg-[var(--sidebar)] p-4 text-[var(--sidebar-foreground)]">
      <div className="flex items-center justify-between">
        <span className="text-lg font-bold">🗄️ Text-to-SQL</span>
        {onClose && (
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close sidebar">
            ✕
          </Button>
        )}
      </div>

      {canInstall && (
        <Button variant="secondary" size="sm" onClick={() => void promptInstall()}>
          <Download className="h-3.5 w-3.5" />
          {t('sidebar.installApp')}
        </Button>
      )}

      {/* Appearance */}
      <section>
        <SectionHeader id="appearance" title={t('sidebar.appearance')} />
        <SectionBody id="appearance">
          <div className="flex items-center justify-between">
            <span className="text-sm">{t('sidebar.theme')}</span>
            <ThemeToggle />
          </div>
          <div className="flex items-center justify-between">
            <span className="text-sm">{t('sidebar.accentColor')}</span>
            <AccentColorPicker />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-sm">{t('sidebar.font')}</span>
            <FontPicker />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-sm">{t('sidebar.language')}</span>
            <LanguageSelector />
          </div>
        </SectionBody>
      </section>

      {/* Connection status */}
      <section>
        <SectionHeader id="connection" title={t('sidebar.connectionStatus')} />
        <SectionBody id="connection">
          {databases.map((db) => (
            <div
              key={db.name}
              className="rounded-md border border-[var(--border)] bg-[var(--card)] p-2 text-xs"
            >
              <div className="flex items-center gap-2 font-medium">
                {db.connection.ok ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-[var(--success)]" />
                ) : (
                  <XCircle className="h-3.5 w-3.5 text-[var(--danger)]" />
                )}
                {db.name}
              </div>
              <p className="mt-1 text-[var(--muted-foreground)]">{db.connection.detail}</p>
            </div>
          ))}
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" className="flex-1" onClick={() => void health.refetch()}>
              {t('sidebar.testConnection')}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              className="flex-1"
              onClick={() => refreshSchemaMutation.mutate()}
              disabled={refreshSchemaMutation.isPending}
            >
              <RefreshCw className={refreshSchemaMutation.isPending ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
              {t('sidebar.refreshSchema')}
            </Button>
          </div>
          {isMultiDb && lastRoutedDatabase && (
            <p className="text-xs text-[var(--muted-foreground)]">
              🧭 {t('sidebar.routedTo')}: <strong>{lastRoutedDatabase}</strong>
            </p>
          )}
        </SectionBody>
      </section>

      {/* Discovered tables */}
      <section>
        <SectionHeader
          id="tables"
          title={`${t('sidebar.discoveredTables')} (${schemaTables.data?.tables.length ?? 0})`}
        />
        <SectionBody id="tables">
          {[...tablesByDatabase.entries()].map(([dbName, tables]) => (
            <div key={dbName} className="text-xs">
              {isMultiDb && <p className="mb-1 font-semibold">{dbName}</p>}
              <ul className="flex flex-col gap-0.5 text-[var(--muted-foreground)]">
                {tables.map((name) => (
                  <li key={name} className="truncate">
                    {name}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </SectionBody>
      </section>

      {/* Options */}
      <section className="flex items-center justify-between">
        <label htmlFor="enable-insight" className="text-sm">
          💡 {t('sidebar.generateInsight')}
        </label>
        <Switch id="enable-insight" checked={enableInsight} onCheckedChange={setEnableInsight} />
      </section>

      {/* History */}
      <section className="flex flex-1 flex-col overflow-hidden">
        <SectionHeader id="history" title={`📜 ${t('sidebar.history')} (${queryHistory.length})`} />
        <SectionBody id="history">
          <div className="flex flex-col gap-2 overflow-y-auto">
            {queryHistory.length === 0 && (
              <p className="text-xs text-[var(--muted-foreground)]">{t('sidebar.noHistory')}</p>
            )}
            {[...queryHistory].reverse().map((entry) => {
              const { icon, label } = statusLabel(entry)
              return (
                <div
                  key={entry.entryId}
                  className="rounded-md border border-[var(--border)] bg-[var(--card)] p-2 text-xs"
                >
                  <div className="flex items-center gap-1 font-medium">
                    <span>{icon}</span>
                    <span className="capitalize">{label}</span>
                  </div>
                  <p className="mt-1 truncate" title={entry.question}>
                    {entry.question}
                  </p>
                  <div className="mt-1.5 flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => scrollToEntry(entry.entryId)}>
                      {t('chat.view')}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void rerunEntry(entry.entryId)}>
                      {t('chat.rerun')}
                    </Button>
                  </div>
                </div>
              )
            })}
          </div>
          {queryHistory.length > 0 && (
            <Button size="sm" variant="secondary" onClick={clearHistory}>
              <Trash2 className="h-3.5 w-3.5" />
              {t('sidebar.clearHistory')}
            </Button>
          )}
        </SectionBody>
      </section>

      {isMultiDb && (
        <Badge tone="neutral" className="w-fit">
          {databases.length} databases configured
        </Badge>
      )}
    </aside>
  )
}
