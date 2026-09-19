import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from './chatStore'

const initialState = useChatStore.getState()

describe('chatStore askQuestion / cancelPendingQuestion', () => {
  let abortSignals: AbortSignal[]

  beforeEach(() => {
    useChatStore.setState(initialState, true)
    abortSignals = []
    // Simulates a genuinely in-flight, cancellable /ask request: never
    // resolves on its own, only rejects with a real DOMException AbortError
    // once its signal is aborted -- the same shape a real aborted `fetch()`
    // call produces, so chatStore's AbortError-detection code path is
    // exercised for real, not assumed.
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init?: RequestInit) => {
        return new Promise((_resolve, reject) => {
          const signal = init?.signal
          if (signal) {
            abortSignals.push(signal)
            signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
          }
        })
      }),
    )
  })

  afterEach(() => {
    useChatStore.setState(initialState, true)
    vi.unstubAllGlobals()
  })

  it('sets pendingQuestion while a request is in flight', () => {
    void useChatStore.getState().askQuestion('How many orders?')
    expect(useChatStore.getState().pendingQuestion?.question).toBe('How many orders?')
  })

  it('cancelPendingQuestion aborts the in-flight request and resolves with a cancelled entry', async () => {
    const promise = useChatStore.getState().askQuestion('How many orders?')
    expect(abortSignals).toHaveLength(1)

    useChatStore.getState().cancelPendingQuestion()
    const entry = await promise

    expect(entry.agentStatus).toBe('failed')
    expect(entry.finalState.failure_explanation).toBe('Cancelled.')
    expect(useChatStore.getState().pendingQuestion).toBeNull()
  })

  it('is a no-op when nothing is pending', () => {
    expect(() => useChatStore.getState().cancelPendingQuestion()).not.toThrow()
  })
})
