import { ChatInput } from '@/components/chat/ChatInput'
import { ChatLiveRegion } from '@/components/chat/ChatLiveRegion'
import { PendingTurn } from '@/components/chat/PendingTurn'
import { TurnCard } from '@/components/chat/TurnCard'
import { useEffect, useRef } from 'react'
import { useHealth } from '@/hooks/queries'
import { useChatStore } from '@/store/chatStore'

export function Chat() {
  const health = useHealth()

  const queryHistory = useChatStore((state) => state.queryHistory)
  const pendingQuestion = useChatStore((state) => state.pendingQuestion)

  const isMultiDb = (health.data?.databases.length ?? 0) > 1

  // Keep the newest turn in view, like the history list reaching its bottom:
  // when a question is added or an answer lands, scroll the message region to
  // its end. Motion is skipped for reduced-motion users.
  const scrollRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollTo({ top: el.scrollHeight, behavior: reduceMotion ? 'auto' : 'smooth' })
  }, [queryHistory.length, pendingQuestion])

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
      <ChatLiveRegion
        pendingQuestion={pendingQuestion}
        latestEntryId={queryHistory.at(-1)?.entryId ?? null}
      />
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex min-h-full max-w-4xl flex-col px-4 pb-4 pt-6">
          <div className="flex flex-col gap-8">
            {queryHistory.map((entry) => (
              <TurnCard key={entry.entryId} entry={entry} isMultiDb={isMultiDb} />
            ))}
            {pendingQuestion && <PendingTurn pendingQuestion={pendingQuestion} />}
          </div>

          {/* Sticky at the bottom of the scroll region: the composer stays in view
              while the messages scroll beneath it, and the scrollbar itself runs
              all the way to the bottom edge of the page. */}
          <div className="sticky bottom-0 mt-auto bg-[var(--background)] pb-4 pt-2">
            <ChatInput />
          </div>
        </div>
      </div>
    </div>
  )
}
