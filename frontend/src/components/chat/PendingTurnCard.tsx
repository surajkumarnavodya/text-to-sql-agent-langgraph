import type { PendingQuestion } from '@/store/chatStore'
import { ChatMessage } from './ChatMessage'
import { TimingBadge } from './TimingBadge'

export function PendingTurnCard({ pending }: { pending: PendingQuestion }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-end">
        <ChatMessage message={{ role: 'user', content: pending.question }} />
      </div>
      <div className="pl-10">
        <TimingBadge mode="pending" startedAt={pending.startedAt} />
      </div>
    </div>
  )
}
