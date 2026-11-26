// The taps this device sent. The server lists taps for nobody, by design: a
// tap's id is what its sender reads the answer with, so the id is kept here, on
// the device, and nowhere else. Nothing here is sent anywhere.
//
// An id is deleted from storage when the server stops answering for it, and not
// only left out of what is shown: `sweepTaps` does it when the app starts, when
// the next id runs out while the app is open, and when the page is looked at
// again. An app that is closed runs nothing, so an id that runs out then stays
// in storage until the app is next opened.
//
// What is shown and what is stored are kept apart. A tap is off the list
// (`tapsSnapshot`) from the moment it runs out, whether or not storage let it
// be deleted.

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
// The list as it is shown: the stored taps that have not run out, and the
// moment the next of them does, until which the list is the same array.
let shown: { of: KeptTap[]; taps: KeptTap[]; until: number } | null = null

function notify(): void {
  for (const listener of listeners) listener()
}

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
  notify()
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

/** When the first of `taps` runs out, or Infinity when there is none. */
function firstEnd(taps: KeptTap[]): number {
  return Math.min(...taps.map((tap) => Date.parse(tap.at) + KEPT_MS))
}

/**
 * Keeps the taps swept for as long as the app is open: now, at the moment each
 * kept tap runs out, whenever the page is shown again, since a phone holds a
 * hidden page's timers back, and when another tab of the app changes what is
 * kept. Answers with how to stop.
 *
 * A tap that runs out is taken off the list that is shown at that moment, by
 * telling whoever shows it, whether or not storage let it be deleted. One that
 * storage would not delete is waited for by no timer: the next timer is for the
 * next tap that has not run out, and the delete is tried again then, when the
 * page is next looked at, and when the list next changes.
 */
export function sweepTaps(): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined
  const plan = () => {
    clearTimeout(timer)
    const now = Date.now()
    // Only the taps that have not run out are waited for.
    const end = firstEnd(current(read(), now))
    // A millisecond past the end, so the tap has run out when the timer fires.
    if (Number.isFinite(end)) timer = setTimeout(sweep, end - now + 1)
  }
  let sweeping = false
  const sweep = () => {
    // Told of its own doing, a sweep does nothing more.
    if (sweeping) return
    sweeping = true
    try {
      // A sweep that deletes something writes, and the write tells the
      // listeners. One that could not delete tells them itself: the list that
      // is shown has changed all the same.
      if (pruneTaps() === 'failed') notify()
    } finally {
      sweeping = false
    }
    plan()
  }
  const seen = () => {
    if (document.visibilityState === 'visible') sweep()
  }
  // A change to the list, from this tab or another, is swept as well: what
  // another tab wrote can hold a tap that has run out.
  const stopListening = subscribeTaps(sweep)
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
  notify()
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

/**
 * The taps to show, for `useSyncExternalStore`: those in storage that the
 * server still answers for. It is the same array until storage changes or one
 * of them runs out, and `sweepTaps` tells the listeners when one does.
 */
export function tapsSnapshot(now: number = Date.now()): KeptTap[] {
  const stored = read()
  if (shown === null || shown.of !== stored || now >= shown.until) {
    const taps = current(stored, now)
    shown = { of: stored, taps, until: firstEnd(taps) }
  }
  return shown.taps
}
