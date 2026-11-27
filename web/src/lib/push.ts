import { ApiError, OTHER_MODE } from '../api/http.ts'
import type { Mode } from '../api/incidents.ts'
import {
  fetchPushInfo,
  type PushNotice,
  subscribePush,
  unsubscribePush,
} from '../api/push.ts'
import { placesSnapshot } from './placeStore.ts'

// Notifications: whether this device wants them, and keeping Beacon's server
// told of it.
//
// What is kept where:
// - On this device: that notifications are on (`KEY`), the words a notification
//   shows (a cache the service worker reads, `public/push-sw.js`), and the
//   browser's own subscription.
// - On Beacon's server: the address the browser's push service made for this
//   device, and where the saved places are, each rounded to about a block and
//   without its name. Nothing when no place is saved.
//
// There is one way the server is told anything: `syncPush`, which reads what is
// wanted now and makes the server's copy match it. Turning notifications on or
// off, saving or removing a place, in this tab or another, only changes what is
// wanted; `usePushSync` (in `App`) then syncs. Only one sync runs at a time
// across the app's tabs, under the browser's lock, and each reads the places
// when its turn comes: a place that was removed is never told again by a tab
// that read the list before.

const KEY = 'beacon.push.v1'
// The same two names as in `public/push-sw.js`.
export const NOTICE_CACHE = 'beacon-push-v1'
export const NOTICE_URL = '/__beacon/push-notice'
const LOCK = 'beacon.push'
// How long after a telling that failed the server is told again.
export const RETRY_MS = 30_000
// The most addresses kept to have the server delete.
const FORGET_MOST = 5

type Wanted = {
  on: boolean
  // Addresses that were turned off here and that the server has not yet been
  // told to delete. The browser has let go of them already.
  forget: string[]
}

export type PushStatus = {
  // Whether the person turned notifications on, on this device.
  wanted: boolean
  // What came of the latest telling of the server by this tab.
  told: 'not-yet' | 'told' | 'failed'
  // Why it failed, in a sentence.
  problem: string | null
  // How many places the server was told of.
  places: number
}

type Listener = () => void
// Told when anything a view shows changes: what is wanted, or what came of a
// telling.
const listeners = new Set<Listener>()
// Told only when what is wanted changes, which is what the server has to be
// told of. A telling's own outcome is not one of these: a listener that told
// the server again for it would never stop.
const wantedListeners = new Set<Listener>()
let cache: { raw: string | null; wanted: Wanted } | null = null
let told: Pick<PushStatus, 'told' | 'problem' | 'places'> = {
  told: 'not-yet',
  problem: null,
  places: 0,
}
let status: PushStatus | null = null
// What this tab last told the server, so that it is not told the same again.
let lastTold: string | null = null

const NOTHING_WANTED: Wanted = { on: false, forget: [] }

function readWanted(): Wanted {
  let raw: string | null = null
  try {
    raw = window.localStorage.getItem(KEY)
  } catch {
    // Storage that is switched off keeps nothing, and notifications stay off.
  }
  if (cache !== null && cache.raw === raw) return cache.wanted
  let wanted = NOTHING_WANTED
  try {
    const parsed: unknown = raw === null ? null : JSON.parse(raw)
    if (typeof parsed === 'object' && parsed !== null) {
      const kept = parsed as Record<string, unknown>
      wanted = {
        on: kept.on === true,
        forget: Array.isArray(kept.forget)
          ? kept.forget
              .filter((address) => typeof address === 'string')
              .slice(0, FORGET_MOST)
          : [],
      }
    }
  } catch {
    wanted = NOTHING_WANTED
  }
  cache = { raw, wanted }
  return wanted
}

function changed(what: 'wanted' | 'told'): void {
  status = null
  for (const listener of listeners) listener()
  if (what === 'wanted') for (const listener of wantedListeners) listener()
}

function writeWanted(wanted: Wanted): boolean {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(wanted))
  } catch {
    return false
  }
  cache = null
  changed('wanted')
  return true
}

function setTold(next: typeof told): void {
  if (
    next.told === told.told &&
    next.problem === told.problem &&
    next.places === told.places
  )
    return
  told = next
  changed('told')
}

function onStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== KEY) return
  cache = null
  changed('wanted')
}

function listening(to: Set<Listener>, listener: Listener): () => void {
  if (listeners.size + wantedListeners.size === 0)
    window.addEventListener('storage', onStorage)
  to.add(listener)
  return () => {
    to.delete(listener)
    if (listeners.size + wantedListeners.size === 0)
      window.removeEventListener('storage', onStorage)
  }
}

/** Be told when what is wanted changes, in this tab or another, and when a
 * telling of the server ends: whenever what a view shows can have changed. */
export function onPushChange(listener: Listener): () => void {
  return listening(listeners, listener)
}

/** Be told when what is wanted of notifications changes, in this tab or
 * another, and at no other time. */
export function onPushWanted(listener: Listener): () => void {
  return listening(wantedListeners, listener)
}

/** For `useSyncExternalStore`: the same object while nothing changed. */
export function pushSnapshot(): PushStatus {
  const wanted = readWanted().on
  if (status === null || status.wanted !== wanted) status = { wanted, ...told }
  return status
}

/** Whether this browser can receive a push and show a notification here. On an
 * iPhone it can only once the app is on the Home Screen. */
export function pushSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window &&
    'caches' in window &&
    (navigator as { locks?: LockManager }).locks !== undefined
  )
}

function bytesOf(key: string): Uint8Array<ArrayBuffer> {
  const plain = atob(key.replace(/-/g, '+').replace(/_/g, '/'))
  const bytes = new Uint8Array(new ArrayBuffer(plain.length))
  for (let index = 0; index < plain.length; index += 1)
    bytes[index] = plain.charCodeAt(index)
  return bytes
}

/** The key a subscription was made for, as the server writes one. */
function keyOf(subscription: PushSubscription): string | null {
  const key = subscription.options.applicationServerKey
  if (key === null) return null
  let plain = ''
  for (const byte of new Uint8Array(key)) plain += String.fromCharCode(byte)
  return btoa(plain).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function keepNotice(notice: PushNotice): Promise<void> {
  const kept = await window.caches.open(NOTICE_CACHE)
  await kept.put(
    NOTICE_URL,
    new Response(JSON.stringify({ title: notice.title, text: notice.text }), {
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

/** The words a notification shows on this device, or null when none are kept. */
export async function keptNotice(): Promise<PushNotice | null> {
  try {
    const kept = await (
      await window.caches.open(NOTICE_CACHE)
    ).match(NOTICE_URL)
    if (kept === undefined) return null
    const words = (await kept.json()) as Partial<PushNotice>
    return typeof words.title === 'string' && typeof words.text === 'string'
      ? { title: words.title, text: words.text }
      : null
  } catch {
    return null
  }
}

function alone<T>(work: () => Promise<T>): Promise<T> {
  const locks = (navigator as { locks?: LockManager }).locks
  if (locks === undefined) return Promise.reject(new Error('No lock.'))
  return locks.request(LOCK, work) as Promise<T>
}

/**
 * Turn notifications on for this device: ask the browser's leave, and note
 * that they are wanted. The server is told by the sync that follows. Answers
 * with what came of it: `on`; `blocked` when the browser does not allow
 * notifications from this site; `no-worker` when this copy of the app has no
 * service worker to receive one; `failed` when the browser cannot do this or
 * would not keep the change.
 */
export async function turnOn(): Promise<
  'on' | 'blocked' | 'no-worker' | 'failed'
> {
  if (!pushSupported()) return 'failed'
  let allowed: NotificationPermission
  try {
    allowed = await Notification.requestPermission()
  } catch {
    return 'failed'
  }
  if (allowed !== 'granted') return 'blocked'
  try {
    if ((await navigator.serviceWorker.getRegistration()) === undefined)
      return 'no-worker'
    return await alone(async () =>
      writeWanted({ ...readWanted(), on: true }) ? 'on' : 'failed',
    )
  } catch {
    return 'failed'
  }
}

/**
 * Turn notifications off for this device. The browser lets go of its
 * subscription at once, so nothing can arrive, and the words a notification
 * shows are deleted. The address is noted for the server to be told to delete,
 * which the sync that follows does, and does again until the server has heard.
 * Answers with whether the change was kept.
 */
export async function turnOff(): Promise<boolean> {
  try {
    return await alone(async () => {
      const registration = await navigator.serviceWorker.getRegistration()
      const subscription =
        (await registration?.pushManager.getSubscription()) ?? null
      const wanted = readWanted()
      const forget =
        subscription === null || wanted.forget.includes(subscription.endpoint)
          ? wanted.forget
          : [...wanted.forget, subscription.endpoint].slice(-FORGET_MOST)
      if (!writeWanted({ on: false, forget })) return false
      await subscription?.unsubscribe().catch(() => false)
      await window.caches.delete(NOTICE_CACHE).catch(() => false)
      lastTold = null
      return true
    })
  } catch {
    return false
  }
}

/** The server is in another mode than the app is showing. */
export class OtherModeError extends Error {}

/** Make the server's copy match what is wanted now. Called under the lock. */
async function tell(mode: Mode): Promise<void> {
  // First, the addresses that were turned off: the server deletes each.
  for (const address of readWanted().forget) {
    await unsubscribePush(address)
    const now = readWanted()
    writeWanted({
      ...now,
      forget: now.forget.filter((kept) => kept !== address),
    })
  }
  if (!readWanted().on || Notification.permission !== 'granted') return
  const registration = await navigator.serviceWorker.getRegistration()
  if (registration === undefined) return
  const info = await fetchPushInfo()
  if (info.mode !== mode) throw new OtherModeError()
  if (!info.enabled || info.public_key === null || info.notice === null) return
  let subscription = await registration.pushManager.getSubscription()
  if (subscription !== null && keyOf(subscription) !== info.public_key) {
    // Made for another server's key: only that server can wake it, and the
    // words kept here are that server's. It is let go of, and one is made for
    // this server.
    const old = subscription.endpoint
    await subscription.unsubscribe()
    subscription = null
    lastTold = null
    await unsubscribePush(old).catch(() => undefined)
  }
  // Read now, under the lock: what another tab did a moment ago holds.
  const places = placesSnapshot().map(({ lat, lon }) => ({ lat, lon }))
  if (places.length === 0) {
    // With no place there is nothing to be notified about, and the server
    // keeps nothing of this device.
    if (subscription !== null && lastTold !== `none ${subscription.endpoint}`) {
      await unsubscribePush(subscription.endpoint)
      lastTold = `none ${subscription.endpoint}`
    }
    setTold({ told: 'told', problem: null, places: 0 })
    return
  }
  // The words are kept before the server has the address, so that no push can
  // arrive before its words.
  await keepNotice(info.notice)
  subscription ??= await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: bytesOf(info.public_key),
  })
  const telling = `${mode} ${subscription.endpoint} ${JSON.stringify(places)}`
  if (lastTold !== telling) {
    await subscribePush(subscription.endpoint, places, mode)
    lastTold = telling
  }
  setTold({ told: 'told', problem: null, places: places.length })
}

const NOT_TOLD =
  'Beacon’s server could not be told of the change to your notifications.'

/**
 * Make the server's copy match what is wanted on this device now. Answers with
 * whether it does: false when the server could not be told, which the status
 * then says, and which is worth trying again. Throws `OtherModeError` when the
 * server is in another mode than `mode`: nothing was sent to it.
 */
export async function syncPush(mode: Mode): Promise<boolean> {
  if (!pushSupported()) return true
  const wanted = readWanted()
  if (!wanted.on && wanted.forget.length === 0) return true
  let problem: string | null = null
  try {
    await alone(() => tell(mode))
  } catch (error) {
    if (error instanceof OtherModeError) throw error
    if (error instanceof ApiError && error.status === OTHER_MODE)
      throw new OtherModeError()
    // The server's own sentence when it gave one.
    problem = error instanceof ApiError ? error.message : NOT_TOLD
  }
  if (problem === null) return true
  setTold({ told: 'failed', problem, places: told.places })
  return false
}

/** Forget what this tab knows of the server's copy. For tests. */
export function resetPush(): void {
  cache = null
  status = null
  lastTold = null
  told = { told: 'not-yet', problem: null, places: 0 }
}
