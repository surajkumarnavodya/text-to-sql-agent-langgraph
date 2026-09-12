export interface ChatMessageData {
  role: 'user' | 'assistant'
  content: string
}

const AVATARS: Record<ChatMessageData['role'], string> = { user: '🧑', assistant: '🗄️' }

export function ChatMessage({ message }: { message: ChatMessageData }) {
  const isUser = message.role === 'user'
  return (
    <div className={`flex gap-2.5 ${isUser ? 'flex-row-reverse' : ''}`}>
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--muted)] text-base">
        {AVATARS[message.role]}
      </span>
      <div
        className={`max-w-[85%] rounded-2xl px-3.5 py-2 text-sm ${
          isUser
            ? 'bg-[var(--accent)] text-[var(--accent-foreground)]'
            : 'border border-[var(--border)] bg-[var(--card)]'
        }`}
      >
        <p className="whitespace-pre-wrap">{message.content}</p>
      </div>
    </div>
  )
}
