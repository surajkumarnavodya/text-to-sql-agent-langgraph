import { useEffect, useState } from 'react'

/** Live-updating elapsed-seconds counter from a `performance.now()`
 * timestamp -- ticks once a second while `active` is true, and stops (last
 * value frozen) once it flips false, so a "Thinking Ns" badge can become a
 * static "Answered in Ns" one without a re-render loop continuing forever. */
export function useElapsedSeconds(startedAt: number, active: boolean): number {
  const [elapsed, setElapsed] = useState(() => (performance.now() - startedAt) / 1000)

  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => {
      setElapsed((performance.now() - startedAt) / 1000)
    }, 1000)
    return () => window.clearInterval(id)
  }, [startedAt, active])

  return elapsed
}
