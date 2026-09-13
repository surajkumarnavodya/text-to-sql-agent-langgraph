import { ThumbsDown, ThumbsUp } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useChatStore } from '@/store/chatStore'

/** Golden-example thumbs feedback widget: only shown for a confirmed
 * (executed) result, once per history entry per session. */
export function GoldenFeedbackWidget({ entryId }: { entryId: string }) {
  const { t } = useTranslation()
  const giveGoldenFeedback = useChatStore((state) => state.giveGoldenFeedback)
  const alreadyGiven = useChatStore((state) => state.goldenFeedbackGiven.has(entryId))
  const [toast, setToast] = useState<string | null>(null)

  if (alreadyGiven) {
    return toast ? <p className="text-xs text-[var(--muted-foreground)]">{toast}</p> : null
  }

  const respond = (thumbsUp: boolean) => {
    setToast(thumbsUp ? t('feedback.savedToast') : t('feedback.thanksToast'))
    void giveGoldenFeedback(entryId, thumbsUp)
  }

  return (
    <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
      <span>{t('feedback.question')}</span>
      <button
        type="button"
        aria-label="Thumbs up"
        onClick={() => respond(true)}
        className="rounded p-1 hover:bg-[var(--muted)] hover:text-[var(--success)]"
      >
        <ThumbsUp className="h-4 w-4" />
      </button>
      <button
        type="button"
        aria-label="Thumbs down"
        onClick={() => respond(false)}
        className="rounded p-1 hover:bg-[var(--muted)] hover:text-[var(--danger)]"
      >
        <ThumbsDown className="h-4 w-4" />
      </button>
    </div>
  )
}
