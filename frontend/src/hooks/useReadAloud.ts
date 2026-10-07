import { useEffect, useSyncExternalStore } from 'react'
import { getReadAloudController, type ReadAloudStatus } from '@/lib/readAloud'

/** Read-aloud state for one answer, identified by `key` (its turn's entryId).
 * Another answer's playback never shows as this answer's status, and unmounting
 * this answer's card stops playback if it is the one speaking, so speech never
 * outlives the answer it belongs to. */
export function useReadAloud(key: string) {
  const controller = getReadAloudController()
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot)

  useEffect(() => () => controller.stopIfActive(key), [controller, key])

  const isActive = snapshot.activeKey === key
  const status: ReadAloudStatus = isActive ? snapshot.status : 'idle'

  return {
    isSupported: controller.isSupported,
    status,
    start: (markdown: string, lang: string) => controller.start(key, markdown, lang),
    pause: controller.pause,
    resume: controller.resume,
    stop: () => controller.stopIfActive(key),
  }
}
