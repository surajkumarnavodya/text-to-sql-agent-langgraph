import { Loader2, Mic, Square, Volume2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import type { useVoiceConversation } from '@/hooks/useVoiceConversation'
import { cn } from '@/lib/utils'

/** Replaces the whole composer while a hands-free voice conversation is
 * active (`voice.isActive`) -- the "Google Assistant / ChatGPT voice mode"
 * takeover, rather than a small inline widget next to a still-visible
 * textarea. Purely presentational: the actual conversation state machine
 * lives in `useVoiceConversation`, owned by `ChatInput` (which stays
 * mounted across the active/inactive transition so the hook's own state --
 * and its `MediaRecorder`/`SpeechRecognition` handles -- survive it). */
export function VoiceConversationBar({ voice }: { voice: ReturnType<typeof useVoiceConversation> }) {
  const { t } = useTranslation()
  const { phase, liveCaption, error, isSupported, stopListening, stopConversation } = voice

  const statusText: Record<typeof phase, string> = {
    idle: '',
    listening: t('voice.listening'),
    transcribing: t('voice.transcribing'),
    thinking: t('voice.thinking'),
    speaking: t('voice.speaking'),
  }

  return (
    <div className="flex flex-col items-center gap-3 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-6 shadow-sm">
      <div
        className={cn(
          'flex h-14 w-14 items-center justify-center rounded-full',
          phase === 'listening' ? 'animate-pulse bg-[var(--accent-soft)]' : 'bg-[var(--muted)]',
        )}
      >
        {phase === 'transcribing' || phase === 'thinking' ? (
          <Loader2 className="h-6 w-6 animate-spin text-[var(--accent)]" />
        ) : phase === 'speaking' ? (
          <Volume2 className="h-6 w-6 text-[var(--accent)]" />
        ) : (
          <Mic className="h-6 w-6 text-[var(--accent)]" />
        )}
      </div>

      <p className="text-sm font-medium text-[var(--muted-foreground)]">{statusText[phase]}</p>

      {phase === 'listening' && (
        <p className="min-h-5 max-w-md text-center text-sm text-[var(--foreground)]">
          {liveCaption || (isSupported ? '' : t('voice.unsupportedCaption'))}
        </p>
      )}

      {error && <p className="text-sm text-[var(--danger)]">{t(error)}</p>}

      <div className="flex items-center gap-2">
        {phase === 'listening' && !isSupported && (
          <Button variant="secondary" size="sm" onClick={stopListening}>
            {t('voice.doneSpeaking')}
          </Button>
        )}
        <Button variant="danger" size="sm" onClick={stopConversation}>
          <Square className="h-3.5 w-3.5" />
          {t('voice.stopConversation')}
        </Button>
      </div>
    </div>
  )
}
