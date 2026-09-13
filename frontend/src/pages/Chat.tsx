import { Database } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { ChatInput } from '@/components/chat/ChatInput'
import { SuggestedPrompts } from '@/components/chat/SuggestedPrompts'
import { TurnCard } from '@/components/chat/TurnCard'
import { useHealth } from '@/hooks/queries'
import { useChatStore } from '@/store/chatStore'

export function Chat() {
  const { t } = useTranslation()
  const health = useHealth()

  const queryHistory = useChatStore((state) => state.queryHistory)
  const pendingQuestion = useChatStore((state) => state.pendingQuestion)
  const askQuestion = useChatStore((state) => state.askQuestion)

  const isMultiDb = (health.data?.databases.length ?? 0) > 1

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
            <div className="flex min-h-[60vh] flex-col items-center justify-center gap-5 text-center">
              <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[var(--accent-soft)] text-[var(--accent)]">
                <Database className="h-6 w-6" />
              </span>
              <div className="flex flex-col gap-1.5">
                <h1 className="text-2xl font-semibold tracking-tight">{t('workspace.heading')}</h1>
                <p className="max-w-md text-sm text-[var(--muted-foreground)]">{t('workspace.subheading')}</p>
              </div>
              <SuggestedPrompts onSelect={(prompt) => void askQuestion(prompt)} />
            </div>
          )}

          <div className="flex flex-col gap-8">
            {queryHistory.map((entry) => (
              <TurnCard key={entry.entryId} entry={entry} isMultiDb={isMultiDb} />
            ))}
          </div>
        </div>
      </div>
      <div className="mx-auto w-full max-w-4xl px-4 pb-4">
        <ChatInput
          onSubmit={(question) => void askQuestion(question)}
          disabled={pendingQuestion !== null}
          isLoading={pendingQuestion !== null}
        />
      </div>
    </div>
  )
}
