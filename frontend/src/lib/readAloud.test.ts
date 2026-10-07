import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createReadAloudController,
  getReadAloudController,
  pickVoice,
  READ_ALOUD_RATE,
  resetReadAloudController,
  type SpeakRequest,
  type SpeechEngine,
} from './readAloud'

/** A deterministic stand-in for window.speechSynthesis: records every request so a test
 * can finish or fail a specific utterance on demand. */
function fakeEngine(voices: SpeechSynthesisVoice[] = []) {
  const requests: SpeakRequest[] = []
  const engine: SpeechEngine = {
    speak: vi.fn((request: SpeakRequest) => {
      requests.push(request)
    }),
    cancel: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    getVoices: vi.fn(() => voices),
  }
  return { engine, requests, latest: () => requests[requests.length - 1] }
}

const ONE_CHUNK = 'Revenue grew.'
const TWO_CHUNKS = Array.from({ length: 30 }, (_, i) => `Sentence ${i + 1} is about revenue.`).join(' ')

describe('createReadAloudController -- state machine', () => {
  it('starts reading the first chunk and reports the answer as active', () => {
    const { engine, requests } = fakeEngine()
    const controller = createReadAloudController(engine)

    expect(controller.start('a', ONE_CHUNK, 'en')).toBe('started')
    expect(requests).toHaveLength(1)
    expect(requests[0].text).toBe('Revenue grew.')
    expect(requests[0].rate).toBe(READ_ALOUD_RATE)
    expect(controller.getSnapshot()).toEqual({ status: 'reading', activeKey: 'a' })
  })

  it('moves to idle once the last chunk ends', () => {
    const { engine, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')

    latest().onEnd()

    expect(controller.getSnapshot()).toEqual({ status: 'idle', activeKey: null })
  })

  it('speaks every chunk in order, advancing on each end', () => {
    const { engine, requests, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', TWO_CHUNKS, 'en')
    while (controller.getSnapshot().status === 'reading') latest().onEnd()

    expect(requests.length).toBeGreaterThan(1)
    expect(requests.map((r) => r.text).join(' ')).toBe(TWO_CHUNKS)
    expect(controller.getSnapshot().status).toBe('idle')
  })

  it('returns "empty" and leaves the current session untouched when there is nothing to read', () => {
    const { engine } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')
    vi.mocked(engine.cancel).mockClear()

    expect(controller.start('b', '   ', 'en')).toBe('empty')
    expect(controller.getSnapshot()).toEqual({ status: 'reading', activeKey: 'a' })
    expect(engine.cancel).not.toHaveBeenCalled()
  })

  it('reports "unsupported" without throwing when there is no engine', () => {
    const controller = createReadAloudController(null)

    expect(controller.isSupported).toBe(false)
    expect(controller.start('a', ONE_CHUNK, 'en')).toBe('unsupported')
    expect(controller.getSnapshot()).toEqual({ status: 'idle', activeKey: null })
  })
})

describe('createReadAloudController -- pause, resume, stop', () => {
  it('pauses and resumes the engine without restarting the answer', () => {
    const { engine, requests } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', TWO_CHUNKS, 'en')

    controller.pause()
    expect(engine.pause).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().status).toBe('paused')

    controller.resume()
    expect(engine.resume).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot()).toEqual({ status: 'reading', activeKey: 'a' })
    expect(requests).toHaveLength(1)
  })

  it('ignores pause and resume when there is nothing to pause or resume', () => {
    const { engine } = fakeEngine()
    const controller = createReadAloudController(engine)

    controller.pause()
    controller.resume()

    expect(engine.pause).not.toHaveBeenCalled()
    expect(engine.resume).not.toHaveBeenCalled()
    expect(controller.getSnapshot().status).toBe('idle')
  })

  it('stops, cancels the queue, and ignores late callbacks from the stopped session', () => {
    const { engine, requests, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', TWO_CHUNKS, 'en')
    const stale = latest()

    controller.stop()
    stale.onEnd()

    expect(engine.cancel).toHaveBeenCalled()
    expect(controller.getSnapshot()).toEqual({ status: 'stopped', activeKey: null })
    expect(requests).toHaveLength(1)
  })

  it('can be started again after a stop', () => {
    const { engine } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')
    controller.stop()

    expect(controller.start('a', ONE_CHUNK, 'en')).toBe('started')
    expect(controller.getSnapshot()).toEqual({ status: 'reading', activeKey: 'a' })
  })
})

describe('createReadAloudController -- one answer at a time', () => {
  it('starting answer B stops answer A, and A\'s late callbacks cannot advance B', () => {
    const { engine, requests, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', TWO_CHUNKS, 'en')
    const staleA = latest()

    controller.start('b', 'Second answer.', 'en')
    staleA.onEnd()

    expect(engine.cancel).toHaveBeenCalled()
    expect(controller.getSnapshot()).toEqual({ status: 'reading', activeKey: 'b' })
    expect(requests.at(-1)?.text).toBe('Second answer.')
    expect(requests.filter((r) => r.text === 'Second answer.')).toHaveLength(1)
  })

  it('stopIfActive only stops the answer that is currently speaking', () => {
    const { engine } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')

    controller.stopIfActive('some-other-answer')
    expect(controller.getSnapshot().status).toBe('reading')

    controller.stopIfActive('a')
    expect(controller.getSnapshot()).toEqual({ status: 'stopped', activeKey: null })
  })
})

describe('createReadAloudController -- errors and interruptions', () => {
  it('enters the error state on a synthesis failure, keeping the answer identified for Retry', () => {
    const { engine, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')

    latest().onError('synthesis-failed')

    expect(controller.getSnapshot()).toEqual({ status: 'error', activeKey: 'a' })
  })

  it('recovers through Retry (a fresh start) after an error', () => {
    const { engine, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')
    latest().onError('synthesis-failed')

    expect(controller.start('a', ONE_CHUNK, 'en')).toBe('started')
    expect(controller.getSnapshot()).toEqual({ status: 'reading', activeKey: 'a' })
  })

  it('treats a browser-side cancel or interruption as a stop, not an error', () => {
    const { engine, latest } = fakeEngine()
    const controller = createReadAloudController(engine)
    controller.start('a', ONE_CHUNK, 'en')

    latest().onError('interrupted')

    expect(controller.getSnapshot()).toEqual({ status: 'stopped', activeKey: null })
  })

  it('turns a synchronous engine exception into the error state', () => {
    const { engine } = fakeEngine()
    vi.mocked(engine.speak).mockImplementationOnce(() => {
      throw new Error('engine blew up')
    })
    const controller = createReadAloudController(engine)

    expect(controller.start('a', ONE_CHUNK, 'en')).toBe('started')
    expect(controller.getSnapshot()).toEqual({ status: 'error', activeKey: 'a' })
  })
})

describe('createReadAloudController -- subscriptions and voices', () => {
  it('notifies subscribers on every change and stops after unsubscribe', () => {
    const { engine } = fakeEngine()
    const controller = createReadAloudController(engine)
    const listener = vi.fn()
    const unsubscribe = controller.subscribe(listener)

    controller.start('a', ONE_CHUNK, 'en')
    expect(listener).toHaveBeenCalled()

    unsubscribe()
    listener.mockClear()
    controller.stop()
    expect(listener).not.toHaveBeenCalled()
  })

  it('passes the voice matching the language, re-reading voices on each start', () => {
    const french = { lang: 'fr-FR', name: 'French' } as SpeechSynthesisVoice
    const voices: SpeechSynthesisVoice[] = []
    const { engine, requests } = fakeEngine(voices)
    const controller = createReadAloudController(engine)

    controller.start('a', ONE_CHUNK, 'fr-FR')
    expect(requests[0].voice).toBeNull()

    voices.push(french)
    controller.start('b', ONE_CHUNK, 'fr-FR')
    expect(requests.at(-1)?.voice).toBe(french)
  })
})

describe('pickVoice', () => {
  const en = { lang: 'en-US', name: 'en' } as SpeechSynthesisVoice
  const fr = { lang: 'fr-FR', name: 'fr' } as SpeechSynthesisVoice

  it('prefers an exact locale match', () => {
    expect(pickVoice([en, fr], 'fr-FR')).toBe(fr)
  })

  it('falls back to the same base language', () => {
    expect(pickVoice([en, fr], 'fr-CA')).toBe(fr)
  })

  it('returns null so the browser chooses its own default', () => {
    expect(pickVoice([en], 'hi')).toBeNull()
    expect(pickVoice([], 'en')).toBeNull()
  })
})

describe('browser engine detection', () => {
  afterEach(() => {
    resetReadAloudController()
    vi.unstubAllGlobals()
  })

  it('is unsupported when speechSynthesis is missing', () => {
    // jsdom does not implement speechSynthesis, so the real singleton is unsupported here.
    expect(getReadAloudController().isSupported).toBe(false)
    expect(getReadAloudController().start('a', 'Hello.', 'en')).toBe('unsupported')
  })

  it('uses window.speechSynthesis when the browser provides it', () => {
    const speak = vi.fn()
    vi.stubGlobal('speechSynthesis', { speak, cancel: vi.fn(), pause: vi.fn(), resume: vi.fn(), getVoices: () => [] })
    vi.stubGlobal(
      'SpeechSynthesisUtterance',
      class {
        text: string
        constructor(text: string) {
          this.text = text
        }
      },
    )
    resetReadAloudController()

    expect(getReadAloudController().isSupported).toBe(true)
    expect(getReadAloudController().start('a', 'Hello there.', 'en')).toBe('started')
    expect(speak).toHaveBeenCalledTimes(1)
    expect(speak.mock.calls[0][0].text).toBe('Hello there.')
  })
})
