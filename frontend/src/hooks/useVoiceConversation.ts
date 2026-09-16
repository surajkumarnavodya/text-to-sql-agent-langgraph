import { useEffect, useRef, useState } from 'react'
import { useAudioRecorder } from '@/hooks/useAudioRecorder'
import { useSpeechRecognition } from '@/hooks/useSpeechRecognition'
import { transcribeAudio } from '@/lib/api'

export type VoiceConversationPhase = 'idle' | 'listening' | 'transcribing' | 'speaking'

// A ~0-sample silent WAV, used only to "warm up" `<audio>` playback for
// the browser's autoplay policy -- see `start()`'s comment below.
const SILENT_AUDIO_SRC =
  'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA='

/** Drives one voice turn *inline in the composer* -- there is no separate
 * takeover card. `listen()` -> (auto-detected silence, or the mic button
 * itself toggled to a stop button) -> transcribe locally -> the raw heard
 * text is handed to `onTranscribed`, whose caller (`ChatInput`) drops it
 * straight into the normal textarea. From there it's exactly like a typed
 * question: the user can read/edit it, and the existing Send button (or
 * Enter) is the only way it's ever submitted -- nothing here auto-submits.
 * Only the raw transcript is ever surfaced, never an AI-cleaned rewrite --
 * what lands in the box is exactly what was heard.
 *
 * Two independent inputs run in parallel while listening:
 *   - `useSpeechRecognition` (browser-native, live interim captions only --
 *     see that hook's own docstring for the local-vs-cloud disclosure).
 *     `ChatInput` renders these interim words directly into the textarea
 *     as they arrive, so the box visibly "types itself" while the user
 *     talks.
 *   - `useAudioRecorder` (plain `MediaRecorder`, captures the same
 *     utterance for the authoritative local `faster-whisper` pass via
 *     `POST /voice/transcribe`).
 * The live caption is *replaced*, never merged, by the local transcript
 * once it comes back -- captions are a UI nicety, the local result is
 * what's actually asked.
 *
 * `continuous: false` on the recognition side means the browser itself
 * signals end-of-utterance (`onend`), which also ends listening -- but the
 * mic-turned-stop button (`stopListening`) works identically in every
 * browser, so listening never depends on `SpeechRecognition` support to be
 * endable.
 */
export function useVoiceConversation(onTranscribed: (text: string) => void) {
  const [phase, setPhase] = useState<VoiceConversationPhase>('idle')
  const [liveCaption, setLiveCaption] = useState('')
  const [error, setError] = useState<string | null>(null)

  const recorder = useAudioRecorder()
  const speech = useSpeechRecognition()

  // Mutable, read-fresh-at-call-time mirror of `phase` that async callbacks
  // (speech recognition's onend, in particular -- registered once per
  // listening turn, potentially long-lived) need without risking a stale
  // closure over a `useState` value captured at registration time.
  const phaseRef = useRef<VoiceConversationPhase>('idle')
  const audioElRef = useRef<HTMLAudioElement | null>(null)

  const updatePhase = (next: VoiceConversationPhase) => {
    phaseRef.current = next
    setPhase(next)
  }

  /** Plays a synthesized spoken-answer URL through the same `<audio>`
   * element `start()` warmed up for autoplay -- called by `ChatInput`
   * after a voice-originated question's answer comes back. Resolves once
   * playback ends (or fails), never rejects. */
  const playAnswer = (url: string): Promise<void> =>
    new Promise<void>((resolve) => {
      if (!audioElRef.current) audioElRef.current = new Audio()
      const audioEl = audioElRef.current
      audioEl.src = url
      const finish = () => {
        updatePhase('idle')
        resolve()
      }
      audioEl.onended = finish
      audioEl.onerror = finish
      updatePhase('speaking')
      audioEl.play().catch(finish)
    })

  /** Aborts whatever's in flight and returns to idle. `clearError` is
   * false when a turn just failed (transcription error, mic denied) -- the
   * message needs to survive so it's still visible after the reset; the
   * next `start()` clears it. */
  const reset = (clearError: boolean) => {
    speech.abort()
    void recorder.stop()
    setLiveCaption('')
    if (clearError) setError(null)
    updatePhase('idle')
  }

  const finishListening = async () => {
    updatePhase('transcribing')
    const audioBlob = await recorder.stop()
    speech.abort()

    if (!audioBlob) {
      reset(false)
      return
    }

    let text = ''
    try {
      const result = await transcribeAudio(audioBlob)
      // Only ever the raw heard transcript -- `result.corrected_text` (an
      // AI-cleaned rewrite) is deliberately never used here, so what lands
      // in the composer is exactly what was heard, nothing else.
      text = result.text.trim()
    } catch {
      setError('voice.transcribeFailed')
    }
    setLiveCaption('')
    updatePhase('idle')
    if (text) onTranscribed(text)
  }

  const start = async () => {
    setError(null)
    setLiveCaption('')
    updatePhase('listening')
    // "Warms up" this <audio> element with a real, gesture-attributed
    // play() call, synchronously inside this click handler. The actual
    // spoken answer plays several seconds later (after transcription +
    // the agent's own round trip via `playAnswer`), well outside this
    // call stack -- some browsers only treat that later, code-triggered
    // play() as allowed if the tab has already produced audio once;
    // without this, the very first spoken answer of a session can be
    // silently blocked by the browser's autoplay policy.
    if (!audioElRef.current) audioElRef.current = new Audio()
    audioElRef.current.src = SILENT_AUDIO_SRC
    void audioElRef.current.play().catch(() => {})

    const recordingStarted = await recorder.start()
    // The mic permission prompt can take a while (the user has to actually
    // click "Allow") -- if the turn was cancelled/superseded during that
    // wait, don't resurrect a turn nobody wants anymore.
    if (phaseRef.current !== 'listening') {
      void recorder.stop()
      return
    }
    if (!recordingStarted.ok) {
      // Always a translation key, not the raw browser-facing string --
      // the caller translates it with `t()`.
      setError('voice.micDenied')
      reset(false)
      return
    }
    if (speech.isSupported) {
      speech.start(
        (text) => setLiveCaption(text),
        () => {
          void finishListening()
        },
      )
    }
    // Unsupported browsers: no auto-stop signal. ChatInput shows a static
    // "recording" placeholder in this case; the mic-turned-stop button is
    // the only way to end listening.
  }

  /** Ends the current listening turn early -- the fallback path for
   * browsers without live speech recognition, and also usable any time the
   * user doesn't want to wait for silence detection. Same button that
   * started listening, just toggled -- no separate stop control. */
  const stopListening = () => {
    if (phaseRef.current !== 'listening') return
    if (speech.isSupported) {
      speech.stop()
    } else {
      void finishListening()
    }
  }

  useEffect(() => () => reset(true), []) // eslint-disable-line react-hooks/exhaustive-deps -- cleanup only, intentionally runs once

  return {
    phase,
    liveCaption,
    /** An i18next key (e.g. `"voice.micDenied"`), not a display string --
     * the caller translates it with `t()`. `null` when there's nothing to show. */
    error,
    isSupported: speech.isSupported,
    start,
    stopListening,
    playAnswer,
  }
}
