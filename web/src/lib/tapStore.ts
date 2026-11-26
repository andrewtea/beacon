// The taps this device sent. The server lists taps for nobody, by design: a
// tap's id is what its sender reads the answer with, so the id is kept here, on
// the device, and nowhere else. Nothing here is sent anywhere.
//
// An id is deleted from storage when the server stops answering for it, and not
// only left out of what is shown: `sweepTaps` does it when the app starts, when
// the next id runs out while the app is open, and when the page is looked at
// again. An app that is closed runs nothing, so an id that runs out then stays
// in storage until the app is next opened.

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

/** Writes the list, and answers with whether storage took it. Storage that is
 * full or switched off keeps what it had, and nobody is told of a change that
 * did not happen. */
function write(taps: KeptTap[]): boolean {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(taps))
  } catch {
    return false
  }
  cache = null
  for (const listener of listeners) listener()
  return true
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

/** Delete from storage the taps the server no longer answers for. Answers with
 * what came of it: some were `deleted`, there was `nothing` to delete, or
 * storage would not take the shorter list and it `failed`. */
export function pruneTaps(
  now: number = Date.now(),
): 'deleted' | 'nothing' | 'failed' {
  const stored = read()
  const kept = current(stored, now)
  if (kept.length === stored.length) return 'nothing'
  return write(kept) ? 'deleted' : 'failed'
}

/** How long until the next kept tap runs out, or null when none is kept. */
function untilNextExpiry(now: number): number | null {
  const ends = read().map((tap) => Date.parse(tap.at) + KEPT_MS - now)
  return ends.length === 0 ? null : Math.max(0, Math.min(...ends))
}

/**
 * Keeps storage swept for as long as the app is open: now, at the moment each
 * kept tap runs out, whenever the page is shown again, since a phone holds a
 * hidden page's timers back, and when another tab of the app changes what is
 * kept. Answers with how to stop.
 */
export function sweepTaps(): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined
  const plan = () => {
    clearTimeout(timer)
    const wait = untilNextExpiry(Date.now())
    // A millisecond past the end, so the tap has run out when the timer fires.
    if (wait !== null) timer = setTimeout(sweep, wait + 1)
  }
  const sweep = () => {
    clearTimeout(timer)
    // A sweep that deletes something writes, and the write plans the next one.
    // One that storage refused plans nothing: the tap that ran out is still
    // there, and a timer would find it again every millisecond. It is tried
    // again when the page is next looked at or the list next changes.
    if (pruneTaps() === 'nothing') plan()
  }
  const seen = () => {
    if (document.visibilityState === 'visible') sweep()
  }
  const stopListening = subscribeTaps(plan)
  document.addEventListener('visibilitychange', seen)
  sweep()
  return () => {
    clearTimeout(timer)
    stopListening()
    document.removeEventListener('visibilitychange', seen)
  }
}

/** Forget a tap: the person asked to, or the server no longer has it. */
export function forgetTap(id: string): void {
  write(read().filter((tap) => tap.id !== id))
}

/** Another tab of the app kept or forgot a tap, or storage was cleared (`key`
 * is then null): the browser tells every other tab, and this one's listeners
 * are told in turn. Nothing is written here, so two tabs do not answer each
 * other for ever. */
function onStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== KEY) return
  cache = null
  for (const listener of listeners) listener()
}

/** Be told when the kept taps change, in this tab or another. */
export function subscribeTaps(listener: Listener): () => void {
  if (listeners.size === 0) window.addEventListener('storage', onStorage)
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) window.removeEventListener('storage', onStorage)
  }
}

/** The stored list as it is, for `useSyncExternalStore`: the same array while
 * nothing changed. */
export function tapsSnapshot(): KeptTap[] {
  return read()
}
