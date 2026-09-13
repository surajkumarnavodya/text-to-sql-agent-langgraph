import { useTranslation } from 'react-i18next'
import { Expander } from '@/components/ui/expander'
import type { AttemptRecord } from '@/lib/types'

const OUTCOME_ICONS: Record<string, string> = {
  succeeded: '✅',
  safety_violation: '🛑',
  timeout: '⏱️',
  high_cost: '💰',
  rate_limited: '🐌',
  off_topic: '🚫',
}

export function RetryTimeline({ attempts }: { attempts: AttemptRecord[] }) {
  const { t } = useTranslation()
  if (attempts.length === 0) return null

  return (
    <Expander title={`🔁 ${t('sql.retryTimeline')} (${attempts.length} attempt${attempts.length === 1 ? '' : 's'})`}>
      <div className="flex flex-col gap-2 text-xs">
        {attempts.map((attempt) => (
          <div key={attempt.attempt} className="border-b border-[var(--border)] pb-2 last:border-none last:pb-0">
            <p className="font-medium">
              {OUTCOME_ICONS[attempt.outcome] ?? '•'} Attempt {attempt.attempt}: {attempt.outcome}
              {attempt.will_retry && ' (will retry)'}
            </p>
            {attempt.sql && (
              <pre className="mt-1 whitespace-pre-wrap break-words rounded bg-[var(--muted)] p-2 font-mono">
                {attempt.sql}
              </pre>
            )}
            {attempt.error && <p className="mt-1 text-[var(--danger)]">{attempt.error}</p>}
          </div>
        ))}
      </div>
    </Expander>
  )
}
