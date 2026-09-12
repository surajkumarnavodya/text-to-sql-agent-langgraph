import { Sparkles } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useElapsedSeconds } from '@/hooks/useElapsedSeconds'
import { cn } from '@/lib/utils'

type TimingBadgeProps =
  | { mode: 'pending'; startedAt: number }
  | { mode: 'done'; durationMs: number }

/** Perplexity/ChatGPT-style "Thinking Ns" -> "Answered in Ns" pill.
 *
 * While pending, the icon/label pulse (Tailwind's `animate-pulse`) and the
 * seconds counter ticks live -- a clear, continuously-updating signal that
 * the agent is still working, not stalled, addressing the "instead of a
 * static 'Loading' message, show it's actively in progress" request. Once
 * a turn resolves, its card switches to `mode: 'done'` with the actual
 * measured duration -- a plain, static, non-animated summary.
 */
export function TimingBadge(props: TimingBadgeProps) {
  const { t } = useTranslation()
  const liveSeconds = useElapsedSeconds(
    props.mode === 'pending' ? props.startedAt : 0,
    props.mode === 'pending',
  )
  const seconds = props.mode === 'pending' ? liveSeconds : props.durationMs / 1000

  return (
    <div
      className={cn(
        'inline-flex w-fit items-center gap-1.5 rounded-full bg-[var(--muted)] px-2.5 py-1 text-xs text-[var(--muted-foreground)]',
        props.mode === 'pending' && 'animate-pulse',
      )}
    >
      <Sparkles className="h-3.5 w-3.5" />
      {props.mode === 'pending' ? (
        <span>
          {t('common.thinking')} {seconds.toFixed(0)}s…
        </span>
      ) : (
        <span>{t('common.answeredIn', { seconds: seconds.toFixed(1) })}</span>
      )}
    </div>
  )
}
