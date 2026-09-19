import { useCallback, useState } from 'react'

interface HistoryState<T> {
  past: T[]
  present: T
  future: T[]
}

export interface UseHistoryResult<T> {
  state: T
  /** Pushes a new present value, clearing any redo-able future (the
   * standard undo/redo contract: making a new change after undoing
   * discards whatever was undone, same as every text editor). */
  set: (value: T) => void
  undo: () => void
  redo: () => void
  canUndo: boolean
  canRedo: boolean
  /** Resets history entirely -- past and future both cleared, present set
   * to `value`. Used by ImageEditor's "Reset all edits." */
  reset: (value: T) => void
}

const MAX_HISTORY_ENTRIES = 100

/** Generic linear undo/redo over any value type -- used by ImageEditor for
 * its drawn-elements/mask arrays, kept here as its own hook (rather than
 * inlined) so it's independently unit-testable without mounting a Konva
 * canvas. */
export function useHistory<T>(initial: T): UseHistoryResult<T> {
  const [history, setHistory] = useState<HistoryState<T>>({ past: [], present: initial, future: [] })

  const set = useCallback((value: T) => {
    setHistory((current) => ({
      past: [...current.past, current.present].slice(-MAX_HISTORY_ENTRIES),
      present: value,
      future: [],
    }))
  }, [])

  const undo = useCallback(() => {
    setHistory((current) => {
      if (current.past.length === 0) return current
      const previous = current.past[current.past.length - 1]
      return {
        past: current.past.slice(0, -1),
        present: previous,
        future: [current.present, ...current.future],
      }
    })
  }, [])

  const redo = useCallback(() => {
    setHistory((current) => {
      if (current.future.length === 0) return current
      const [next, ...rest] = current.future
      return { past: [...current.past, current.present], present: next, future: rest }
    })
  }, [])

  const reset = useCallback((value: T) => {
    setHistory({ past: [], present: value, future: [] })
  }, [])

  return {
    state: history.present,
    set,
    undo,
    redo,
    canUndo: history.past.length > 0,
    canRedo: history.future.length > 0,
    reset,
  }
}
