import { Pause, Play, RotateCcw, Square, Volume2 } from 'lucide-react'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { useToast } from '@/components/ui/toast'
import { useReadAloud } from '@/hooks/useReadAloud'

/** "Read aloud" for one completed answer, placed in the same action row as Copy
 * and Download. Idle shows a single button. While this answer is speaking, the row
 * shows its status plus Pause/Resume and Stop. A failed read offers Retry.
 *
 * `answer` is the plain-markdown export (`buildAnswerMarkdown`) that Copy and
 * Download already use, so there is no DOM scraping and no second copy of the answer. */
export function ReadAloudButton({ entryId, answer }: { entryId: string; answer: string }) {
  const { t, i18n } = useTranslation()
  const { toast } = useToast()
  const speech = useReadAloud(entryId)
  const isPlaying = speech.status === 'reading' || speech.status === 'paused'
  const isPaused = speech.status === 'paused'

  // Errors are announced once, on the transition into the error state.
  useEffect(() => {
    if (speech.status === 'error') toast({ title: t('readAloud.failed'), variant: 'error' })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to status changes, not toast/t identity
  }, [speech.status])

  const handleRead = () => {
    if (!speech.isSupported) {
      toast({ title: t('readAloud.unsupported'), variant: 'info' })
      return
    }
    if (speech.start(answer, i18n.language) === 'empty') {
      toast({ title: t('readAloud.empty'), variant: 'info' })
    }
  }

  // A polite live region that stays mounted, so state changes are announced
  // (a region mounted together with its content is often skipped by screen readers).
  const announcement = isPaused ? t('readAloud.paused') : isPlaying ? t('readAloud.reading') : ''

  return (
    <div className="flex items-center gap-1">
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
      {isPlaying ? (
        <>
          <span aria-hidden="true" className="text-xs text-[var(--muted-foreground)]">
            {isPaused ? t('readAloud.paused') : t('readAloud.reading')}
          </span>
          <Button
            size="sm"
            variant="ghost"
            onClick={isPaused ? speech.resume : speech.pause}
            aria-label={isPaused ? t('readAloud.resumeReading') : t('readAloud.pauseReading')}
            title={isPaused ? t('readAloud.resumeReading') : t('readAloud.pauseReading')}
          >
            {isPaused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
            {isPaused ? t('readAloud.resume') : t('readAloud.pause')}
          </Button>
          <Button
            size="icon"
            variant="ghost"
            onClick={speech.stop}
            aria-label={t('readAloud.stopReading')}
            title={t('readAloud.stopReading')}
          >
            <Square className="h-3.5 w-3.5" />
          </Button>
        </>
      ) : (
        <Button
          size="sm"
          variant="ghost"
          onClick={handleRead}
          aria-disabled={speech.isSupported ? undefined : true}
          title={speech.isSupported ? t('readAloud.read') : t('readAloud.unsupported')}
        >
          {speech.status === 'error' ? (
            <RotateCcw className="h-3.5 w-3.5" />
          ) : (
            <Volume2 className="h-3.5 w-3.5" />
          )}
          {speech.status === 'error' ? t('readAloud.retry') : t('readAloud.read')}
        </Button>
      )}
    </div>
  )
}
