import { useCallback, useRef } from 'react'

/** Thin wrapper around the browser's built-in Web Speech API
 * (`SpeechRecognition`/`webkitSpeechRecognition`) -- used *only* for live,
 * word-by-word interim captions while the user is talking. This is a
 * deliberate, disclosed exception to this feature's "fully local, no
 * cloud" design: in Chromium-based browsers, `SpeechRecognition` sends
 * microphone audio to the browser vendor's own speech service for
 * processing, same as any other use of this API (e.g. Chrome's own
 * dictation). The *authoritative* final transcript that's actually
 * submitted as the question still comes from local `faster-whisper`
 * (`voice/stt.py`, via `useVoiceConversation`'s `/voice/transcribe` call)
 * -- this hook's output is discarded and overwritten the moment that
 * local result comes back. See `CLAUDE.md`'s "Voice mode" section.
 *
 * `continuous: false` is intentional: the browser stops on its own once it
 * detects the end of an utterance (`onend` fires), which is what gives
 * "auto-stop on silence" turn-taking without a manual per-turn stop
 * button -- the same behavior Google Assistant/ChatGPT voice mode has.
 */
export function useSpeechRecognition() {
  const recognitionRef = useRef<SpeechRecognition | null>(null)

  const isSupported =
    typeof window !== 'undefined' && Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)

  /** Starts listening for one utterance. `onInterimText` is called with the
   * best-effort transcript so far every time the browser reports new
   * results (partial or final) -- purely for the live caption; the caller
   * must not treat this as the final answer. `onEnd` fires once, when the
   * browser itself decides the utterance is over (silence detected) or
   * listening is stopped/aborted for any other reason. */
  const start = useCallback((onInterimText: (text: string) => void, onEnd: () => void) => {
    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SpeechRecognitionCtor) return

    const recognition = new SpeechRecognitionCtor()
    recognition.continuous = false
    recognition.interimResults = true
    recognition.maxAlternatives = 1

    recognition.onresult = (event) => {
      let combined = ''
      for (let i = 0; i < event.results.length; i += 1) {
        combined += event.results[i][0]?.transcript ?? ''
      }
      onInterimText(combined.trim())
    }
    // Fires on natural end-of-speech, an error, or an explicit stop()/abort()
    // -- exactly one `onEnd` per `start()` call either way, so the caller
    // never needs to distinguish why listening stopped.
    recognition.onend = () => onEnd()
    recognition.onerror = () => onEnd()

    recognitionRef.current = recognition
    recognition.start()
  }, [])

  const stop = useCallback(() => {
    recognitionRef.current?.stop()
  }, [])

  const abort = useCallback(() => {
    // Prevents a stray onend/onresult from an utterance the caller no
    // longer cares about (e.g. the user hit "Stop conversation" mid-turn).
    if (recognitionRef.current) {
      recognitionRef.current.onresult = null
      recognitionRef.current.onend = null
      recognitionRef.current.onerror = null
      recognitionRef.current.abort()
      recognitionRef.current = null
    }
  }, [])

  return { isSupported, start, stop, abort }
}
