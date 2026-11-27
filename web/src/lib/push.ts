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
// - On this device (`KEY`): the mode notifications were turned on for; the
//   address the server of that mode was last given (`told`); and the addresses
//   a server has yet to answer that it deleted, each with the mode of the server
//   that has it (`forget`). Beside that, the words a notification shows (a cache
//   the service worker reads, `public/push-sw.js`) and the browser's own
//   subscription.
// - On Beacon's server: what its own words say (`about_text`): the address,
//   where the saved places are, and what it sent. Nothing when no place is saved.
//
// **Each mode is apart.** Notifications are turned on for the mode the app is
// showing, and that choice is not carried to the other: when the app finds the
// server in another mode, it lets go of the subscription and the person chooses
// again. Each mode has a service worker registration of its own, with its own
// subscription and its own words, so a push from the invented scenario's server
// can only ever show the invented scenario's words.
//
// **An address is deleted when its server says so, and not before.** `told` is
// written before the server is given an address, and an address leaves `forget`
// only when the server of its mode answered `204`. A server that is busy, in
// another mode, or not reached has deleted nothing, and is asked again.
//
// There is one way the server is told anything: `syncPush`, which reads what is
// wanted now and makes the server's copy match it. Turning notifications on or
// off, saving or removing a place, in this tab or another, only changes what is
// wanted; `usePushSync` (in `App`) then syncs. Only one sync runs at a time
// across the app's tabs, under the browser's lock. A sync tells the server every
// time, and keeps no memory of an earlier telling: another tab may have told it
// something else since.

const KEY = 'beacon.push.v2'
// The same names as in `public/push-sw.js`.
export const NOTICE_CACHE = 'beacon-push-v2'
export const NOTICE_URL = '/__beacon/push-notice/'
export const WORKER = '/push-sw.js'
const LOCK = 'beacon.push'
// How long after a telling that failed the server is told again.
export const RETRY_MS = 30_000
// The most addresses kept to have a server delete. One more pushes the oldest
// out, which the server then deletes when its time is over, as its words say.
const FORGET_MOST = 10
const MODES: Mode[] = ['mock', 'live']

/** The scope of the service worker registration that receives a mode's pushes. */
export function scopeOf(mode: Mode): string {
  return `/push/${mode}/`
}

type Address = { endpoint: string; mode: Mode }

type Wanted = {
  // The mode notifications were turned on for, or null when they are off.
  on: Mode | null
  // The address a server was given, or is about to be, with that server's mode.
  told: Address | null
  // Addresses a server has yet to answer that it deleted.
  forget: Address[]
}

export type PushStatus = {
  // The mode the person turned notifications on for, on this device.
  on: Mode | null
  // What came of the latest telling of the server by this tab. `server-off`
  // when the server said it sends no notifications.
  told: 'not-yet' | 'told' | 'failed' | 'server-off'
  // Why it failed, in a sentence.
  problem: string | null
  // How many places the server was told of.
  places: number
  // The modes of the servers that have yet to answer that they deleted an
  // address of this device, one entry for each address.
  forget: Mode[]
  // Whether this browser would not let go of a subscription that is no longer
  // wanted. Until it does, or its server deletes the address, a push can arrive.
  held: boolean
  // Whether a server answered this tab that it deleted an address.
  deleted: boolean
}

type Listener = () => void
// Told when anything a view shows changes.
const listeners = new Set<Listener>()
// Told only when the mode notifications are on for changes, which is when the
// server has to be told. What a telling itself notes down (`told`, `forget`) is
// not one of these: a listener that told the server again for it would never
// stop.
const wantedListeners = new Set<Listener>()
let cache: { raw: string | null; wanted: Wanted } | null = null
// The `on` that was last read, to tell a change of it from any other change.
let seenOn: Mode | null = null
const NOT_YET = { told: 'not-yet', problem: null, places: 0 } as const
let told: Pick<PushStatus, 'told' | 'problem' | 'places'> = NOT_YET
let held = false
let deleted = false
let status: { raw: string | null; shown: PushStatus } | null = null

const NOTHING_WANTED: Wanted = { on: null, told: null, forget: [] }

function isMode(value: unknown): value is Mode {
  return value === 'mock' || value === 'live'
}

function addressOf(value: unknown): Address | null {
  if (typeof value !== 'object' || value === null) return null
  const { endpoint, mode } = value as Record<string, unknown>
  return typeof endpoint === 'string' && isMode(mode)
    ? { endpoint, mode }
    : null
}

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
        on: isMode(kept.on) ? kept.on : null,
        told: addressOf(kept.told),
        forget: Array.isArray(kept.forget)
          ? kept.forget
              .map(addressOf)
              .filter((address) => address !== null)
              .slice(-FORGET_MOST)
          : [],
      }
    }
  } catch {
    wanted = NOTHING_WANTED
  }
  cache = { raw, wanted }
  seenOn = wanted.on
  return wanted
}

function changed(what: 'wanted' | 'shown'): void {
  status = null
  for (const listener of listeners) listener()
  if (what === 'wanted') for (const listener of wantedListeners) listener()
}

/** Keep `next` in place of what is kept. Answers with whether it is kept. */
function writeWanted(next: Wanted): boolean {
  const before = readWanted()
  const raw = JSON.stringify(next)
  if (raw === JSON.stringify(before)) return true
  try {
    window.localStorage.setItem(KEY, raw)
  } catch {
    return false
  }
  cache = null
  changed(before.on !== next.on ? 'wanted' : 'shown')
  return true
}

/** Change what is kept, or throw: a telling that could not note down what it
 * is about to do does not do it. */
function keep(change: (now: Wanted) => Wanted): void {
  if (!writeWanted(change(readWanted())))
    throw new Error('The browser would not keep the change.')
}

const same = (one: Address, other: Address) =>
  one.endpoint === other.endpoint && one.mode === other.mode

/** With `address` among those a server is to delete. */
const queued =
  (address: Address) =>
  (now: Wanted): Wanted => ({
    ...now,
    forget: [
      ...now.forget.filter((kept) => !same(kept, address)),
      address,
    ].slice(-FORGET_MOST),
  })

/** With the address a server was given among those it is to delete. */
function untold(now: Wanted): Wanted {
  return now.told === null ? now : { ...queued(now.told)(now), told: null }
}

/** With what a server was told for another mode than is on, to be deleted. */
function retired(now: Wanted): Wanted {
  return now.told !== null && now.told.mode !== now.on ? untold(now) : now
}

function show(change: () => boolean): void {
  if (change()) changed('shown')
}

function setTold(next: typeof told): void {
  show(() => {
    const differs =
      next.told !== told.told ||
      next.problem !== told.problem ||
      next.places !== told.places
    told = next
    return differs
  })
}

function onStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== KEY) return
  const before = seenOn
  cache = null
  changed(readWanted().on !== before ? 'wanted' : 'shown')
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

/** Be told whenever what a view shows of notifications can have changed. */
export function onPushChange(listener: Listener): () => void {
  return listening(listeners, listener)
}

/** Be told when notifications are turned on or off, in this tab or another,
 * and at no other time. */
export function onPushWanted(listener: Listener): () => void {
  return listening(wantedListeners, listener)
}

/** For `useSyncExternalStore`: the same object while nothing changed. */
export function pushSnapshot(): PushStatus {
  const wanted = readWanted()
  const raw = cache?.raw ?? null
  if (status === null || status.raw !== raw)
    status = {
      raw,
      shown: {
        on: wanted.on,
        ...told,
        forget: wanted.forget.map((address) => address.mode),
        held,
        deleted,
      },
    }
  return status.shown
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

/** The registration that receives a mode's pushes, or null when there is none.
 * The app's other service worker, at the site's root, is not it. */
async function registrationOf(
  mode: Mode,
): Promise<ServiceWorkerRegistration | null> {
  const found = await navigator.serviceWorker.getRegistration(scopeOf(mode))
  return found !== undefined && new URL(found.scope).pathname === scopeOf(mode)
    ? found
    : null
}

/** The registration that receives a mode's pushes, made now when there is
 * none, once its worker is running. Throws when this copy of the app cannot
 * have one. */
async function workerFor(mode: Mode): Promise<ServiceWorkerRegistration> {
  // One script at two scopes: which mode a worker is for is its scope's to say.
  const registration = await navigator.serviceWorker.register(WORKER, {
    scope: scopeOf(mode),
  })
  if (registration.active !== null) return registration
  const coming = registration.installing ?? registration.waiting
  if (coming === null) throw new Error('No service worker.')
  await new Promise<void>((resolve, reject) => {
    const look = () => {
      if (coming.state === 'activated') resolve()
      else if (coming.state === 'redundant')
        reject(new Error('No service worker.'))
    }
    coming.addEventListener('statechange', look)
    look()
  })
  return registration
}

/** Keeps the words a mode's notification shows. They are that mode's server's,
 * and are kept under that mode alone. */
async function keepNotice(mode: Mode, notice: PushNotice): Promise<void> {
  const kept = await window.caches.open(NOTICE_CACHE)
  await kept.put(
    NOTICE_URL + mode,
    new Response(JSON.stringify({ title: notice.title, text: notice.text }), {
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

async function dropNotice(mode: Mode): Promise<void> {
  try {
    await (await window.caches.open(NOTICE_CACHE)).delete(NOTICE_URL + mode)
  } catch {
    // Words that could not be deleted are shown by no push: the subscription
    // they were for is let go of.
  }
}

/** The words a mode's notification shows on this device, or null when none
 * are kept. */
export async function keptNotice(mode: Mode): Promise<PushNotice | null> {
  try {
    const kept = await (
      await window.caches.open(NOTICE_CACHE)
    ).match(NOTICE_URL + mode)
    if (kept === undefined) return null
    const words = (await kept.json()) as Partial<PushNotice>
    return typeof words.title === 'string' && typeof words.text === 'string'
      ? { title: words.title, text: words.text }
      : null
  } catch {
    return null
  }
}

/** Shows, on this device, the notification a push from the `mode` server would
 * show, marked as a preview in its title and its text: no push was sent, and
 * nothing was decided. Answers `no-words` when the device has none to show.
 * Throws when the browser would not show it. */
export async function previewNotice(mode: Mode): Promise<'shown' | 'no-words'> {
  const notice = await keptNotice(mode)
  const registration = await registrationOf(mode)
  if (notice === null || registration === null) return 'no-words'
  await registration.showNotification(`Preview: ${notice.title}`, {
    body: `A preview, not a notification. What one would say: ${notice.text}`,
    tag: 'beacon-preview',
    icon: '/pwa-192x192.png',
  })
  return 'shown'
}

function alone<T>(work: () => Promise<T>): Promise<T> {
  const locks = (navigator as { locks?: LockManager }).locks
  if (locks === undefined) return Promise.reject(new Error('No lock.'))
  return locks.request(LOCK, work) as Promise<T>
}

/**
 * Turn notifications on for this device, for the `mode` the app is showing: ask
 * the browser's leave, and note that they are wanted. The server is told by the
 * sync that follows. Answers with what came of it: `on`; `blocked` when the
 * browser does not allow notifications from this site; `no-worker` when this
 * copy of the app cannot have a service worker to receive one; `failed` when
 * the browser cannot do this or would not keep the change.
 */
export async function turnOn(
  mode: Mode,
): Promise<'on' | 'blocked' | 'no-worker' | 'failed'> {
  if (!pushSupported()) return 'failed'
  let allowed: NotificationPermission
  try {
    allowed = await Notification.requestPermission()
  } catch {
    return 'failed'
  }
  if (allowed !== 'granted') return 'blocked'
  try {
    await workerFor(mode)
  } catch {
    return 'no-worker'
  }
  try {
    return await alone(async () => {
      if (!writeWanted(retired({ ...readWanted(), on: mode }))) return 'failed'
      show(() => {
        const was = deleted
        deleted = false
        return was
      })
      return 'on'
    })
  } catch {
    return 'failed'
  }
}

/**
 * Has the browser let go of every subscription that is not wanted: the other
 * mode's, and this one's once notifications are off. Each address is noted for
 * its server to delete first. Answers with whether the browser let go of all of
 * them, which `held` then says too.
 */
async function letGo(): Promise<boolean> {
  let all = true
  for (const mode of MODES) {
    if (readWanted().on === mode) continue
    let subscription: PushSubscription | null = null
    try {
      const registration = await registrationOf(mode)
      subscription = (await registration?.pushManager.getSubscription()) ?? null
    } catch {
      // A browser that cannot say what it holds is not said to hold anything.
    }
    if (subscription === null) continue
    // Noted for its server to delete before the browser is asked: the address
    // the server was told is noted already (`told`), and this covers one the
    // browser made since, and one this device has no note of at all.
    keep(queued({ endpoint: subscription.endpoint, mode }))
    try {
      if (!(await subscription.unsubscribe())) all = false
    } catch {
      all = false
    }
    await dropNotice(mode)
  }
  show(() => {
    const was = held
    held = !all
    return was !== held
  })
  return all
}

/**
 * Turn notifications off for this device. It needs no server and no mode: that
 * they are off is noted first, with the address for its server to delete, which
 * the sync that follows asks for, and asks again until the server has answered.
 * Then the browser is asked to let go of its subscription. Answers `off` when it
 * did, so that nothing can arrive; `held` when notifications are off here and
 * the browser would not let go, so that a push can still arrive until the server
 * has deleted the address; `failed` when the change was not kept.
 */
export async function turnOff(): Promise<'off' | 'held' | 'failed'> {
  try {
    return await alone(async () => {
      if (!writeWanted(untold({ ...readWanted(), on: null }))) return 'failed'
      return (await letGo()) ? 'off' : 'held'
    })
  } catch {
    return 'failed'
  }
}

/** The server is in another mode than the app is showing. */
export class OtherModeError extends Error {}

/** Has the `mode` server delete each address it is to delete. An address is
 * taken off the list only when the server answered that it did. Throws on the
 * first it did not. */
async function forgetHere(mode: Mode): Promise<void> {
  for (const address of readWanted().forget) {
    if (address.mode !== mode) continue
    await unsubscribePush(address.endpoint, mode)
    keep((now) => ({
      ...now,
      forget: now.forget.filter((kept) => !same(kept, address)),
    }))
    show(() => {
      const was = deleted
      deleted = true
      return !was
    })
  }
}

const rounded = () => placesSnapshot().map(({ lat, lon }) => ({ lat, lon }))

/** Make the server's copy match what is wanted now. Called under the lock. */
async function tell(mode: Mode): Promise<void> {
  // A choice made for one mode is not carried to the other.
  keep((now) =>
    retired(now.on !== null && now.on !== mode ? { ...now, on: null } : now),
  )
  await letGo()
  if (readWanted().on !== mode || Notification.permission !== 'granted') {
    // Off, or the browser's leave was taken back, and its subscription with
    // it: the server's copy is to go.
    keep(untold)
    await forgetHere(mode)
    if (readWanted().on !== mode) setTold(NOT_YET)
    return
  }
  await forgetHere(mode)
  const info = await fetchPushInfo()
  if (info.mode !== mode) throw new OtherModeError()
  if (!info.enabled || info.public_key === null || info.notice === null) {
    setTold({ told: 'server-off', problem: null, places: 0 })
    return
  }
  const registration = await workerFor(mode)
  let subscription = await registration.pushManager.getSubscription()
  if (subscription !== null && keyOf(subscription) !== info.public_key) {
    // Made for another key than this server's: this server cannot wake it. Its
    // address is noted for the server to delete before it is let go of.
    keep(queued({ endpoint: subscription.endpoint, mode }))
    await subscription.unsubscribe()
    subscription = null
  }
  // The server was told an address the browser no longer has: it made another,
  // or dropped it.
  const has = subscription?.endpoint ?? null
  keep((now) =>
    now.told !== null && now.told.endpoint !== has ? untold(now) : now,
  )
  await forgetHere(mode)
  if (placesSnapshot().length > 0) {
    // The words are kept before the server has the address, so that no push
    // can arrive before its words.
    await keepNotice(mode, info.notice)
    subscription ??= await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: bytesOf(info.public_key),
    })
    // Noted before the server is given it: were this tab closed before the
    // server answered, the address would still be known here, to be deleted.
    const address = { endpoint: subscription.endpoint, mode }
    keep((now) => ({
      ...now,
      told: address,
      forget: now.forget.filter((kept) => !same(kept, address)),
    }))
    // Read at the last moment, with nothing waited for between the reading and
    // the sending: a place that was removed while the browser subscribed is not
    // sent. One removed after this is sent by the sync its removal starts.
    const places = rounded()
    if (places.length > 0) {
      await subscribePush(address.endpoint, places, mode)
      setTold({ told: 'told', problem: null, places: places.length })
      return
    }
  }
  // With no place there is nothing to be notified about, and the server keeps
  // nothing of this device.
  keep(untold)
  await forgetHere(mode)
  setTold({ told: 'told', problem: null, places: 0 })
}

const NOT_TOLD =
  'Beacon’s server could not be told of the change to your notifications.'

/**
 * Make the server's copy match what is wanted on this device now. Answers with
 * whether it does: false when the server could not be told or the browser would
 * not let go of a subscription, which the status then says, and which is worth
 * trying again. Throws `OtherModeError` when the server is in another mode than
 * `mode`: nothing was kept or deleted by it.
 */
export async function syncPush(mode: Mode): Promise<boolean> {
  if (!pushSupported()) return true
  // Run with notifications off too: it asks the server nothing then, and finds
  // a subscription the browser still holds that nothing here has a note of.
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
  if (problem === null) return !held
  setTold({ told: 'failed', problem, places: told.places })
  return false
}

/** Forget what this tab knows. For tests. */
export function resetPush(): void {
  cache = null
  seenOn = null
  status = null
  told = NOT_YET
  held = false
  deleted = false
}
