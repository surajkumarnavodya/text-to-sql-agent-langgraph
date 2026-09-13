import { CheckCircle2, Download, RefreshCw, XCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { AccentColorPicker } from '@/components/settings/AccentColorPicker'
import { FontPicker } from '@/components/settings/FontPicker'
import { LanguageSelector } from '@/components/settings/LanguageSelector'
import { ThemeToggle } from '@/components/settings/ThemeToggle'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { useHealth, useRefreshSchema, useSchemaTables } from '@/hooks/queries'
import { usePwaInstall } from '@/hooks/usePwaInstall'
import { useChatStore } from '@/store/chatStore'
import { useSettingsStore } from '@/store/settingsStore'
import { SectionBody, SectionHeader } from './SectionHeader'

/** Everything that used to live in the always-visible left sidebar and
 * isn't "chat history" -- appearance, connection/schema status, and the
 * AI-insight toggle. Folded into a collapsed-by-default block at the
 * bottom of the history drawer so none of that functionality is lost, but
 * none of it competes with chat history for primary screen space (see
 * CLAUDE.md-adjacent redesign notes: the drawer's one job is history, this
 * is secondary and stays out of the way by default). */
export function HistorySettingsSection() {
  const { t } = useTranslation()
  const health = useHealth()
  const schemaTables = useSchemaTables()
  const refreshSchemaMutation = useRefreshSchema()
  const { canInstall, promptInstall } = usePwaInstall()

  const enableInsight = useChatStore((state) => state.enableInsight)
  const setEnableInsight = useChatStore((state) => state.setEnableInsight)
  const voiceModeEnabled = useSettingsStore((state) => state.voiceModeEnabled)
  const setVoiceModeEnabled = useSettingsStore((state) => state.setVoiceModeEnabled)

  const databases = health.data?.databases ?? []
  const isMultiDb = databases.length > 1
  const tablesByDatabase = new Map<string, string[]>()
  for (const table of schemaTables.data?.tables ?? []) {
    const list = tablesByDatabase.get(table.database) ?? []
    list.push(table.table_name)
    tablesByDatabase.set(table.database, list)
  }

  return (
    <div className="flex flex-col gap-3 border-t border-[var(--border)] p-3">
      {canInstall && (
        <Button variant="secondary" size="sm" onClick={() => void promptInstall()}>
          <Download className="h-3.5 w-3.5" />
          {t('sidebar.installApp')}
        </Button>
      )}

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

      <section>
        <SectionHeader id="connection" title={t('sidebar.connectionStatus')} />
        <SectionBody id="connection">
          {databases.map((db) => (
            <div key={db.name} className="rounded-md border border-[var(--border)] bg-[var(--card)] p-2 text-xs">
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
        </SectionBody>
      </section>

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

      <section className="flex items-center justify-between">
        <label htmlFor="enable-insight" className="text-sm">
          {t('sidebar.generateInsight')}
        </label>
        <Switch id="enable-insight" checked={enableInsight} onCheckedChange={setEnableInsight} />
      </section>

      {health.data?.voice_enabled && (
        <section className="flex items-center justify-between">
          <label htmlFor="voice-mode" className="text-sm">
            {t('voice.settingsLabel')}
          </label>
          <Switch id="voice-mode" checked={voiceModeEnabled} onCheckedChange={setVoiceModeEnabled} />
        </section>
      )}
    </div>
  )
}
