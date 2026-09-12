import { Send } from 'lucide-react'
import { useRef, useState, type ChangeEvent, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'

const MAX_WORDS = 250
// Caps how tall the box can grow before it scrolls internally instead --
// otherwise a very long question could push the send button (and
// eventually the whole input bar) off-screen.
const MAX_HEIGHT_PX = 240

function countWords(text: string): number {
  const trimmed = text.trim()
  return trimmed === '' ? 0 : trimmed.split(/\s+/).length
}

function resizeToFitContent(el: HTMLTextAreaElement): void {
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`
}

export function ChatInput({
  onSubmit,
  disabled,
}: {
  onSubmit: (question: string) => void
  disabled?: boolean
}) {
  const { t } = useTranslation()
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const submit = () => {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSubmit(trimmed)
    setValue('')
    // Collapse back to a single line once the question is sent -- without
    // this the box would stay at whatever height the last question grew
    // to, since the auto-resize below only ever grows/shrinks in response
    // to a change event, and clearing the value programmatically doesn't
    // fire one.
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  const handleChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const el = event.target
    const words = el.value.trim() === '' ? [] : el.value.trim().split(/\s+/)
    // Hard cap at 250 words -- typing or pasting past it simply stops
    // accepting more, rather than rejecting the whole input.
    const next = words.length > MAX_WORDS ? words.slice(0, MAX_WORDS).join(' ') : el.value
    setValue(next)
    resizeToFitContent(el)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
    // Shift+Enter falls through to the textarea's default behavior --
    // inserts a newline, growing the box on the next change event.
  }

  const wordCount = countWords(value)

  return (
    <div className="flex flex-col gap-1 border-t border-[var(--border)] bg-[var(--card)] p-3">
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={t('chat.placeholder')}
          disabled={disabled}
          rows={1}
          className="min-h-10 max-h-60 flex-1 resize-none overflow-y-auto rounded-md border border-[var(--border)] bg-[var(--input)] px-3 py-2.5 text-sm leading-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        />
        <Button variant="primary" size="icon" onClick={submit} disabled={disabled || !value.trim()}>
          <Send className="h-4 w-4" />
        </Button>
      </div>
      <span
        className={`self-end text-[11px] ${
          wordCount >= MAX_WORDS ? 'text-[var(--danger)]' : 'text-[var(--muted-foreground)]'
        }`}
      >
        {wordCount} / {MAX_WORDS} {t('chat.words')}
      </span>
    </div>
  )
}
