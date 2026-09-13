import { useEffect, useRef, useState } from 'react'
import { useAudioRecorder } from '@/hooks/useAudioRecorder'
import { useSpeechRecognition } from '@/hooks/useSpeechRecognition'
import { transcribeAudio } from '@/lib/api'
import { useChatStore } from '@/store/chatStore'

export type VoiceConversationPhase = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

// A ~0-sample silent WAV, used only to "warm up" `<audio>` playback for
// the browser's autoplay policy -- see `start()`'s comment below.
const SILENT_AUDIO_SRC =
  'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA='

/** Orchestrates a single-shot voice turn -- listen -> (auto-detected
 * silence) -> transcribe locally -> ask -> speak the answer -> back to the
 * normal typing composer. One question, one spoken answer, then it stops
 * on its own; the user clicks the mic again to ask another.
 *
 * Two independent inputs run in parallel while listening:
 *   - `useSpeechRecognition` (browser-native, live interim captions only --
 *     see that hook's own docstring for the local-vs-cloud disclosure).
 *   - `useAudioRecorder` (plain `MediaRecorder`, captures the same
 *     utterance for the authoritative local `faster-whisper` pass via
 *     `POST /voice/transcribe`).
 * The live caption is *replaced*, never merged, by the local transcript
 * once it comes back -- captions are a UI nicety, the local result is
 * what's actually asked.
 *
 * `continuous: false` on the recognition side means the browser itself
 * signals end-of-utterance (`onend`), which is what ends listening
 * automatically without a manual stop button. Browsers without
 * `SpeechRecognition` support (`isSupported: false`) get no live caption
 * and no auto-stop signal -- the caller (`VoiceConversationBar`) falls
 * back to a manual "Done speaking" button that calls `stopListening()`
 * itself in that case.
 */
export function useVoiceConversation() {
  const [phase, setPhase] = useState<VoiceConversationPhase>('idle')
  const [isActive, setIsActive] = useState(false)
  const [liveCaption, setLiveCaption] = useState('')
  const [error, setError] = useState<string | null>(null)

  const recorder = useAudioRecorder()
  const speech = useSpeechRecognition()
  const askQuestion = useChatStore((state) => state.askQuestion)

  // Mutable, read-fresh-at-call-time mirrors of state that async callbacks
  // (speech recognition's onend, in particular -- registered once per
  // listening turn, potentially long-lived) need without risking a stale
  // closure over a `useState` value captured at registration time.
  const isActiveRef = useRef(false)
  const phaseRef = useRef<VoiceConversationPhase>('idle')
  const audioElRef = useRef<HTMLAudioElement | null>(null)

  const updatePhase = (next: VoiceConversationPhase) => {
    phaseRef.current = next
    setPhase(next)
  }

  const playAnswer = (url: string): Promise<void> =>
    new Promise((resolve) => {
      if (!audioElRef.current) audioElRef.current = new Audio()
      const audioEl = audioElRef.current
      audioEl.src = url
      audioEl.onended = () => resolve()
      audioEl.onerror = () => resolve()
      audioEl.play().catch(() => resolve())
    })

  /** Ends the current turn and returns to the normal typing composer.
   * `clearError` is false when a turn just failed (transcription error,
   * mic denied) -- the message needs to survive past this reset so
   * `ChatInput`'s normal (non-voice) view can still show it; the next
   * `start()` clears it. */
  const reset = (clearError: boolean) => {
    isActiveRef.current = false
    setIsActive(false)
    speech.abort()
    void recorder.stop()
    audioElRef.current?.pause()
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
      text = result.text.trim()
    } catch {
      setError('voice.transcribeFailed')
    }
    setLiveCaption('')

    if (!text) {
      reset(false)
      return
    }

    updatePhase('thinking')
    const entry = await askQuestion(text, { originatedFromVoice: true })

    if (entry.spokenAudioUrl && isActiveRef.current) {
      updatePhase('speaking')
      await playAnswer(entry.spokenAudioUrl)
    }

    // One question, one spoken answer, then back to the normal composer --
    // a single voice turn, not a continuous hands-free loop.
    reset(false)
  }

  const listen = async () => {
    setLiveCaption('')
    updatePhase('listening')
    const recordingStarted = await recorder.start()
    // The mic permission prompt can take a while (the user has to actually
    // click "Allow") -- if the turn was cancelled during that wait, don't
    // resurrect a turn nobody wants anymore.
    if (!isActiveRef.current) {
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
    // Unsupported browsers: no auto-stop signal. VoiceConversationBar shows
    // a manual "Done speaking" button in this case, wired to stopListening().
  }

  const start = () => {
    isActiveRef.current = true
    setIsActive(true)
    setError(null)
    // "Warms up" this <audio> element with a real, gesture-attributed
    // play() call, synchronously inside this click handler. The actual
    // spoken answer plays several seconds later (after transcription +
    // the agent's own round trip via `playAnswer`), well outside this
    // call stack -- some browsers only treat that first later, code-
    // triggered play() as allowed if the tab has already produced audio
    // once; without this, the very first spoken answer of a session can
    // be silently blocked by the browser's autoplay policy.
    if (!audioElRef.current) audioElRef.current = new Audio()
    audioElRef.current.src = SILENT_AUDIO_SRC
    void audioElRef.current.play().catch(() => {})
    void listen()
  }

  /** Ends the current listening turn early -- the fallback path for
   * browsers without live speech recognition, and also usable any time
   * the user doesn't want to wait for silence detection. */
  const stopListening = () => {
    if (phaseRef.current !== 'listening') return
    if (speech.isSupported) {
      speech.stop()
    } else {
      void finishListening()
    }
  }

  /** Cancels the current turn from any phase -- the user explicitly
   * backing out, so any pending error is dismissed along with it. */
  const stopConversation = () => reset(true)

  useEffect(() => stopConversation, []) // eslint-disable-line react-hooks/exhaustive-deps -- cleanup only, intentionally runs once

  return {
    phase,
    isActive,
    liveCaption,
    /** An i18next key (e.g. `"voice.micDenied"`), not a display string --
     * the caller translates it with `t()`. `null` when there's nothing to show. */
    error,
    isSupported: speech.isSupported,
    start,
    stopListening,
    stopConversation,
  }
}
