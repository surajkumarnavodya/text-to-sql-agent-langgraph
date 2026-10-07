import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '@/components/ui/toast'
import { resetReadAloudController } from '@/lib/readAloud'
import { ReadAloudButton } from './ReadAloudButton'

interface FakeUtterance {
  text: string
  lang: string
  voice: SpeechSynthesisVoice | null
  rate: number
  onend: (() => void) | null
  onerror: ((event: { error: string }) => void) | null
}

/** Installs a fake window.speechSynthesis + SpeechSynthesisUtterance so the real
 * browser-engine path (lib/readAloud.ts) runs end to end. jsdom has neither. */
function installFakeSpeechSynthesis() {
  const utterances: FakeUtterance[] = []
  const synth = {
    speak: vi.fn((utterance: FakeUtterance) => {
      utterances.push(utterance)
    }),
    cancel: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    getVoices: vi.fn((): SpeechSynthesisVoice[] => []),
  }
  class FakeSpeechSynthesisUtterance {
    text: string
    lang = ''
    voice: SpeechSynthesisVoice | null = null
    rate = 1
    onend: (() => void) | null = null
    onerror: ((event: { error: string }) => void) | null = null
    constructor(text: string) {
      this.text = text
    }
  }
  vi.stubGlobal('speechSynthesis', synth)
  vi.stubGlobal('SpeechSynthesisUtterance', FakeSpeechSynthesisUtterance)
  return { synth, utterances }
}

function renderButtons(...cards: Array<{ entryId: string; answer: string }>) {
  return render(
    <ToastProvider>
      {cards.map((card) => (
        <ReadAloudButton key={card.entryId} entryId={card.entryId} answer={card.answer} />
      ))}
    </ToastProvider>,
  )
}

/** The polite live region that belongs to ReadAloudButton (the toast container has its own). */
function liveRegion(container: HTMLElement): HTMLElement {
  const region = container.querySelector<HTMLElement>('[role="status"][aria-live="polite"].sr-only')
  if (!region) throw new Error('ReadAloudButton live region not rendered')
  return region
}

const ANSWER = '## Revenue\n\n- India: 40%\n- Germany: 20%'

describe('ReadAloudButton', () => {
  let fake: ReturnType<typeof installFakeSpeechSynthesis>

  beforeEach(() => {
    fake = installFakeSpeechSynthesis()
    resetReadAloudController()
  })

  afterEach(() => {
    resetReadAloudController()
    vi.unstubAllGlobals()
  })

  it('shows the idle Read aloud action when the browser supports speech', () => {
    renderButtons({ entryId: 'turn-1', answer: ANSWER })
    expect(screen.getByRole('button', { name: 'Read aloud' })).toBeEnabled()
    expect(fake.synth.speak).not.toHaveBeenCalled()
  })

  it('starts speech on click and shows Pause and Stop while reading', () => {
    renderButtons({ entryId: 'turn-1', answer: ANSWER })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    expect(fake.synth.speak).toHaveBeenCalledTimes(1)
    expect(fake.utterances[0].text).toContain('Revenue.')
    expect(fake.utterances[0].text).toContain('India: 40 percent.')
    expect(screen.getByRole('button', { name: 'Pause reading' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop reading' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Read aloud' })).not.toBeInTheDocument()
  })

  it('pauses and resumes without restarting the answer', () => {
    renderButtons({ entryId: 'turn-1', answer: ANSWER })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    fireEvent.click(screen.getByRole('button', { name: 'Pause reading' }))
    expect(fake.synth.pause).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Resume reading' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Resume reading' }))
    expect(fake.synth.resume).toHaveBeenCalledTimes(1)
    expect(fake.synth.speak).toHaveBeenCalledTimes(1)
  })

  it('stops speech and returns to the idle action', () => {
    renderButtons({ entryId: 'turn-1', answer: ANSWER })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    fireEvent.click(screen.getByRole('button', { name: 'Stop reading' }))

    expect(fake.synth.cancel).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Read aloud' })).toBeInTheDocument()
  })

  it('reading another answer stops the first one and only one answer shows controls', () => {
    renderButtons(
      { entryId: 'turn-1', answer: ANSWER },
      { entryId: 'turn-2', answer: 'A second answer.' },
    )
    const [firstRead] = screen.getAllByRole('button', { name: 'Read aloud' })
    fireEvent.click(firstRead)
    expect(screen.getAllByRole('button', { name: 'Pause reading' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    expect(fake.synth.cancel).toHaveBeenCalled()
    expect(screen.getAllByRole('button', { name: 'Pause reading' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Read aloud' })).toHaveLength(1)
    expect(fake.utterances.at(-1)?.text).toBe('A second answer.')
  })

  it('stops speech when its card unmounts', () => {
    const { unmount } = renderButtons({ entryId: 'turn-1', answer: ANSWER })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    unmount()

    expect(fake.synth.cancel).toHaveBeenCalled()
  })

  it('moves on to the next chunk when an utterance ends', () => {
    const longAnswer = Array.from({ length: 30 }, (_, i) => `Sentence ${i + 1} is about revenue.`).join('\n\n')
    renderButtons({ entryId: 'turn-1', answer: longAnswer })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    act(() => fake.utterances[0].onend?.())

    expect(fake.utterances.length).toBeGreaterThan(1)
    expect(fake.utterances[1].text.startsWith('Sentence')).toBe(true)
  })

  it('offers Retry and a notice when speech fails', () => {
    renderButtons({ entryId: 'turn-1', answer: ANSWER })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    act(() => fake.utterances[0].onerror?.({ error: 'synthesis-failed' }))

    expect(screen.getByRole('button', { name: 'Retry reading' })).toBeInTheDocument()
    expect(screen.getByText('Could not read this answer aloud.')).toBeInTheDocument()
  })

  it('recovers when Retry is clicked after a failure', () => {
    renderButtons({ entryId: 'turn-1', answer: ANSWER })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))
    act(() => fake.utterances[0].onerror?.({ error: 'synthesis-failed' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry reading' }))

    expect(fake.synth.speak).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('button', { name: 'Pause reading' })).toBeInTheDocument()
  })

  it('reports an empty answer with a notice instead of starting speech', () => {
    renderButtons({ entryId: 'turn-1', answer: '   ' })
    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))

    expect(fake.synth.speak).not.toHaveBeenCalled()
    expect(screen.getByText('There is no answer text to read.')).toBeInTheDocument()
  })

  it('announces state changes through a polite live region', () => {
    const { container } = renderButtons({ entryId: 'turn-1', answer: ANSWER })
    expect(liveRegion(container).textContent).toBe('')

    fireEvent.click(screen.getByRole('button', { name: 'Read aloud' }))
    expect(liveRegion(container).textContent).toBe('Reading…')

    fireEvent.click(screen.getByRole('button', { name: 'Pause reading' }))
    expect(liveRegion(container).textContent).toBe('Paused')
  })
})

describe('ReadAloudButton -- browser without speech support', () => {
  it('stays visible, is marked unavailable, and explains itself on click', () => {
    // jsdom has no speechSynthesis, so the shared controller is unsupported here.
    resetReadAloudController()
    renderButtons({ entryId: 'turn-1', answer: ANSWER })

    const button = screen.getByRole('button', { name: 'Read aloud' })
    expect(button).toHaveAttribute('aria-disabled', 'true')

    fireEvent.click(button)

    expect(screen.getByText('Read aloud is not supported in this browser.')).toBeInTheDocument()
    resetReadAloudController()
  })
})
