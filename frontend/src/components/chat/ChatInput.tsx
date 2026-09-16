import { Loader2, Mic, Send, Square, Volume2 } from 'lucide-react'
import { useEffect, useRef, useState, type ChangeEvent, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { useHealth } from '@/hooks/queries'
import { useVoiceConversation } from '@/hooks/useVoiceConversation'
import { useSettingsStore } from '@/store/settingsStore'
import { useChatStore } from '@/store/chatStore'

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

/** The composer: textarea + mic + send, always in the same layout -- voice
 * mode never replaces it with a separate card. The mic button toggles in
 * place to a stop button while listening (`voice.phase === 'listening'`),
 * so "stop" always sits exactly where "mic" was, and there is never a
 * second button for it. A finished voice turn writes its raw heard text
 * straight into the textarea, same as if it had been typed -- the user
 * reviews/edits it there and presses the existing Send button (or Enter)
 * to confirm; nothing is ever auto-submitted. */
export function ChatInput() {
  const { t } = useTranslation()
  const [value, setValue] = useState('')
  // True only while `value` is (still) exactly what voice transcription
  // produced -- the one signal `submit()` uses to tag the question as
  // voice-originated (so `chatStore.askQuestion` synthesizes a spoken
  // answer) and to know whether to play that answer back automatically.
  // Cleared on submit and on any manual edit -- editing a voice transcript
  // still counts as voice-originated (the user is correcting what was
  // heard, not writing a fresh question), so it's only cleared once the
  // box is emptied and typed into from scratch.
  const [fromVoice, setFromVoice] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const health = useHealth()
  const voiceModeEnabled = useSettingsStore((state) => state.voiceModeEnabled)
  const showVoiceButton = Boolean(health.data?.voice_enabled) && voiceModeEnabled

  const pendingQuestion = useChatStore((state) => state.pendingQuestion)
  const askQuestion = useChatStore((state) => state.askQuestion)
  const disabled = pendingQuestion !== null

  const voice = useVoiceConversation((text) => {
    setValue(text)
    setFromVoice(true)
  })

  // Once a finished transcript lands in `value` (voice.phase back to
  // 'idle' after having been 'transcribing'), resize + focus the textarea
  // for editing, same as a user would expect after typing.
  useEffect(() => {
    if (voice.phase === 'idle' && fromVoice && textareaRef.current) {
      resizeToFitContent(textareaRef.current)
      textareaRef.current.focus()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to phase changes, not every value/fromVoice change
  }, [voice.phase])

  const submit = async () => {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    const voiceOriginated = fromVoice
    setValue('')
    setFromVoice(false)
    // Collapse back to a single line once the question is sent -- without
    // this the box would stay at whatever height the last question grew
    // to, since the auto-resize below only ever grows/shrinks in response
    // to a change event, and clearing the value programmatically doesn't
    // fire one.
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    const entry = await askQuestion(trimmed, { originatedFromVoice: voiceOriginated })
    if (voiceOriginated && entry.spokenAudioUrl) {
      await voice.playAnswer(entry.spokenAudioUrl)
    }
  }

  const handleChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const el = event.target
    const words = el.value.trim() === '' ? [] : el.value.trim().split(/\s+/)
    // Hard cap at 250 words -- typing or pasting past it simply stops
    // accepting more, rather than rejecting the whole input.
    const next = words.length > MAX_WORDS ? words.slice(0, MAX_WORDS).join(' ') : el.value
    setValue(next)
    if (fromVoice && next.trim() === '') setFromVoice(false)
    resizeToFitContent(el)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void submit()
    }
    // Shift+Enter falls through to the textarea's default behavior --
    // inserts a newline, growing the box on the next change event.
  }

  const wordCount = countWords(value)
  const isListening = voice.phase === 'listening'
  const isTranscribing = voice.phase === 'transcribing'
  const isSpeaking = voice.phase === 'speaking'
  // While listening, the box shows the live (interim, browser-native)
  // caption instead of `value` -- the composer visibly "types" what it
  // hears in real time. It's read-only during listening/transcribing so
  // the user can't type over words that are about to be replaced by the
  // authoritative local transcript.
  const displayValue = isListening ? voice.liveCaption : value
  const voiceLocked = isListening || isTranscribing || isSpeaking

  return (
    <div className="flex flex-col gap-1 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-3 shadow-sm transition-shadow focus-within:border-[var(--accent)] focus-within:shadow-md">
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={displayValue}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={
            isListening && !voice.isSupported ? t('voice.unsupportedCaption') : t('chat.placeholder')
          }
          disabled={disabled || voiceLocked}
          readOnly={isListening || isTranscribing}
          rows={1}
          className="min-h-10 max-h-60 flex-1 resize-none overflow-y-auto bg-transparent px-1 py-1.5 text-sm leading-normal focus-visible:outline-none"
        />
        {showVoiceButton && (
          <Button
            variant="secondary"
            size="icon"
            onClick={() => {
              if (isListening) voice.stopListening()
              else if (!voiceLocked) void voice.start()
            }}
            disabled={disabled || isTranscribing || isSpeaking}
            aria-label={isListening ? t('voice.stopConversation') : t('voice.startConversation')}
            title={isListening ? t('voice.stopConversation') : t('voice.startConversation')}
            className="rounded-xl"
          >
            {isListening ? (
              <Square className="h-4 w-4" />
            ) : isTranscribing || isSpeaking ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Mic className="h-4 w-4" />
            )}
          </Button>
        )}
        <Button
          variant="primary"
          size="icon"
          onClick={() => void submit()}
          disabled={disabled || voiceLocked || !value.trim()}
          aria-label={t('chat.placeholder')}
          className="rounded-xl"
        >
          {disabled ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        </Button>
      </div>
      <div className="flex items-center justify-between">
        {voice.error ? (
          <span className="text-[11px] text-[var(--danger)]">{t(voice.error)}</span>
        ) : isSpeaking ? (
          <span className="flex items-center gap-1 text-[11px] text-[var(--muted-foreground)]">
            <Volume2 className="h-3 w-3" /> {t('voice.speaking')}
          </span>
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
