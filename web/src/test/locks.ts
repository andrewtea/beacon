import { vi } from 'vitest'

/** The browser's locks, as the tabs of one app share them: for each name one
 * holder at a time, in the order they asked. jsdom has none. */
export function fakeLocks() {
  const last = new Map<string, Promise<unknown>>()
  const request = vi.fn((name: string, work: () => unknown) => {
    const turn = (last.get(name) ?? Promise.resolve()).then(() => work())
    last.set(
      name,
      turn.catch(() => undefined),
    )
    return turn
  })
  return { request }
}
