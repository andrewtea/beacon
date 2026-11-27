import { useEffect, useLayoutEffect, useRef } from 'react'
import type { Input } from '../api/health.ts'
import type { Mode } from '../api/incidents.ts'
import { subscribePlaces } from './placeStore.ts'
import { onPushWanted, OtherModeError, RETRY_MS, syncPush } from './push.ts'

/**
 * Keeps Beacon's server told of what this device wants of notifications, for as
 * long as the app is open: when the app is opened (which renews what the server
 * keeps), when notifications are turned on or off, and when a place is saved or
 * removed, in this tab or another. A telling that failed is tried again after
 * `RETRY_MS`.
 *
 * Nothing is told while the server's mode is not known, and a server in another
 * mode than is shown is told nothing: `onOtherMode` is called instead.
 *
 * **The server is given an address and places only while it says it takes a
 * visitor's own** (`input` is `own`). While it says anything else or nothing, a
 * sync still asks it to delete what is to be deleted, and gives it nothing. A
 * telling that is under way when `input` changes reads the change before it
 * gives the server anything (`takes`), and a new one is begun.
 */
export function usePushSync(
  mode: Mode | null,
  input: Input | null,
  onOtherMode: () => void,
): void {
  const otherMode = useRef(onOtherMode)
  // Read by a telling at the moment it is about to give the server something.
  // Set as the change is drawn, not a frame after.
  const taken = useRef(input)
  useLayoutEffect(() => {
    taken.current = input
  }, [input])
  useEffect(() => {
    otherMode.current = onOtherMode
  })
  useEffect(() => {
    if (mode === null) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    // One telling at a time from this tab; a change that comes meanwhile is
    // told once the telling under way has ended.
    let telling = false
    let again = false
    const tell = async () => {
      if (stopped) return
      if (telling) {
        again = true
        return
      }
      telling = true
      clearTimeout(timer)
      let done = true
      try {
        done = await syncPush(mode, () => taken.current)
      } catch (error) {
        if (error instanceof OtherModeError) otherMode.current()
        else done = false
      }
      telling = false
      if (stopped) return
      if (again) {
        again = false
        void tell()
      } else if (!done) timer = setTimeout(() => void tell(), RETRY_MS)
    }
    void tell()
    const offPlaces = subscribePlaces(() => void tell())
    const offPush = onPushWanted(() => void tell())
    return () => {
      stopped = true
      clearTimeout(timer)
      offPlaces()
      offPush()
    }
    // Begun anew when what the server takes changes: the telling before reads
    // the change itself, and this one makes the server's copy what it now is.
  }, [mode, input])
}
