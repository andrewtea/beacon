import { vi } from 'vitest'

type Work = (lock: { name: string } | null) => unknown

/** The browser's locks, as the tabs of one app share them: for each name one
 * holder at a time, in the order they asked. One asked for only `ifAvailable`
 * is given none, and is not made to wait, while the lock is held or asked for.
 * jsdom has none. */
export function fakeLocks() {
  const last = new Map<string, Promise<unknown>>()
  const asking = new Map<string, number>()
  const request = vi.fn(
    (name: string, second: Work | { ifAvailable?: boolean }, third?: Work) => {
      const work = typeof second === 'function' ? second : third!
      const options = typeof second === 'function' ? {} : second
      if (options.ifAvailable === true && (asking.get(name) ?? 0) > 0)
        return Promise.resolve().then(() => work(null))
      asking.set(name, (asking.get(name) ?? 0) + 1)
      const turn = (last.get(name) ?? Promise.resolve()).then(() =>
        work({ name }),
      )
      const ended = turn
        .catch(() => undefined)
        .then(() => asking.set(name, (asking.get(name) ?? 1) - 1))
      last.set(name, ended)
      return turn
    },
  )
  return { request }
}
