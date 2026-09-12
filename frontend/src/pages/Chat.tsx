import { useTranslation } from 'react-i18next'
import { ChatInput } from '@/components/chat/ChatInput'
import { TurnCard } from '@/components/chat/TurnCard'
import { Badge } from '@/components/ui/badge'
import { useHealth, useSchemaTables } from '@/hooks/queries'
import { useChatStore } from '@/store/chatStore'

export function Chat() {
  const { t } = useTranslation()
  const health = useHealth()
  const schemaTables = useSchemaTables()

  const queryHistory = useChatStore((state) => state.queryHistory)
  const pendingQuestion = useChatStore((state) => state.pendingQuestion)
  const askQuestion = useChatStore((state) => state.askQuestion)

  const isMultiDb = (health.data?.databases.length ?? 0) > 1
  const tableCount = schemaTables.data?.tables.length ?? 0

  return (
    // Full-width, single-scrollbar layout (matches ChatGPT): this outer
    // column never scrolls itself -- the middle region below is the only
    // scroll container, spanning the full remaining width so its
    // scrollbar sits flush against the browser's right edge, with the
    // actual message content centered inside it via max-w-4xl/mx-auto.
    //
    // Every turn ever asked this session renders here, in order, each
    // fully expanded (question directly above its own answer) -- not just
    // the latest one -- so scrolling up always reveals the full prior
    // conversation, and each turn keeps its own editable SQL/confirmed
    // result independently (see TurnCard).
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-4 py-6">
          {queryHistory.length === 0 && !pendingQuestion && (
            <div className="mb-6 flex flex-col items-center gap-2 text-center">
              <span className="text-3xl">🗄️</span>
              <h1 className="text-xl font-bold">{t('app.title')}</h1>
              <p className="max-w-lg text-sm text-[var(--muted-foreground)]">{t('app.subtitle')}</p>
              <div className="flex flex-wrap justify-center gap-2">
                <Badge tone="accent">
                  {isMultiDb
                    ? `${health.data?.databases.length} databases`
                    : (health.data?.databases[0]?.name ?? '—')}
                </Badge>
                <Badge tone="neutral">{tableCount} tables</Badge>
              </div>
            </div>
          )}

          <div className="flex flex-col gap-8">
            {queryHistory.map((entry) => (
              <TurnCard key={entry.entryId} entry={entry} isMultiDb={isMultiDb} />
            ))}
          </div>
        </div>
      </div>
      <div className="mx-auto w-full max-w-4xl">
        <ChatInput onSubmit={(question) => void askQuestion(question)} disabled={pendingQuestion !== null} />
      </div>
    </div>
  )
}
