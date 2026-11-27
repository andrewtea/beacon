import { vi } from 'vitest'

/** The browser's lock, as the tabs of one app share it: one holder at a time, in
 * the order they asked. jsdom has none. */
export function fakeLocks() {
  let last: Promise<unknown> = Promise.resolve()
  const request = vi.fn((_name: string, work: () => unknown) => {
    const turn = last.then(() => work())
    last = turn.catch(() => undefined)
    return turn
  })
  return { request }
}
