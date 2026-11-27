import { ApiError, OTHER_MODE } from '../api/http.ts'
import type { Input } from '../api/health.ts'
import type { Mode } from '../api/incidents.ts'
import {
  fetchPushInfo,
  PERMIT_SPENT,
  permitPush,
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
//   that has it (`forget`); and the permit of a request to be notified that
//   may still be on its way to a server (`out`). Beside it (`OFF_KEY`), a mark
//   of the latest time
//   notifications were turned off, which `KEY` says it has taken note of
//   (`seen`). Beside those, the words a notification shows (a cache the service
//   worker reads, `public/push-sw.js`) and the browser's own subscription.
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
// another mode, or not reached has deleted nothing, and is asked again. No
// address is dropped from `forget` to make room: with `FORGET_MOST` of them
// unanswered, the server is given no new address until one has been answered.
//
// There is one way the server is told anything: `syncPush`, which reads what is
// wanted now and makes the server's copy match it. Turning notifications on or
// off, saving or removing a place, in this tab or another, only changes what is
// wanted; `usePushSync` (in `App`) then syncs. Only one sync runs at a time
// across the app's tabs, under the browser's lock. A sync tells the server every
// time, and keeps no memory of an earlier telling: another tab may have told it
// something else since.
//
// **Turning notifications off waits for no server and for no telling.** It
// writes the mark (`OFF_KEY`), a new one each time, which is one write that no
// other write can undo: nothing ever takes a mark away, and what is kept only
// notes which mark it has taken in (`seen`). From that write on, every reading
// here says they are off and that the address the server was given is one to
// delete (`read`), in every tab. A telling that was under way reads it after
// each thing it waited for (`fence` in `tell`) and gives the server nothing
// more; it then runs again as one for notifications that are off, and asks the
// server to delete. **A press of On that is older than a press of Off never
// undoes it**: `turnOn` takes the mark as it is when it is pressed, before the
// browser is asked anything, turns nothing on when the mark is another by the
// time its turn comes, and writes that it took note of that mark and of no
// later one, so an Off that comes after its last look still reads as off.
// Everything else that is kept (`KEY`) is written only under the lock, with the
// mark of the one reading the write was made from (`writeWanted`).
//
// **One telling waits for nothing longer than `SYNC_MS` in all**: a server that
// does not answer, or a browser that does not, ends it, and it is tried again
// later. Of that, the browser is given `BROWSER_MS` in all to say what it holds,
// let go, and delete the words, so that a browser that does not answer keeps
// neither the lock nor the request that has the server delete.
//
// **A request that was cut off can still reach the server, and puts nothing
// back.** A request to be notified names a permit the server gave for it
// (`permitPush`), good once. The permit is noted here (`out`) before the request
// is sent, and it is named when the server is asked to delete the address and
// when the next permit is asked for; the server takes it away in the same write.
// So a request that arrives after the server answered that it deleted the
// address keeps nothing, and neither does an older list of places that arrives
// after a newer one. The other way round, a request to delete that arrives late
// deletes an address that was given again since: the server then keeps nothing
// of this device until the app next tells it, which is the safe way to be wrong.
//
// **That holds for a deletion the app asks for, which names the permits. It
// does not hold for the one the service worker asks for** when the browser
// replaces an address while the app is closed (`pushsubscriptionchange` in
// `public/push-sw.js`): a worker reads nothing of what is kept here, so it names
// no permit, and a request to be notified that was still on its way can put the
// old address and its places back after the worker was answered. The app mends
// that when it is next opened: the address it told the server is not the one
// the browser has, so it has the server delete it, naming the permit. Until
// then the server keeps the old address as it keeps any: until a push to it is
// refused as gone, or its time is over.

const KEY = 'beacon.push.v2'
// Written anew each time notifications are turned off, and never taken away.
// Nothing reads it and writes it back, so two tabs cannot undo one another's
// writes of it.
export const OFF_KEY = 'beacon.push.off.v1'
// The same names as in `public/push-sw.js`.
export const NOTICE_CACHE = 'beacon-push-v2'
export const NOTICE_URL = '/__beacon/push-notice/'
export const WORKER = '/push-sw.js'
const LOCK = 'beacon.push'
// How long after a telling that failed the server is told again.
export const RETRY_MS = 30_000
// The longest one telling waits, for the server and the browser together.
export const SYNC_MS = 20_000
// The longest the browser is waited for when it is asked what it holds, or to
// let go, outside a telling.
export const BROWSER_MS = 5_000
// The most addresses kept for a server to delete before the server is given
// no new one. None is ever dropped to make room.
export const FORGET_MOST = 10
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
  // The permits of requests to be notified that were sent and not answered, at
  // most one for a mode: such a request can still reach its server.
  out: Out[]
}

type Out = { permit: string; mode: Mode }

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
// What was last read: what `KEY` holds (`blob`, and the mark it has taken note
// of, `seen`), the mark, and what is wanted, which is what `KEY` holds unless a
// mark it has not taken note of says they were turned off since.
type Read = {
  raw: string | null
  mark: string | null
  seen: string | null
  wanted: Wanted
}
let cache: Read | null = null
// The `on` that was last read, to tell a change of it from any other change.
let seenOn: Mode | null = null
const NOT_YET = { told: 'not-yet', problem: null, places: 0 } as const
let told: Pick<PushStatus, 'told' | 'problem' | 'places'> = NOT_YET
let held = false
let deleted = false
let status: { from: Read; shown: PushStatus } | null = null

const NOTHING_WANTED: Wanted = { on: null, told: null, forget: [], out: [] }

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

function outOf(value: unknown): Out | null {
  if (typeof value !== 'object' || value === null) return null
  const { permit, mode } = value as Record<string, unknown>
  return typeof permit === 'string' && isMode(mode) ? { permit, mode } : null
}

function parsed(raw: string | null): { blob: Wanted; seen: string | null } {
  try {
    const kept: unknown = raw === null ? null : JSON.parse(raw)
    if (typeof kept !== 'object' || kept === null)
      return { blob: NOTHING_WANTED, seen: null }
    const { on, told, forget, out, seen } = kept as Record<string, unknown>
    return {
      blob: {
        on: isMode(on) ? on : null,
        told: addressOf(told),
        // Every one of them: an address is dropped only when its server
        // answered.
        forget: Array.isArray(forget)
          ? forget.map(addressOf).filter((address) => address !== null)
          : [],
        out: Array.isArray(out)
          ? out.map(outOf).filter((permit) => permit !== null)
          : [],
      },
      seen: typeof seen === 'string' ? seen : null,
    }
  } catch {
    return { blob: NOTHING_WANTED, seen: null }
  }
}

function read(): Read {
  let raw: string | null = null
  let mark: string | null = null
  try {
    raw = window.localStorage.getItem(KEY)
    mark = window.localStorage.getItem(OFF_KEY)
  } catch {
    // Storage that is switched off keeps nothing, and notifications stay off.
  }
  if (cache !== null && cache.raw === raw && cache.mark === mark) return cache
  const { blob, seen } = parsed(raw)
  // Turned off since what is kept was written, whatever it says: the address
  // the server was given is one for it to delete.
  const off = mark !== null && mark !== seen
  cache = {
    raw,
    mark,
    seen,
    wanted: off ? untold({ ...blob, on: null }) : blob,
  }
  seenOn = cache.wanted.on
  return cache
}

/** What is wanted now, in every tab: read anew each time it is asked. */
function readWanted(): Wanted {
  return read().wanted
}

function changed(what: 'wanted' | 'shown'): void {
  status = null
  for (const listener of listeners) listener()
  if (what === 'wanted') for (const listener of wantedListeners) listener()
}

/** Change what is kept, from what one reading (`now`) said is wanted, and note
 * the mark of that same reading, never a later one: a turning off that came
 * after the reading is then one the write knows nothing of, and holds. Answers
 * with whether it is kept. Called under the lock only. */
function writeWanted(
  change: (now: Wanted) => Wanted,
  now: Read = read(),
): boolean {
  const raw = JSON.stringify({ ...change(now.wanted), seen: now.mark })
  // Nothing is written that says what is there already, and so nothing at
  // all on a device that never turned notifications on.
  const { blob, seen } = parsed(now.raw)
  if (raw === JSON.stringify({ ...blob, seen })) return true
  try {
    window.localStorage.setItem(KEY, raw)
  } catch {
    return false
  }
  cache = null
  changed(now.wanted.on !== readWanted().on ? 'wanted' : 'shown')
  return true
}

/** Change what is kept, or throw: a telling that could not note down what it
 * is about to do does not do it. Called under the lock only. */
function keep(change: (now: Wanted) => Wanted): void {
  if (!writeWanted(change))
    throw new Error('The browser would not keep the change.')
}

/** Write what a turning off means into what is kept, when it is not there
 * yet. Called under the lock only. */
function settle(): void {
  keep((now) => now)
}

const same = (one: Address, other: Address) =>
  one.endpoint === other.endpoint && one.mode === other.mode

/** With `address` among those a server is to delete. */
const queued =
  (address: Address) =>
  (now: Wanted): Wanted => ({
    ...now,
    forget: [...now.forget.filter((kept) => !same(kept, address)), address],
  })

/** Whether an address is one this device has a note of: the one a server was
 * given, or one a server is to delete. */
function noted(address: Address): boolean {
  const now = readWanted()
  return (
    (now.told !== null && same(now.told, address)) ||
    now.forget.some((kept) => same(kept, address))
  )
}

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
  if (event.key !== null && event.key !== KEY && event.key !== OFF_KEY) return
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
  const from = read()
  const wanted = from.wanted
  if (status === null || status.from !== from)
    status = {
      from,
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

/** Deletes the words a mode's notification shows, waiting for the browser no
 * later than `until`. */
async function dropNotice(mode: Mode, until: number): Promise<void> {
  try {
    await within(until - Date.now(), async () => {
      await (await window.caches.open(NOTICE_CACHE)).delete(NOTICE_URL + mode)
    })
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

/** Something was waited for as long as it may be. */
class TookTooLong extends Error {}

/** `work`, for `ms` at the most: then its signal is aborted and this rejects
 * with `TookTooLong`, whatever `work` goes on to do. */
function within<T>(
  ms: number,
  work: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  if (ms <= 0) return Promise.reject(new TookTooLong())
  const control = new AbortController()
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      control.abort()
      reject(new TookTooLong())
    }, ms)
    const ended = () => clearTimeout(timer)
    try {
      // Begun at once: nothing is waited for between a caller's reading of
      // what to send and the sending.
      work(control.signal).then(resolve, reject).finally(ended)
    } catch (error) {
      ended()
      reject(error instanceof Error ? error : new Error('It could not begin.'))
    }
  })
}

/**
 * Turn notifications on for this device, for the `mode` the app is showing: ask
 * the browser's leave, and note that they are wanted. The server is told by the
 * sync that follows. Answers with what came of it: `on`; `blocked` when the
 * browser does not allow notifications from this site; `no-worker` when this
 * copy of the app cannot have a service worker to receive one; `failed` when
 * the browser cannot do this or would not keep the change; `overtaken` when
 * notifications were turned off, in this tab or another, after this was
 * pressed, and are off.
 */
export async function turnOn(
  mode: Mode,
): Promise<'on' | 'blocked' | 'no-worker' | 'overtaken' | 'failed'> {
  if (!pushSupported()) return 'failed'
  // The latest turning off there was when this was pressed, before anything
  // is waited for: one that comes from here on is the later press.
  const before = read().mark
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
      // One reading, which the write below is made from and whose mark it
      // notes. A second reading could take note of a turning off it never saw.
      const now = read()
      if (now.mark !== before) {
        // Turned off, in this tab or another, since this was pressed: the
        // later press holds.
        settle()
        return 'overtaken'
      }
      if (!writeWanted((wanted) => retired({ ...wanted, on: mode }), now))
        return 'failed'
      // Turned off after that reading: what was written knows nothing of it,
      // and notifications are off.
      if (readWanted().on !== mode) return 'overtaken'
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
 * mode's, and this one's once notifications are off. Answers with whether it is
 * known that the browser holds none of them any more, which `held` then says
 * too: a browser that would not let go, would not say what it holds, or did not
 * answer may hold one still. **Nothing here is waited for past `until`**, which
 * is one moment for all of it: what the browser holds, its letting go, and the
 * deleting of the words, for both modes.
 *
 * Under the lock (`noting`), each address is noted for its server to delete
 * before the browser is asked to let go of it. Outside it, where nothing is
 * written, only an address this device already has a note of is let go of; any
 * other is left for the telling that follows, and counts as held until then.
 */
async function letGo(
  noting: boolean,
  until = Date.now() + BROWSER_MS,
): Promise<boolean> {
  let all = true
  const wordless: Mode[] = []
  for (const mode of MODES) {
    if (readWanted().on === mode) continue
    const held = () =>
      within(until - Date.now(), async () => {
        const registration = await registrationOf(mode)
        return (await registration?.pushManager.getSubscription()) ?? null
      })
    let subscription: PushSubscription | null
    try {
      subscription = await held()
    } catch {
      // A browser that did not say what it holds may hold a subscription.
      all = false
      continue
    }
    // Turned on for this mode while the browser was asked: it is wanted again.
    if (subscription === null || readWanted().on === mode) continue
    const address = { endpoint: subscription.endpoint, mode }
    // Noted for its server to delete before the browser is asked: the address
    // the server was told is noted already (`told`), and this covers one the
    // browser made since, and one this device has no note of at all.
    if (noting) keep(queued(address))
    else if (!noted(address)) {
      all = false
      continue
    }
    const mine = subscription
    let gone = false
    try {
      gone = await within(until - Date.now(), () => mine.unsubscribe())
      // A browser also answers that it did not when the subscription was let
      // go of already, by another tab, say. What it holds now is what counts.
      if (!gone) gone = (await held()) === null
    } catch {
      // Not known to be gone.
    }
    if (!gone) all = false
    wordless.push(mode)
  }
  // Last, and together, with what is left of the time: a browser that is slow
  // to delete the words takes nothing from the letting go, which is what keeps
  // a push from being delivered.
  await Promise.all(wordless.map((mode) => dropNotice(mode, until)))
  show(() => {
    const was = held
    held = !all
    return was !== held
  })
  return all
}

/**
 * Turn notifications off for this device. It waits for no server and for no
 * telling that is under way: that they are off is one write (`OFF_KEY`),
 * which nothing undoes,
 * from which on every tab reads them as off and the address the server was
 * given as one for it to delete. A telling under way gives the server nothing
 * more once it has read that; the one that follows asks the server to delete,
 * and asks again until it has answered. Then the browser is asked to let go of
 * its subscription. Answers `off` when it did, so that the push service has
 * nowhere to deliver to; `held` when notifications are off here and it is not
 * known that the browser let go, so that a push can still arrive until the
 * server has deleted the address; `failed` when the change was not kept.
 */
export async function turnOff(): Promise<'off' | 'held' | 'failed'> {
  try {
    window.localStorage.setItem(OFF_KEY, `${Date.now()}-${Math.random()}`)
  } catch {
    return 'failed'
  }
  cache = null
  // Told as a change of what is wanted every time, so that a telling follows.
  changed('wanted')
  const locks = (navigator as { locks?: LockManager }).locks
  try {
    // With no telling under way, in this tab or another, the browser lets go
    // under the lock, where an address this device has no note of can be noted
    // first. With one under way, it is not waited for.
    const came =
      locks === undefined
        ? null
        : ((await locks.request(LOCK, { ifAvailable: true }, async (lock) => {
            if (lock === null) return null
            settle()
            return await letGo(true)
          })) as boolean | null)
    if (came !== null) return came ? 'off' : 'held'
  } catch {
    // What could not be done under the lock is tried without it.
  }
  try {
    return (await letGo(false)) ? 'off' : 'held'
  } catch {
    return 'held'
  }
}

/** The server is in another mode than the app is showing. */
export class OtherModeError extends Error {}

/** Notifications were turned off, or on for another mode, under a telling. */
class Overtaken extends Error {}

/** As many addresses as are kept wait for a server to answer that it deleted
 * them, so the server is given no new one. */
class Unanswered extends Error {}

type Step = <T>(work: (signal: AbortSignal) => Promise<T>) => Promise<T>

const outFor = (mode: Mode) =>
  readWanted()
    .out.filter((sent) => sent.mode === mode)
    .map((sent) => sent.permit)

/** Without the permits `gone` of the `mode` server's. */
const withdrawn =
  (mode: Mode, gone: string[]) =>
  (now: Wanted): Wanted => ({
    ...now,
    out: now.out.filter(
      (sent) => !(sent.mode === mode && gone.includes(sent.permit)),
    ),
  })

/** Has the `mode` server delete each address it is to delete, and with it take
 * away the permits of the requests to be notified that may still be on their
 * way to it, so that none of them puts an address back. An address is taken off
 * the list only when the server answered that it did. Throws on the first it
 * did not. */
async function forgetHere(mode: Mode, step: Step): Promise<void> {
  for (const address of readWanted().forget) {
    if (address.mode !== mode) continue
    const permits = outFor(mode)
    await step((signal) =>
      unsubscribePush(address.endpoint, mode, permits, signal),
    )
    keep((now) => ({
      ...withdrawn(mode, permits)(now),
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

/** What the server said it takes from a visitor at the latest asking, read
 * when it is called: `own`, `invented`, or null when the server has not said.
 * The server is given an address and places only while this answers `own`. */
export type Takes = () => Input | null

/** Make the server's copy match what is wanted now, waiting for nothing past
 * `until`. Called under the lock. Throws `Overtaken` when notifications were
 * turned off while it waited for something: nothing more was given to the
 * server, and it is to be called again. */
async function tell(mode: Mode, until: number, takes: Takes): Promise<void> {
  const step: Step = (work) => within(until - Date.now(), work)
  // Asked after everything that is waited for on the way to giving the server
  // an address: whether notifications are still wanted for this mode, and
  // whether the server still says it takes an address of a visitor's own. The
  // server can stop saying so while a telling waits, and the telling that is
  // begun again then gives it nothing.
  const fence = () => {
    if (readWanted().on !== mode || takes() !== 'own') throw new Overtaken()
  }
  settle()
  // A choice made for one mode is not carried to the other.
  keep((now) =>
    retired(now.on !== null && now.on !== mode ? { ...now, on: null } : now),
  )
  // The browser has a part of the telling's time and no more, so that the
  // server is asked to delete whatever the browser does.
  await letGo(true, Math.min(until, Date.now() + BROWSER_MS))
  if (readWanted().on !== mode || Notification.permission !== 'granted') {
    // Off, or the browser's leave was taken back, and its subscription with
    // it: the server's copy is to go.
    keep(untold)
    await forgetHere(mode, step)
    if (readWanted().on !== mode) setTold(NOT_YET)
    return
  }
  await forgetHere(mode, step)
  const taken = takes()
  if (taken === null) {
    // The server has not said what it takes. It is given no address and no
    // place, anew or again, and what it was given before is left as it is:
    // the deleting above is all that is asked of it.
    setTold(NOT_YET)
    return
  }
  const info = await step((signal) => fetchPushInfo(signal))
  if (info.mode !== mode) throw new OtherModeError()
  if (readWanted().on !== mode) throw new Overtaken()
  const key = info.public_key
  const notice = info.notice
  // A server that takes only the invented places takes no address, whatever
  // it answers here.
  if (taken !== 'own' || !info.enabled || key === null || notice === null) {
    // A server that sends none takes no address and no change of places, so
    // the copy an earlier one kept can no longer be made to match what is
    // wanted: a place removed since would stay in it. It is deleted, which
    // such a server does, and the address is given anew, with the places as
    // they are then, when a server that sends is next told.
    keep(untold)
    await forgetHere(mode, step)
    setTold({ told: 'server-off', problem: null, places: 0 })
    return
  }
  const registration = await step(() => workerFor(mode))
  let subscription = await step(() =>
    registration.pushManager.getSubscription(),
  )
  fence()
  if (subscription !== null && keyOf(subscription) !== key) {
    // Made for another key than this server's: this server cannot wake it. Its
    // address is noted for the server to delete before it is let go of.
    keep(queued({ endpoint: subscription.endpoint, mode }))
    const old = subscription
    await step(() => old.unsubscribe())
    subscription = null
  }
  // The server was told an address the browser no longer has: it made another,
  // or dropped it.
  const has = subscription?.endpoint ?? null
  keep((now) =>
    now.told !== null && now.told.endpoint !== has ? untold(now) : now,
  )
  await forgetHere(mode, step)
  fence()
  if (placesSnapshot().length > 0) {
    // Room for it is seen to before the server is given an address: one day it
    // is one more for a server to delete, and none is dropped to make room.
    const now = readWanted()
    if (now.told === null && now.forget.length >= FORGET_MOST)
      throw new Unanswered()
    // The words are kept before the server has the address, so that no push
    // can arrive before its words.
    await step(() => keepNotice(mode, notice))
    subscription ??= await step(() =>
      registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: bytesOf(key),
      }),
    )
    // Turned off while the browser made it: the server is not given it, and
    // the telling that is run next has the browser let go of it.
    fence()
    // A permit for this one request, which also takes away the permit of an
    // earlier request that was never answered: that one, with the places as
    // they were then, is too late whenever it comes.
    const earlier = outFor(mode)
    const { permit } = await step((signal) => permitPush(earlier, mode, signal))
    fence()
    // Noted before the server is given it: were this tab closed before the
    // server answered, the address would still be known here, to be deleted,
    // and so would the permit of the request that gave it.
    const address = { endpoint: subscription.endpoint, mode }
    // Read at the last moment, with nothing waited for between the reading and
    // the sending: a place that was removed while the browser subscribed, or
    // while the permit was asked for, is not sent. One removed after this is
    // sent by the sync its removal starts.
    const places = rounded()
    keep((now) => ({
      ...now,
      told: address,
      forget: now.forget.filter((kept) => !same(kept, address)),
      // The earlier permits were taken away with the giving of this one, and
      // this one is out only when a request is about to carry it.
      out: [
        ...now.out.filter((sent) => sent.mode !== mode),
        ...(places.length > 0 ? [{ permit, mode }] : []),
      ],
    }))
    if (places.length > 0) {
      // Asked once more, with nothing waited for between this and the sending:
      // another tab can have turned notifications off while that was noted
      // down, and where the places are is not sent once this one has read so.
      fence()
      try {
        await step((signal) =>
          subscribePush(address.endpoint, places, mode, permit, signal),
        )
      } catch (error) {
        // Refused because the permit is no longer good: the request kept
        // nothing and never will, so it is not out.
        if (error instanceof ApiError && error.status === PERMIT_SPENT)
          keep(withdrawn(mode, [permit]))
        throw error
      }
      // Answered: the permit is used up, and the request is not out.
      keep(withdrawn(mode, [permit]))
      // Turned off while the server was given it: the address is one for the
      // server to delete, which the telling that is run next asks for.
      fence()
      setTold({ told: 'told', problem: null, places: places.length })
      return
    }
  }
  // With no place there is nothing to be notified about, and the server keeps
  // nothing of this device.
  keep(untold)
  await forgetHere(mode, step)
  setTold({ told: 'told', problem: null, places: 0 })
}

const NOT_TOLD =
  'Beacon’s server could not be told of the change to your notifications.'
const UNANSWERED =
  'This device is still waiting for Beacon’s servers to answer that they deleted earlier addresses of it, and keeps only so many of those at a time. Beacon’s server is given this device’s address once one of them has answered.'
// How many times in a row a telling is begun again because notifications were
// turned off or on under it.
const OVERTAKEN_MOST = 3

/**
 * Make the server's copy match what is wanted on this device now, as far as
 * the server takes it (`takes`): an address and places only for `own`.
 * Deleting is asked for whatever it answers. Answers with
 * whether it does: false when the server could not be told or it is not known
 * that the browser let go of a subscription, which the status then says, and
 * which is worth trying again. Throws `OtherModeError` when the server is in
 * another mode than `mode`: nothing was kept or deleted by it.
 */
export async function syncPush(mode: Mode, takes: Takes): Promise<boolean> {
  if (!pushSupported()) return true
  // Run with notifications off too: it asks the server nothing then, and finds
  // a subscription the browser still holds that nothing here has a note of.
  let problem: string | null = null
  try {
    await alone(async () => {
      for (let begun = 1; ; begun += 1) {
        try {
          // Each has its own time: the one begun after notifications were
          // turned off is the one that asks the server to delete.
          return await tell(mode, Date.now() + SYNC_MS, takes)
        } catch (error) {
          if (!(error instanceof Overtaken) || begun >= OVERTAKEN_MOST)
            throw error
        }
      }
    })
  } catch (error) {
    if (error instanceof OtherModeError) throw error
    if (error instanceof ApiError && error.status === OTHER_MODE)
      throw new OtherModeError()
    // The change that overtook it starts a telling of its own.
    if (error instanceof Overtaken) return false
    // The server's own sentence when it gave one.
    problem =
      error instanceof ApiError
        ? error.message
        : error instanceof Unanswered
          ? UNANSWERED
          : NOT_TOLD
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
