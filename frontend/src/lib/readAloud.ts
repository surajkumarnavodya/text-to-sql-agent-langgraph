/**
 * "Read aloud" playback controller: an explicit state machine over the
 * browser's one SpeechSynthesis queue.
 *
 *   idle --start--> reading <--pause/resume--> paused
 *   reading|paused --stop--> stopped --start--> reading
 *   reading --last chunk ends--> idle
 *   reading --engine error--> error --start (Retry)--> reading
 *
 * Only one answer speaks at a time: starting another answer cancels the
 * current session first. Every utterance callback is tagged with the session
 * that created it, so a cancelled or superseded session can never change the
 * state of the one that replaced it.
 *
 * The browser engine is a singleton because `window.speechSynthesis` itself is
 * a single, process-wide queue. `createReadAloudController` takes the engine as
 * a parameter so the state machine is testable without a browser.
 */
import { buildSpeechChunks } from './readAloudText'

/** Conversational default; not user-configurable in this minimal version. */
export const READ_ALOUD_RATE = 1

export type ReadAloudStatus = 'idle' | 'reading' | 'paused' | 'stopped' | 'error'

export interface ReadAloudSnapshot {
  status: ReadAloudStatus
  /** The answer currently owning speech (`null` when nothing is active). */
  activeKey: string | null
}

export type StartResult = 'started' | 'empty' | 'unsupported'

export interface SpeakRequest {
  text: string
  lang: string
  voice: SpeechSynthesisVoice | null
  rate: number
  onEnd: () => void
  /** The engine's own error code, e.g. `"canceled"`, `"interrupted"`, `"synthesis-failed"`. */
  onError: (error: string) => void
}

export interface SpeechEngine {
  speak(request: SpeakRequest): void
  cancel(): void
  pause(): void
  resume(): void
  getVoices(): SpeechSynthesisVoice[]
}

export interface ReadAloudController {
  readonly isSupported: boolean
  getSnapshot(): ReadAloudSnapshot
  subscribe(listener: () => void): () => void
  start(key: string, markdown: string, lang: string): StartResult
  pause(): void
  resume(): void
  stop(): void
  /** Stops only if `key` is the answer currently speaking (used on unmount). */
  stopIfActive(key: string): void
}

/** The voice matching `lang` exactly, else the same base language, else `null`
 * (the browser then chooses its own default voice for `lang`). */
export function pickVoice(voices: readonly SpeechSynthesisVoice[], lang: string): SpeechSynthesisVoice | null {
  if (voices.length === 0) return null
  const wanted = lang.toLowerCase()
  const base = wanted.split('-')[0]
  return (
    voices.find((voice) => voice.lang.toLowerCase() === wanted) ??
    (base ? voices.find((voice) => voice.lang.toLowerCase().startsWith(base)) : undefined) ??
    null
  )
}

export function createReadAloudController(engine: SpeechEngine | null): ReadAloudController {
  let snapshot: ReadAloudSnapshot = { status: 'idle', activeKey: null }
  let session = 0
  let chunks: string[] = []
  let nextIndex = 0
  let voice: SpeechSynthesisVoice | null = null
  let lang = 'en'
  const listeners = new Set<() => void>()

  const setSnapshot = (next: ReadAloudSnapshot) => {
    snapshot = next
    listeners.forEach((listener) => listener())
  }

  const finishSession = (status: ReadAloudStatus, activeKey: string | null) => {
    chunks = []
    nextIndex = 0
    setSnapshot({ status, activeKey })
  }

  /** Speaks chunk `nextIndex` for `token`'s session, advancing on each end. */
  const speakNext = (token: number) => {
    if (!engine) return
    if (nextIndex >= chunks.length) {
      finishSession('idle', null)
      return
    }
    try {
      engine.speak({
        text: chunks[nextIndex],
        lang,
        voice,
        rate: READ_ALOUD_RATE,
        onEnd: () => {
          if (token !== session) return
          nextIndex += 1
          speakNext(token)
        },
        onError: (error) => {
          if (token !== session) return
          // "canceled"/"interrupted" mean speech was cut off from outside this
          // controller (another tab, the browser itself) -- a stop, not a fault.
          if (error === 'canceled' || error === 'interrupted') {
            finishSession('stopped', null)
          } else {
            finishSession('error', snapshot.activeKey)
          }
        },
      })
    } catch {
      finishSession('error', snapshot.activeKey)
    }
  }

  const start = (key: string, markdown: string, language: string): StartResult => {
    if (!engine) return 'unsupported'
    // Extract before touching the current session, so an empty answer never
    // interrupts something that is already being read.
    const nextChunks = buildSpeechChunks(markdown)
    if (nextChunks.length === 0) return 'empty'

    if (snapshot.status === 'paused') engine.resume()
    engine.cancel()
    session += 1
    const token = session

    chunks = nextChunks
    nextIndex = 0
    lang = language
    voice = pickVoice(engine.getVoices(), language)
    setSnapshot({ status: 'reading', activeKey: key })
    speakNext(token)
    return 'started'
  }

  const pause = () => {
    if (!engine || snapshot.status !== 'reading') return
    engine.pause()
    setSnapshot({ status: 'paused', activeKey: snapshot.activeKey })
  }

  const resume = () => {
    if (!engine || snapshot.status !== 'paused') return
    engine.resume()
    setSnapshot({ status: 'reading', activeKey: snapshot.activeKey })
  }

  const stop = () => {
    if (!engine || snapshot.activeKey === null) return
    session += 1
    engine.cancel()
    finishSession('stopped', null)
  }

  return {
    isSupported: engine !== null,
    getSnapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    start,
    pause,
    resume,
    stop,
    stopIfActive: (key) => {
      if (snapshot.activeKey === key) stop()
    },
  }
}

/** The real browser engine, or `null` when `speechSynthesis` / `SpeechSynthesisUtterance` is missing. */
function browserSpeechEngine(): SpeechEngine | null {
  if (typeof window === 'undefined' || !('speechSynthesis' in window) || typeof SpeechSynthesisUtterance === 'undefined') {
    return null
  }
  const synth = window.speechSynthesis
  // Browsers have garbage-collected an utterance mid-playback when nothing
  // referenced it, cutting speech off. Holding the current one here prevents that.
  let inFlight: SpeechSynthesisUtterance | null = null
  return {
    speak: ({ text, lang, voice, rate, onEnd, onError }) => {
      const utterance = new SpeechSynthesisUtterance(text)
      utterance.lang = voice?.lang ?? lang
      if (voice) utterance.voice = voice
      utterance.rate = rate
      utterance.onend = () => {
        if (inFlight === utterance) inFlight = null
        onEnd()
      }
      utterance.onerror = (event) => {
        if (inFlight === utterance) inFlight = null
        onError(event.error)
      }
      inFlight = utterance
      synth.speak(utterance)
    },
    cancel: () => synth.cancel(),
    pause: () => synth.pause(),
    resume: () => synth.resume(),
    getVoices: () => synth.getVoices(),
  }
}

let sharedController: ReadAloudController | null = null

/** The one app-wide controller (lazily created). */
export function getReadAloudController(): ReadAloudController {
  sharedController ??= createReadAloudController(browserSpeechEngine())
  return sharedController
}

/** Stops any playback and drops the shared controller so the next use rebuilds it. */
export function resetReadAloudController(): void {
  sharedController?.stop()
  sharedController = null
}
