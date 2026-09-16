import type { PendingQuestion } from '@/store/chatStore'
import { ChatMessage } from './ChatMessage'
import { TimingBadge } from './TimingBadge'

/** The in-flight turn, rendered as the next item in the normal scrolling
 * conversation -- same shape as a finished `TurnCard` (question bubble,
 * then indented content below) with `TimingBadge`'s pulsing "Thinking
 * Ns…" text standing in for the answer. Deliberately not an overlay: no
 * backdrop, no dimming/blurring of prior turns, nothing covering the
 * header or composer -- it just sits at the bottom of the list like any
 * other message, and is replaced in place by the real `TurnCard` the
 * moment the answer arrives. */
export function PendingTurn({ pendingQuestion }: { pendingQuestion: PendingQuestion }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-end">
        <ChatMessage message={{ role: 'user', content: pendingQuestion.question }} />
      </div>
      <div className="pl-10">
        <TimingBadge mode="pending" startedAt={pendingQuestion.startedAt} />
      </div>
    </div>
  )
}
