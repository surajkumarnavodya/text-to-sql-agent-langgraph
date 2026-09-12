import { ExpandableText } from '@/components/ui/expandable-text'
import { useChatStore } from '@/store/chatStore'
import { TimingBadge } from './TimingBadge'

/** While a question is in flight, takes over the answer area -- the
 * question and the "Thinking Ns…" indicator sit large and centered in
 * front, with the rest of that pane (prior conversation) dimmed and
 * blurred behind them, like it's paused/loading. The header and sidebar
 * are NOT covered -- this is mounted inside AppShell's `<main>` (a
 * `relative` container sized to just the content pane, not the full
 * viewport), so navigation and the sidebar stay visible and usable the
 * whole time a question is answering. Deliberately not a bordered/
 * shadowed dialog box -- no "popup" chrome, just the content itself on a
 * frosted backdrop -- so it reads as this pane's own foreground state,
 * not an interruption. Disappears the instant the answer arrives, handing
 * off to the turn's own card in the normal scrolling conversation.
 *
 * A long pasted question collapses to a few lines (with "Show more"),
 * same treatment as the chat bubble -- otherwise a very long question
 * would dominate the whole overlay instead of the thinking indicator. */
export function ThinkingOverlay() {
  const pendingQuestion = useChatStore((state) => state.pendingQuestion)
  if (!pendingQuestion) return null

  return (
    <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-6 bg-[var(--background)]/85 px-6 text-center backdrop-blur-md">
      <span className="text-4xl">🗄️</span>
      <ExpandableText
        text={pendingQuestion.question}
        maxLines={4}
        align="center"
        fadeToColor="var(--background)"
        className="w-full max-w-2xl"
        textClassName="text-xl font-semibold text-[var(--foreground)] sm:text-2xl text-center"
      />
      <TimingBadge mode="pending" startedAt={pendingQuestion.startedAt} />
    </div>
  )
}
