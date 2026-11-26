// The taps this device sent. The server lists taps for nobody, by design: a
// tap's id is what its sender reads the answer with, so the id is kept here, on
// the device, and nowhere else. Nothing here is sent anywhere.

export type KeptTap = {
  id: string
  // When the server received it.
  at: string
  photo: boolean
  clip: boolean
}

const KEY = 'beacon.taps.v1'
// The server answers for a tap for a day less an hour. A tap older than that is
// one it no longer has, so it is forgotten here too.
export const KEPT_MS = 23 * 60 * 60 * 1000
const MOST = 20

type Listener = () => void
const listeners = new Set<Listener>()
let cache: { raw: string | null; taps: KeptTap[] } | null = null

function isKept(value: unknown): value is KeptTap {
  if (typeof value !== 'object' || value === null) return false
  const tap = value as Record<string, unknown>
  return (
    typeof tap.id === 'string' &&
    typeof tap.at === 'string' &&
    typeof tap.photo === 'boolean' &&
    typeof tap.clip === 'boolean'
  )
}

function read(): KeptTap[] {
  let raw: string | null = null
  try {
    raw = window.localStorage.getItem(KEY)
  } catch {
    // Storage that is switched off keeps nothing: a tap's answer is then read
    // only from the page it was sent from.
  }
  if (cache !== null && cache.raw === raw) return cache.taps
  let taps: KeptTap[] = []
  try {
    const parsed: unknown = raw === null ? [] : JSON.parse(raw)
    taps = Array.isArray(parsed) ? parsed.filter(isKept) : []
  } catch {
    taps = []
  }
  cache = { raw, taps }
  return taps
}

function write(taps: KeptTap[]): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(taps))
  } catch {
    // See `read`.
  }
  cache = null
  for (const listener of listeners) listener()
}

/** Those of `taps` the server still answers for. */
export function current(taps: KeptTap[], now: number = Date.now()): KeptTap[] {
  return taps.filter((tap) => now - Date.parse(tap.at) < KEPT_MS)
}

/** The taps kept on this device that the server still answers for, newest first. */
export function keptTaps(now: number = Date.now()): KeptTap[] {
  return current(read(), now)
}

export function keepTap(tap: KeptTap, now: number = Date.now()): void {
  const others = keptTaps(now).filter((kept) => kept.id !== tap.id)
  write([tap, ...others].slice(0, MOST))
}

/** Forget a tap: the person asked to, or the server no longer has it. */
export function forgetTap(id: string): void {
  write(read().filter((tap) => tap.id !== id))
}

export function subscribeTaps(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The stored list as it is, for `useSyncExternalStore`: the same array while
 * nothing changed. */
export function tapsSnapshot(): KeptTap[] {
  return read()
}
