import { ThumbsDown, ThumbsUp } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useChatStore } from '@/store/chatStore'

type Rating = 'positive' | 'negative'

/** General-purpose like/dislike feedback on ANY assistant answer -- SQL,
 * document/policy RAG, web search, or media -- shown once per turn
 * regardless of source (unlike GoldenFeedbackWidget, which only ever
 * appears after a confirmed, executed SQL result and exists specifically
 * to grow the few-shot golden dataset -- this is additive to that, not a
 * replacement).
 *
 * Clicking either button opens a small modal for an optional comment
 * before submitting -- this product's own "Give positive/negative
 * feedback" pattern (rating + optional details, then Submit/Cancel).
 * Re-clicking the other button afterward changes the rating rather than
 * being ignored, since this is a review-quality signal a user may want to
 * correct, not a one-shot action. */
export function ResponseFeedbackWidget({ entryId }: { entryId: string }) {
  const { t } = useTranslation()
  const giveMessageFeedback = useChatStore((state) => state.giveMessageFeedback)
  const currentRating = useChatStore((state) => state.messageFeedbackGiven.get(entryId) ?? null)
  const [pendingRating, setPendingRating] = useState<Rating | null>(null)
  const [comment, setComment] = useState('')
  const [toast, setToast] = useState<string | null>(null)

  const openModal = (rating: Rating) => {
    setComment('')
    setPendingRating(rating)
  }

  const submit = () => {
    if (!pendingRating) return
    void giveMessageFeedback(entryId, pendingRating, comment.trim() || null)
    setToast(t('feedback.submittedToast'))
    setPendingRating(null)
  }

  return (
    <>
      <div className="flex items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t('feedback.ratePositive')}
          aria-pressed={currentRating === 'positive'}
          onClick={() => openModal('positive')}
          className={currentRating === 'positive' ? 'text-[var(--success)]' : undefined}
        >
          <ThumbsUp className="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t('feedback.rateNegative')}
          aria-pressed={currentRating === 'negative'}
          onClick={() => openModal('negative')}
          className={currentRating === 'negative' ? 'text-[var(--danger)]' : undefined}
        >
          <ThumbsDown className="h-4 w-4" />
        </Button>
        {toast && <span className="text-xs text-[var(--muted-foreground)]">{toast}</span>}
      </div>

      <Dialog open={pendingRating !== null} onOpenChange={(open) => !open && setPendingRating(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {pendingRating === 'positive' ? t('feedback.positiveTitle') : t('feedback.negativeTitle')}
            </DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <label
              htmlFor={`feedback-comment-${entryId}`}
              className="text-sm text-[var(--muted-foreground)]"
            >
              {t('feedback.detailsLabel')}
            </label>
            <textarea
              id={`feedback-comment-${entryId}`}
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              placeholder={t('feedback.detailsPlaceholder')}
              rows={4}
              className="w-full resize-none rounded-md border border-[var(--border)] bg-[var(--background)] p-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
            />
            <p className="text-xs italic text-[var(--muted-foreground)]">{t('feedback.privacyNotice')}</p>
          </div>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => setPendingRating(null)}>
              {t('feedback.cancel')}
            </Button>
            <Button type="button" variant="primary" onClick={submit}>
              {t('feedback.submit')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
