import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { PendingQuestion } from '@/store/chatStore'

/** A visually-hidden `aria-live="polite"` region announcing exactly two
 * moments to screen-reader users: a question starting to process, and its
 * answer arriving -- never the live "Thinking Ns…" tick
 * (`TimingBadge.tsx`), which would otherwise re-announce every second and
 * bury the user in noise. Guarded against firing on mount (loading an
 * existing conversation's history must never announce anything) by
 * seeding its "previous" refs from the very first render instead of from
 * empty defaults. */
export function ChatLiveRegion({
  pendingQuestion,
  latestEntryId,
}: {
  pendingQuestion: PendingQuestion | null
  latestEntryId: string | null
}) {
  const { t } = useTranslation()
  const [announcement, setAnnouncement] = useState('')
  const wasPending = useRef(pendingQuestion !== null)
  const previousLatestEntryId = useRef(latestEntryId)

  useEffect(() => {
    const isPending = pendingQuestion !== null
    if (isPending && !wasPending.current) {
      setAnnouncement(t('chat.liveThinking'))
    } else if (!isPending && wasPending.current && latestEntryId !== previousLatestEntryId.current) {
      setAnnouncement(t('chat.liveAnswerReady'))
    }
    wasPending.current = isPending
    previousLatestEntryId.current = latestEntryId
  }, [pendingQuestion, latestEntryId, t])

  return (
    <div role="status" aria-live="polite" className="sr-only">
      {announcement}
    </div>
  )
}
