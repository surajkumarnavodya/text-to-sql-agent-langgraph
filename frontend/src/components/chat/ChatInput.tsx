import { Loader2, Mic, Send } from 'lucide-react'
import { useRef, useState, type ChangeEvent, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { useHealth } from '@/hooks/queries'
import { useVoiceConversation } from '@/hooks/useVoiceConversation'
import { useSettingsStore } from '@/store/settingsStore'
import { VoiceConversationBar } from './VoiceConversationBar'

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
  isLoading,
}: {
  onSubmit: (question: string) => void
  disabled?: boolean
  isLoading?: boolean
}) {
  const { t } = useTranslation()
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const health = useHealth()
  const voiceModeEnabled = useSettingsStore((state) => state.voiceModeEnabled)
  const showVoiceButton = Boolean(health.data?.voice_enabled) && voiceModeEnabled
  // Owned here (not inside VoiceConversationBar) so the hook -- and the
  // MediaRecorder/SpeechRecognition handles it holds -- survives the
  // active/inactive transition; a hook living inside a component that
  // itself only mounts while active would tear its own state down the
  // moment a turn ends and it flips back to idle.
  const voice = useVoiceConversation()

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

  if (showVoiceButton && voice.isActive) {
    return <VoiceConversationBar voice={voice} />
  }

  return (
    <div className="flex flex-col gap-1 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-3 shadow-sm transition-shadow focus-within:border-[var(--accent)] focus-within:shadow-md">
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={t('chat.placeholder')}
          disabled={disabled}
          rows={1}
          className="min-h-10 max-h-60 flex-1 resize-none overflow-y-auto bg-transparent px-1 py-1.5 text-sm leading-normal focus-visible:outline-none"
        />
        {showVoiceButton && (
          <Button
            variant="secondary"
            size="icon"
            onClick={voice.start}
            disabled={disabled}
            aria-label={t('voice.startConversation')}
            title={t('voice.startConversation')}
            className="rounded-xl"
          >
            <Mic className="h-4 w-4" />
          </Button>
        )}
        <Button
          variant="primary"
          size="icon"
          onClick={submit}
          disabled={disabled || !value.trim()}
          aria-label={t('chat.placeholder')}
          className="rounded-xl"
        >
          {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        </Button>
      </div>
      <div className="flex items-center justify-between">
        {voice.error ? (
          // Surfaced here (not only in VoiceConversationBar) because a
          // failed turn returns to this normal view immediately -- see
          // useVoiceConversation's `reset(clearError)` -- so this is the
          // only place a mic-denied/transcription error is still visible.
          <span className="text-[11px] text-[var(--danger)]">{t(voice.error)}</span>
        ) : (
          <span />
        )}
        <span
          className={`text-[11px] ${
            wordCount >= MAX_WORDS ? 'text-[var(--danger)]' : 'text-[var(--muted-foreground)]'
          }`}
        >
          {wordCount} / {MAX_WORDS} {t('chat.words')}
        </span>
      </div>
    </div>
  )
}
