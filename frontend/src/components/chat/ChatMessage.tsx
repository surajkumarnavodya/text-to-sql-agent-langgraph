import { Database, User } from 'lucide-react'
import { ExpandableText } from '@/components/ui/expandable-text'

export interface ChatMessageData {
  role: 'user' | 'assistant'
  content: string
}

export function ChatMessage({ message }: { message: ChatMessageData }) {
  const isUser = message.role === 'user'
  return (
    <div className={`flex gap-2.5 ${isUser ? 'flex-row-reverse' : ''}`}>
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--muted)] text-[var(--muted-foreground)]">
        {isUser ? <User className="h-4 w-4" /> : <Database className="h-4 w-4" />}
      </span>
      <div
        className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-sm ${
          isUser
            ? 'bg-[var(--accent)] text-[var(--accent-foreground)]'
            : 'border border-[var(--border)] bg-[var(--card)]'
        }`}
      >
        {/* Long pasted questions collapse to a few lines with a "Show
            more" toggle (ChatGPT/Perplexity's treatment) instead of
            pushing the rest of the conversation down by default. */}
        <ExpandableText
          text={message.content}
          maxLines={5}
          fadeToColor={isUser ? 'var(--accent)' : 'var(--card)'}
        />
      </div>
    </div>
  )
}
