import { useCallback, useRef, useState } from 'react'

/** Thin wrapper around the browser's native `getUserMedia`/`MediaRecorder`
 * APIs -- no extra npm dependency, matching this app's existing minimal-
 * dependency frontend. Records in whatever container the browser defaults
 * to (webm/opus almost everywhere); the backend's `faster-whisper`
 * dependency (PyAV) decodes it directly, so no client-side transcoding is
 * needed. `error` distinguishes "user denied the mic" from other failures
 * so `useVoiceConversation` can surface a clear message either way. */
export function useAudioRecorder() {
  const [isRecording, setIsRecording] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)

  // Returns whether recording actually started, so a caller that needs to
  // react immediately (`useVoiceConversation`) doesn't have to read back
  // `error` state right after awaiting this -- a `useState` update isn't
  // visible in the same closure until the next render, so that read would
  // see the *previous* render's (stale) value.
  const start = useCallback(async (): Promise<{ ok: boolean; error?: string }> => {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      chunksRef.current = []
      const recorder = new MediaRecorder(stream)
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.start()
      recorderRef.current = recorder
      setIsRecording(true)
      return { ok: true }
    } catch {
      const message = 'Microphone access was denied or is unavailable.'
      setError(message)
      return { ok: false, error: message }
    }
  }, [])

  /** Stops recording and resolves the recorded audio, or `null` if
   * nothing was ever started (e.g. mic permission was denied). */
  const stop = useCallback((): Promise<Blob | null> => {
    return new Promise((resolve) => {
      const recorder = recorderRef.current
      if (!recorder) {
        resolve(null)
        return
      }
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType })
        streamRef.current?.getTracks().forEach((track) => track.stop())
        streamRef.current = null
        recorderRef.current = null
        setIsRecording(false)
        resolve(blob)
      }
      recorder.stop()
    })
  }, [])

  return { isRecording, error, start, stop }
}
