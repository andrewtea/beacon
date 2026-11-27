import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import worker from '../public/push-sw.js?raw'
import App from './App.tsx'
import type { Mode } from './api/incidents.ts'
import type { PushInfo } from './api/push.ts'
import { removePlace, savePlace } from './lib/placeStore.ts'
import {
  BROWSER_MS,
  FORGET_MOST,
  NOTICE_CACHE,
  NOTICE_URL,
  OFF_KEY,
  OtherModeError,
  pushSnapshot,
  resetPush,
  RETRY_MS,
  scopeOf,
  SYNC_MS,
  syncPush,
  turnOff,
  turnOn,
  WORKER,
} from './lib/push.ts'
import { type Asked, MOCK_HEALTH, serve } from './test/server.ts'

const KEY = 'beacon.push.v2'
const INFO = 'GET /api/push'
const SUBSCRIBE = 'POST /api/push/subscribe'
const UNSUBSCRIBE = 'POST /api/push/unsubscribe'
const CHECK = 'POST /api/places/check'
const FIRE = { lat: 37.776, lon: -122.424 }
const FAR = { lat: 37.8, lon: -122.41 }
const ADDRESS = 'https://fcm.googleapis.com/fcm/send/abc:DEF_123'

// Sentences of the server's, which the app shows and keeps as they are given.
const ABOUT =
  'To notify this device, Beacon keeps the address your browser’s push service made for it and the places you have saved.'
const NOTICE = {
  title: 'Beacon: invented scenario',
  text: 'Invented scenario, not a real event. A stand-in turned crisis mode on.',
}
// A public key as the server writes one: 65 bytes, in URL-safe base64.
const KEY_BYTES = Uint8Array.from({ length: 65 }, (_, index) =>
  index === 0 ? 4 : (index * 7) % 256,
)
const base64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
const PUBLIC_KEY = base64url(KEY_BYTES)
const info = (from: Partial<PushInfo> = {}): PushInfo => ({
  mode: 'mock',
  enabled: true,
  text: null,
  public_key: PUBLIC_KEY,
  about_text: ABOUT,
  notice: NOTICE,
  reach_m: 1000,
  max_places: 5,
  kept_days: 30,
  ...from,
})
const SUBSCRIBED = { mode: 'mock', kept_until: '2026-12-27T00:00:00Z' }
const NOTHING_NEAR = {
  mode: 'mock',
  as_of: '2026-11-26T23:44:34Z',
  partial: false,
  reach_m: 1000,
  caveat_text: 'A distance is rough.',
  places: [],
}

type FakeSubscription = {
  endpoint: string
  options: { applicationServerKey: ArrayBuffer | null }
  unsubscribe: ReturnType<typeof vi.fn<() => Promise<boolean>>>
}

const MOCK_SERVER = { on: 'mock', told: { endpoint: ADDRESS, mode: 'mock' } }
/** What a device keeps once notifications are on for the invented scenario and
 * its server was given `endpoint`. */
const turnedOn = (endpoint: string | null = ADDRESS) =>
  JSON.stringify({
    on: 'mock',
    told: endpoint === null ? null : { endpoint, mode: 'mock' },
    forget: [],
  })
const modeOfScope = (scope: string): Mode =>
  scope.endsWith('/push/live/') ? 'live' : 'mock'

/** The browser's side of web push, which jsdom has none of: service worker
 * registrations, each with its push manager, the permission to notify, and the
 * cache the notifications' words are kept in. `events` is what was done, in
 * order. The app's other service worker, at the site's root, is there too, and
 * fails the test if a push is ever asked of it. */
function browser({
  permission = 'default' as NotificationPermission,
  grants = 'granted' as NotificationPermission,
  worker: hasWorker = true,
  subscribed = null as { mode: Mode; key: Uint8Array } | null,
} = {}) {
  const events: string[] = []
  const registered: string[] = []
  const kept = new Map<string, string>()
  const shown: { mode: Mode; title: string; options: NotificationOptions }[] =
    []
  const subscriptions = new Map<Mode, FakeSubscription>()
  const registrations = new Map<string, unknown>()
  const device = {
    events,
    registered,
    kept,
    shown,
    // Whether the browser lets go of a subscription when it is asked to.
    letsGo: true,
    // Whether the browser says what it holds when it is asked.
    says: true,
    // Done while the browser makes a subscription.
    whileSubscribing: async () => {},
    showing: vi.fn(async () => {}),
    subscribe: vi.fn(),
    notification: {
      permission,
      requestPermission: vi.fn(async () => {
        events.push('ask permission')
        device.notification.permission = grants
        return grants
      }),
    },
    subscription: (mode: Mode) => subscriptions.get(mode) ?? null,
    words: (mode: Mode): unknown => {
      const words = kept.get(`${NOTICE_CACHE} ${NOTICE_URL}${mode}`)
      return words === undefined ? null : JSON.parse(words)
    },
  }
  let made = 0
  const subscriptionFor = (mode: Mode, key: Uint8Array, endpoint: string) => {
    const mine: FakeSubscription = {
      endpoint,
      options: { applicationServerKey: key.slice().buffer },
      unsubscribe: vi.fn(async () => {
        events.push(`unsubscribe ${endpoint}`)
        if (!device.letsGo) return false
        if (subscriptions.get(mode) === mine) subscriptions.delete(mode)
        return true
      }),
    }
    subscriptions.set(mode, mine)
    return mine
  }
  const registrationAt = (scope: string) => {
    const mode = modeOfScope(scope)
    const found = registrations.get(scope)
    if (found !== undefined) return found
    const registration = {
      scope: new URL(scope, window.location.href).href,
      active: {},
      installing: null,
      waiting: null,
      pushManager: {
        getSubscription: async () => subscriptions.get(mode) ?? null,
        subscribe: async (options: {
          userVisibleOnly: boolean
          applicationServerKey: Uint8Array
        }) => {
          device.subscribe(options)
          await device.whileSubscribing()
          made += 1
          events.push(`subscribe ${mode}`)
          return subscriptionFor(
            mode,
            options.applicationServerKey,
            made === 1 ? ADDRESS : `${ADDRESS}-${made}`,
          )
        },
      },
      showNotification: async (title: string, options: NotificationOptions) => {
        await device.showing()
        shown.push({ mode, title, options })
      },
    }
    registrations.set(scope, registration)
    return registration
  }
  if (subscribed !== null) {
    registrationAt(scopeOf(subscribed.mode))
    subscriptionFor(subscribed.mode, subscribed.key, `${ADDRESS}-old`)
  }
  // The registration of the app's own service worker. No push is its to take.
  const notThisOne = () => {
    throw new Error('The app’s own service worker receives no push.')
  }
  const root = {
    scope: new URL('/', window.location.href).href,
    active: {},
    pushManager: { getSubscription: notThisOne, subscribe: notThisOne },
    showNotification: notThisOne,
  }
  vi.stubGlobal('Notification', device.notification)
  vi.stubGlobal('PushManager', class {})
  vi.stubGlobal('caches', {
    open: vi.fn(async (name: string) => ({
      put: async (url: string, response: Response) => {
        kept.set(`${name} ${url}`, await response.text())
        events.push(`keep the words ${url.slice(NOTICE_URL.length)}`)
      },
      match: async (url: string) => {
        const body = kept.get(`${name} ${url}`)
        return body === undefined ? undefined : new Response(body)
      },
      delete: async (url: string) => {
        events.push(`delete the words ${url.slice(NOTICE_URL.length)}`)
        return kept.delete(`${name} ${url}`)
      },
    })),
  })
  Object.defineProperty(window.navigator, 'serviceWorker', {
    value: {
      register: vi.fn(async (url: string, options: { scope: string }) => {
        if (!hasWorker) throw new TypeError('No service worker here.')
        registered.push(`${url} ${options.scope}`)
        return registrationAt(options.scope)
      }),
      // As a browser answers: the registration whose scope holds the address
      // asked about, which is the app's own when no other does.
      getRegistration: vi.fn(async (scope?: string) => {
        if (!device.says) throw new DOMException('No.', 'InvalidStateError')
        return (
          (scope === undefined ? undefined : registrations.get(scope)) ?? root
        )
      }),
    },
    configurable: true,
  })
  return device
}

const routes = (more: Record<string, unknown> = {}) => ({
  'GET /api/health': MOCK_HEALTH,
  [INFO]: info(),
  [SUBSCRIBE]: SUBSCRIBED,
  [UNSUBSCRIBE]: () => new Response(null, { status: 204 }),
  [CHECK]: NOTHING_NEAR,
  ...more,
})
const of = (asked: Asked[], key: string) =>
  asked.filter((request) => request.key === key)
const click = (element: HTMLElement) => act(async () => element.click())
type Kept = {
  on: unknown
  told: unknown
  forget: unknown[]
  seen?: string | null
} | null
/** What the device has noted down as wanted: what is kept, read as off, with
 * the address the server was given among those to delete, when notifications
 * were turned off since it was written. */
const wanted = (): unknown => {
  const kept = JSON.parse(window.localStorage.getItem(KEY) ?? 'null') as Kept
  if (kept === null) return null
  const { on, told, forget } = kept
  const mark = window.localStorage.getItem(OFF_KEY)
  if (mark === null || mark === (kept.seen ?? null)) return { on, told, forget }
  return {
    on: null,
    told: null,
    forget: [...forget, ...(told === null ? [] : [told])],
  }
}
const OFF = { on: null, told: null, forget: [] }
const busy = () =>
  Response.json({ detail: 'Beacon is busy. Try again.' }, { status: 503 })

beforeEach(() => {
  window.localStorage.clear()
  window.location.hash = '#/places'
  resetPush()
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  Reflect.deleteProperty(window.navigator, 'serviceWorker')
})

test('a browser that cannot be notified is told so, and nothing is asked of it or of the server', async () => {
  const asked = serve(routes())

  render(<App />)

  expect(
    await screen.findByText(/This browser cannot receive notifications/),
  ).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /notifications/i })).toBeNull()
  await waitFor(() => expect(of(asked, 'GET /api/health')).toHaveLength(1))
  expect(asked.filter((request) => request.key.includes('/api/push'))).toEqual(
    [],
  )
})

test('before notifications are turned on, the server’s words say what is kept, and nothing is asked or sent', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  const asked = serve(routes())

  render(<App />)

  expect(await screen.findByText(ABOUT)).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'Turn notifications on' }),
  ).toBeEnabled()
  // The browser is not asked for its leave until the person presses, no
  // service worker is registered, and the server is given nothing of this
  // device.
  expect(device.events).toEqual([])
  expect(device.registered).toEqual([])
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(of(asked, UNSUBSCRIBE)).toEqual([])
  expect(wanted()).toBeNull()
})

test('turning notifications on keeps the server’s words, then sends the address and where the places are, and nothing else', async () => {
  await savePlace('Home', { lat: 37.77612345, lon: -122.42398765 })
  await savePlace('Work', FAR)
  const device = browser()
  // What the device had noted down when the server was given the address.
  let notedWhenSent: unknown = null
  const asked = serve(
    routes({
      [SUBSCRIBE]: () => {
        notedWhenSent = wanted()
        return Response.json(SUBSCRIBED)
      },
    }),
  )
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  expect(
    await screen.findByText(
      /Beacon’s server has this device’s address and where your 2 saved places are\./,
    ),
  ).toBeInTheDocument()
  // In this order: the person's leave, the words, the subscription. The words
  // are on the device before the server has the address.
  expect(device.events).toEqual([
    'ask permission',
    'keep the words mock',
    'subscribe mock',
  ])
  // A service worker of its own for the mode that is shown, and for no other.
  expect(new Set(device.registered)).toEqual(
    new Set([`${WORKER} ${scopeOf('mock')}`]),
  )
  const [[subscribed]] = device.subscribe.mock.calls
  expect(subscribed.userVisibleOnly).toBe(true)
  expect([...subscribed.applicationServerKey]).toEqual([...KEY_BYTES])
  expect(device.words('mock')).toEqual(NOTICE)
  expect(device.words('live')).toBeNull()
  const sent = of(asked, SUBSCRIBE)
  expect(sent).toHaveLength(1)
  // No name, no id, and each place rounded to about a block.
  expect(sent[0].body).toEqual({ endpoint: ADDRESS, places: [FIRE, FAR] })
  expect(sent[0].headers['beacon-mode']).toBe('mock')
  // The address was noted on the device before the server had it: were the tab
  // closed before the server answered, the device could still have it deleted.
  expect(notedWhenSent).toEqual({ ...MOCK_SERVER, forget: [] })
  expect(wanted()).toEqual({ ...MOCK_SERVER, forget: [] })
  expect(JSON.stringify(asked)).not.toContain('Home')
})

test('the server is told again when a place is saved or removed, and deletes the address once none is saved', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  const asked = serve(routes())
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))

  await act(() => savePlace('Work', FAR))

  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(2))
  expect(of(asked, SUBSCRIBE)[1].body).toEqual({
    endpoint: ADDRESS,
    places: [FIRE, FAR],
  })
  const ids = (
    JSON.parse(window.localStorage.getItem('beacon.places.v1')!) as {
      id: string
    }[]
  ).map((place) => place.id)
  for (const id of ids) await act(() => removePlace(id))

  // With no place, there is nothing to be notified about.
  await waitFor(() => expect(of(asked, UNSUBSCRIBE)).toHaveLength(1))
  expect(of(asked, UNSUBSCRIBE)[0].body).toEqual({ endpoint: ADDRESS })
  expect(of(asked, UNSUBSCRIBE)[0].headers['beacon-mode']).toBe('mock')
  expect(
    await screen.findByText(
      /With no place saved, Beacon’s server has no address for this device\./,
    ),
  ).toBeInTheDocument()
  expect(wanted()).toEqual({ on: 'mock', told: null, forget: [] })
  expect(of(asked, SUBSCRIBE).at(-1)?.body).not.toEqual({
    endpoint: ADDRESS,
    places: [],
  })
  // The browser's own subscription stays, for the next place.
  expect(device.subscription('mock')?.endpoint).toBe(ADDRESS)
  expect(pushSnapshot().on).toBe('mock')
})

test('the app renews what the server keeps when it is opened, with the subscription the browser has', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  const asked = serve(routes())

  render(<App />)

  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  expect(of(asked, SUBSCRIBE)[0].body).toEqual({
    endpoint: `${ADDRESS}-old`,
    places: [FIRE],
  })
  // The subscription the browser has is the one renewed: no other is made.
  expect(device.subscribe).not.toHaveBeenCalled()
  expect(device.notification.requestPermission).not.toHaveBeenCalled()
  // What a telling notes down is no reason to tell again, in this tab or
  // another: the server is told once.
  await act(async () => {
    window.dispatchEvent(new StorageEvent('storage', { key: KEY }))
  })
  await screen.findByText(/where your saved place is\./)
  expect(of(asked, SUBSCRIBE)).toHaveLength(1)
  expect(of(asked, UNSUBSCRIBE)).toEqual([])
})

test('a telling tells the server whatever this tab told it before, because another tab may have told it otherwise', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  browser({ permission: 'granted' })
  const asked = serve(routes())

  expect(await syncPush('mock')).toBe(true)
  expect(await syncPush('mock')).toBe(true)

  expect(of(asked, SUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS, places: [FIRE] },
    { endpoint: ADDRESS, places: [FIRE] },
  ])
})

test('turning notifications off notes the address, lets go of it, and says it is deleted once the server has answered so', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  let answer = () => new Response(null, { status: 204 })
  const asked = serve(routes({ [UNSUBSCRIBE]: () => answer() }))
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  device.events.length = 0
  // What the device had noted down when the browser was asked to let go.
  let notedWhenLetGo: unknown = null
  const letGo = device.subscription('mock')!.unsubscribe
  const inner = letGo.getMockImplementation()!
  letGo.mockImplementation(async () => {
    notedWhenLetGo = wanted()
    return inner()
  })
  // The server cannot be reached just now.
  answer = () => {
    throw new TypeError('Failed to fetch')
  }

  await click(screen.getByRole('button', { name: 'Turn notifications off' }))

  // Off on the device, with the address noted for the server before the
  // browser let go of it.
  const pending = { on: null, told: null, forget: [MOCK_SERVER.told] }
  expect(notedWhenLetGo).toEqual(pending)
  expect(device.events).toEqual([
    `unsubscribe ${ADDRESS}`,
    'delete the words mock',
  ])
  expect(device.subscription('mock')).toBeNull()
  expect(device.words('mock')).toBeNull()
  // The server has not answered, and the app does not say it has.
  expect(
    await screen.findByText(
      /Beacon’s server has not yet answered that it deleted this device’s address\./,
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText(/has answered that it deleted/)).toBeNull()
  expect(wanted()).toEqual(pending)

  answer = () => new Response(null, { status: 204 })
  expect(await syncPush('mock')).toBe(true)

  await waitFor(() => expect(wanted()).toEqual(OFF))
  expect(
    await screen.findByText(
      /Beacon’s server has answered that it deleted this device’s address\./,
    ),
  ).toBeInTheDocument()
  const sent = of(asked, UNSUBSCRIBE).at(-1)!
  expect(sent.body).toEqual({ endpoint: ADDRESS })
  expect(sent.headers['beacon-mode']).toBe('mock')
  expect(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  ).toBeEnabled()
  // The places stay on the device: they are the person's, not the server's.
  expect(screen.getByRole('heading', { name: 'Home' })).toBeInTheDocument()
})

test.each([
  ['is busy', busy],
  [
    'cannot be reached',
    () => {
      throw new TypeError('Failed to fetch')
    },
  ],
  ['errs', () => new Response('oops', { status: 500 })],
])(
  'a server that %s has deleted nothing: the address is kept and it is asked again until it answers',
  async (_what, refusal) => {
    await savePlace('Home', FIRE)
    browser()
    let answers: () => Response = () => new Response(null, { status: 204 })
    const asked = serve(routes({ [UNSUBSCRIBE]: () => answers() }))
    render(<App />)
    await click(
      await screen.findByRole('button', { name: 'Turn notifications on' }),
    )
    await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    answers = refusal

    await click(screen.getByRole('button', { name: 'Turn notifications off' }))

    expect(
      await screen.findByText(/has not yet answered that it deleted/),
    ).toBeInTheDocument()
    await waitFor(() =>
      expect(of(asked, UNSUBSCRIBE).length).toBeGreaterThanOrEqual(1),
    )
    expect(wanted()).toEqual({
      on: null,
      told: null,
      forget: [MOCK_SERVER.told],
    })
    expect(screen.queryByText(/has answered that it deleted/)).toBeNull()
    const tried = of(asked, UNSUBSCRIBE).length

    answers = () => new Response(null, { status: 204 })
    await act(() => vi.advanceTimersByTimeAsync(RETRY_MS + 100))

    await waitFor(() => expect(wanted()).toEqual(OFF))
    expect(of(asked, UNSUBSCRIBE).length).toBe(tried + 1)
    expect(
      await screen.findByText(/has answered that it deleted/),
    ).toBeInTheDocument()
  },
)

test('notifications can be turned off while the server cannot be reached, and nothing is said to be deleted', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  const asked = serve({
    'GET /api/health': () => {
      throw new TypeError('Failed to fetch')
    },
  })
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Turn notifications off' }),
  )

  expect(device.subscription('mock')).toBeNull()
  const old = { endpoint: `${ADDRESS}-old`, mode: 'mock' }
  expect(wanted()).toEqual({ on: null, told: null, forget: [old] })
  // Only the server that has the address can delete it, and it was not asked.
  expect(
    await screen.findByText(
      /Beacon’s server for the invented scenario has not yet answered that it deleted an address of this device\./,
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText(/has answered that it deleted/)).toBeNull()
  expect(asked.filter((request) => request.key.includes('/api/push'))).toEqual(
    [],
  )
})

test('notifications can be turned off on a server that sends none', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  const off = 'This server sends no notifications.'
  const asked = serve(
    routes({
      [INFO]: info({
        enabled: false,
        text: off,
        public_key: null,
        about_text: null,
        notice: null,
      }),
    }),
  )
  render(<App />)
  expect(await screen.findByText(off)).toBeInTheDocument()

  await click(
    await screen.findByRole('button', { name: 'Turn notifications off' }),
  )

  // A server that sends none still deletes what an earlier one kept.
  await waitFor(() => expect(wanted()).toEqual(OFF))
  expect(of(asked, UNSUBSCRIBE).at(-1)?.body).toEqual({
    endpoint: `${ADDRESS}-old`,
  })
  expect(device.subscription('mock')).toBeNull()
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(screen.queryByRole('button', { name: /notifications on/i })).toBeNull()
})

test('turning off has the server delete the address it was told, when the browser no longer has it too', async () => {
  window.localStorage.setItem(KEY, turnedOn())
  // The browser dropped its subscription since. The server still has the address.
  browser({ permission: 'granted' })
  const asked = serve(routes())

  expect(await turnOff()).toBe('off')

  expect(wanted()).toEqual({ on: null, told: null, forget: [MOCK_SERVER.told] })
  expect(await syncPush('mock')).toBe(true)
  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS },
  ])
  expect(wanted()).toEqual(OFF)
})

test('a browser that will not let go of its subscription is said to hold it, and the server is still asked to delete', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  const asked = serve(routes())
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  device.letsGo = false

  await click(screen.getByRole('button', { name: 'Turn notifications off' }))

  // It says what can still come of that, and no less: a notification that was
  // already sent is the push service's to deliver.
  expect(
    await screen.findByText(
      /This browser has not let go of a subscription that is no longer wanted, or has not said whether it holds one\. A notification can be sent to this device through it only while Beacon’s server has this device’s address, and one that was already sent can still arrive\./,
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText(/nothing can arrive/)).toBeNull()
  expect(device.subscription('mock')).not.toBeNull()
  await waitFor(() => expect(wanted()).toEqual(OFF))
  expect(of(asked, UNSUBSCRIBE).at(-1)?.body).toEqual({ endpoint: ADDRESS })
  expect(pushSnapshot().held).toBe(true)
  // Worth trying again: the browser may let go the next time.
  expect(await syncPush('mock')).toBe(false)
  device.letsGo = true
  expect(await syncPush('mock')).toBe(true)
  expect(device.subscription('mock')).toBeNull()
  await waitFor(() => expect(screen.queryByText(/has not let go/)).toBeNull())
})

test('a subscription the browser holds that this device has no note of is let go of, and its server asked to delete it', async () => {
  // Storage was emptied, and the browser's subscription was not.
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  const asked = serve(routes())

  expect(await syncPush('mock')).toBe(true)

  expect(device.subscription('mock')).toBeNull()
  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: `${ADDRESS}-old` },
  ])
  expect(wanted()).toEqual(OFF)
})

test('a browser that does not allow notifications is not subscribed, and the server is sent nothing', async () => {
  await savePlace('Home', FIRE)
  const device = browser({ grants: 'denied' })
  const asked = serve(routes())
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  expect((await screen.findByRole('alert')).textContent).toContain(
    'Notifications from this site are blocked in this browser.',
  )
  expect(device.events).toEqual(['ask permission'])
  expect(device.registered).toEqual([])
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(wanted()).toBeNull()
  expect(
    screen.getByRole('button', { name: 'Turn notifications on' }),
  ).toBeEnabled()
})

test('notifications that were on and are blocked in the browser since are said to be blocked, and the server is asked to delete the address', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn())
  // The browser took its leave back, and its subscription with it.
  const device = browser({ permission: 'denied' })
  const asked = serve(routes())

  render(<App />)

  expect(
    await screen.findByText(
      /Notifications are on for this device\. Notifications from this site are blocked/,
    ),
  ).toBeInTheDocument()
  await waitFor(() => expect(of(asked, UNSUBSCRIBE)).toHaveLength(1))
  expect(of(asked, UNSUBSCRIBE)[0].body).toEqual({ endpoint: ADDRESS })
  await waitFor(() =>
    expect(wanted()).toEqual({ on: 'mock', told: null, forget: [] }),
  )
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(device.subscribe).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: /preview/ })).toBeNull()
  expect(
    screen.getByRole('button', { name: 'Turn notifications off' }),
  ).toBeEnabled()
})

test('a copy of the app that cannot start the service worker says so and subscribes nothing', async () => {
  await savePlace('Home', FIRE)
  const device = browser({ worker: false })
  const asked = serve(routes())
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  expect((await screen.findByRole('alert')).textContent).toContain(
    'could not start the service worker',
  )
  expect(device.subscribe).not.toHaveBeenCalled()
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(wanted()).toBeNull()
})

test('a subscription made for another key is let go of once its address is noted for deletion, and one is made for this server with its words', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: Uint8Array.from(KEY_BYTES, (b) => b ^ 1) },
  })
  let answers: () => Response = busy
  const asked = serve(routes({ [UNSUBSCRIBE]: () => answers() }))

  // The server is too busy to delete the old address: nothing is dropped.
  expect(await syncPush('mock')).toBe(false)

  const old = { endpoint: `${ADDRESS}-old`, mode: 'mock' }
  expect(wanted()).toEqual({ on: 'mock', told: null, forget: [old] })
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(pushSnapshot().problem).toBe('Beacon is busy. Try again.')

  answers = () => new Response(null, { status: 204 })
  expect(await syncPush('mock')).toBe(true)

  expect(of(asked, UNSUBSCRIBE).at(-1)?.body).toEqual({
    endpoint: `${ADDRESS}-old`,
  })
  expect(device.events.filter((event) => !event.startsWith('delete'))).toEqual([
    `unsubscribe ${ADDRESS}-old`,
    'keep the words mock',
    'subscribe mock',
  ])
  expect(of(asked, SUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS, places: [FIRE] },
  ])
  expect(wanted()).toEqual({ ...MOCK_SERVER, forget: [] })
  expect(device.words('mock')).toEqual(NOTICE)
})

test('turning notifications on for the invented scenario is not carried to real sources, and each server deletes only its own', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  // The server the app is shown now runs on real sources.
  const live = { ...MOCK_HEALTH, mode: 'live' }
  const asked = serve(
    routes({ 'GET /api/health': live, [INFO]: info({ mode: 'live' }) }),
  )

  render(<App />)

  // The person is asked again: nothing is on for real sources.
  expect(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  ).toBeEnabled()
  const old = { endpoint: `${ADDRESS}-old`, mode: 'mock' }
  await waitFor(() =>
    expect(wanted()).toEqual({ on: null, told: null, forget: [old] }),
  )
  // The invented scenario's subscription is let go of, with its words.
  expect(device.subscription('mock')).toBeNull()
  expect(device.words('mock')).toBeNull()
  // The live server is given nothing, and is not asked to delete an address
  // that is in the other server's file: its 204 would say nothing of it.
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(of(asked, UNSUBSCRIBE)).toEqual([])
  expect(
    await screen.findByText(
      /Beacon’s server for the invented scenario has not yet answered that it deleted an address of this device\. Only that server can/,
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText(/has answered that it deleted/)).toBeNull()

  // Shown the invented scenario's server again, the app has it delete.
  expect(await syncPush('mock')).toBe(true)
  const [sent] = of(asked, UNSUBSCRIBE)
  expect(sent.body).toEqual({ endpoint: `${ADDRESS}-old` })
  expect(sent.headers['beacon-mode']).toBe('mock')
  expect(wanted()).toEqual(OFF)
})

test('each mode has a service worker, a subscription, and words of its own', async () => {
  await savePlace('Home', FIRE)
  const device = browser({ permission: 'granted' })
  const liveNotice = { title: 'Beacon: crisis mode', text: 'Real words.' }
  window.localStorage.setItem(
    KEY,
    JSON.stringify({ on: 'live', told: null, forget: [] }),
  )
  const asked = serve(
    routes({ [INFO]: info({ mode: 'live', notice: liveNotice }) }),
  )

  expect(await syncPush('live')).toBe(true)

  expect(device.registered).toEqual([`${WORKER} ${scopeOf('live')}`])
  expect(device.events).toEqual(['keep the words live', 'subscribe live'])
  expect(device.words('live')).toEqual(liveNotice)
  expect(device.words('mock')).toBeNull()
  expect(device.subscription('mock')).toBeNull()
  expect(of(asked, SUBSCRIBE)[0].headers['beacon-mode']).toBe('live')
  expect((wanted() as { told: unknown }).told).toEqual({
    endpoint: ADDRESS,
    mode: 'live',
  })
})

test('a server in another mode than is shown is sent nothing, and the app asks what the server is', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  const device = browser({ permission: 'granted' })
  const asked = serve(routes({ [INFO]: info({ mode: 'live' }) }))

  render(<App />)

  // The health is asked for anew.
  await waitFor(() =>
    expect(of(asked, 'GET /api/health').length).toBeGreaterThan(1),
  )
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(device.events).toEqual([])
  await expect(syncPush('mock')).rejects.toBeInstanceOf(OtherModeError)
})

test('a server that says it is in another mode when asked to delete has deleted nothing', async () => {
  window.localStorage.setItem(
    KEY,
    JSON.stringify({ on: null, told: null, forget: [MOCK_SERVER.told] }),
  )
  browser({ permission: 'granted' })
  serve(
    routes({
      [UNSUBSCRIBE]: () =>
        Response.json({ detail: 'Another mode.' }, { status: 409 }),
    }),
  )

  await expect(syncPush('mock')).rejects.toBeInstanceOf(OtherModeError)

  expect(wanted()).toEqual({
    on: null,
    told: null,
    forget: [MOCK_SERVER.told],
  })
  expect(pushSnapshot().deleted).toBe(false)
})

test('a refusal by the server is shown in its own sentence and tried again', async () => {
  await savePlace('Home', FIRE)
  browser()
  const full = 'Beacon keeps as many addresses to notify as it can.'
  let refuse = true
  const asked = serve(
    routes({
      [SUBSCRIBE]: () =>
        refuse
          ? Response.json({ detail: full }, { status: 503 })
          : Response.json(SUBSCRIBED),
    }),
  )
  render(<App />)
  vi.useFakeTimers({ shouldAdvanceTime: true })

  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  expect(
    await screen.findByText(`${full} Beacon tries again while it is open.`),
  ).toBeInTheDocument()
  expect(screen.queryByText(/has this device’s address/)).toBeNull()

  refuse = false
  await act(() => vi.advanceTimersByTimeAsync(RETRY_MS + 100))

  expect(
    await screen.findByText(/where your saved place is\./),
  ).toBeInTheDocument()
  expect(of(asked, SUBSCRIBE)).toHaveLength(2)
})

test('a server that sends no notifications says so in its own words, and there is nothing to turn on', async () => {
  browser()
  const off = 'This server sends no notifications.'
  serve(
    routes({
      [INFO]: info({
        enabled: false,
        text: off,
        public_key: null,
        about_text: null,
        notice: null,
      }),
    }),
  )

  render(<App />)

  expect(await screen.findByText(off)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /notifications/i })).toBeNull()
})

test('a preview shows the words a notification would have, marked as a preview in its title and its text', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  serve(routes())
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  await click(
    await screen.findByRole('button', { name: 'Show a preview of one' }),
  )

  // Collapsed to its title, it still cannot be taken for the real thing.
  expect(device.shown).toEqual([
    {
      mode: 'mock',
      title: `Preview: ${NOTICE.title}`,
      options: {
        body: `A preview, not a notification. What one would say: ${NOTICE.text}`,
        tag: 'beacon-preview',
        icon: '/pwa-192x192.png',
      },
    },
  ])
})

test('a preview the browser will not show says so, and the controls come back', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  serve(routes())
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  device.showing.mockRejectedValue(
    new TypeError('No notification permission has been granted.'),
  )

  await click(
    await screen.findByRole('button', { name: 'Show a preview of one' }),
  )

  expect((await screen.findByRole('alert')).textContent).toContain(
    'This browser would not show a notification just now.',
  )
  expect(
    screen.getByRole<HTMLButtonElement>('button', {
      name: 'Turn notifications off',
    }).disabled,
  ).toBe(false)
})

test('a telling reads the places when its turn comes, so a place removed meanwhile is not sent', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  browser({ permission: 'granted' })
  const asked = serve(routes())
  // Another tab holds the lock, as it does while it tells the server.
  let release = () => {}
  const held = navigator.locks.request(
    'beacon.push',
    () => new Promise<void>((resolve) => (release = resolve)),
  )

  const telling = syncPush('mock')
  // While this telling waits its turn, the place is removed.
  const [{ id }] = JSON.parse(
    window.localStorage.getItem('beacon.places.v1')!,
  ) as { id: string }[]
  await removePlace(id)
  release()
  await held
  expect(await telling).toBe(true)

  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(JSON.stringify(asked)).not.toContain(String(FIRE.lat))
})

test('a place removed while the browser makes the subscription is not sent', async () => {
  await savePlace('Home', FIRE)
  await savePlace('Work', FAR)
  window.localStorage.setItem(KEY, turnedOn(null))
  const device = browser({ permission: 'granted' })
  const asked = serve(routes())
  const places = () =>
    JSON.parse(window.localStorage.getItem('beacon.places.v1')!) as {
      id: string
    }[]
  // The telling has read that there are places, and is waiting for the browser.
  device.whileSubscribing = async () => {
    await removePlace(places()[0].id)
  }

  expect(await syncPush('mock')).toBe(true)

  expect(of(asked, SUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS, places: [FAR] },
  ])
  expect(JSON.stringify(asked)).not.toContain(String(FIRE.lat))

  // And when the last place goes that way, the server is given none, and is
  // asked to delete the address it had.
  device.whileSubscribing = async () => {}
  await device.subscription('mock')!.unsubscribe()
  device.whileSubscribing = async () => {
    await removePlace(places()[0].id)
  }
  expect(await syncPush('mock')).toBe(true)
  expect(of(asked, SUBSCRIBE)).toHaveLength(1)
  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS },
    { endpoint: `${ADDRESS}-2` },
  ])
  expect(wanted()).toEqual({ on: 'mock', told: null, forget: [] })
})

// What an earlier server kept, on a server that sends none.

test('a server that sends none is asked to delete what an earlier one kept, so a place removed since does not stay in it', async () => {
  await savePlace('Home', FIRE)
  await savePlace('Work', FAR)
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  // A server that sent notifications has the address and both places. The one
  // that answers now sends none, and takes no change of places.
  let sends = false
  const off = info({
    enabled: false,
    text: 'This server sends no notifications.',
    public_key: null,
    about_text: null,
    notice: null,
  })
  const asked = serve(
    routes({ [INFO]: () => Response.json(sends ? info() : off) }),
  )

  expect(await syncPush('mock')).toBe(true)

  // Its copy cannot be made to match what is wanted, so it is deleted, which
  // such a server does. The choice and the browser's subscription stay.
  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: `${ADDRESS}-old` },
  ])
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(wanted()).toEqual({ on: 'mock', told: null, forget: [] })
  expect(pushSnapshot().told).toBe('server-off')
  expect(device.subscription('mock')?.endpoint).toBe(`${ADDRESS}-old`)

  // A place is removed meanwhile, and the server sends again without the app
  // having been closed: it is given the address with the places as they are.
  const [{ id }] = JSON.parse(
    window.localStorage.getItem('beacon.places.v1')!,
  ) as { id: string }[]
  await removePlace(id)
  expect(await syncPush('mock')).toBe(true)
  sends = true
  expect(await syncPush('mock')).toBe(true)

  expect(of(asked, SUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: `${ADDRESS}-old`, places: [FAR] },
  ])
  expect(JSON.stringify(asked)).not.toContain(String(FIRE.lat))
  expect(device.subscribe).not.toHaveBeenCalled()
})

// Turning off waits for nothing.

test('notifications are turned off at once while the server holds a request open, and the server is then asked to delete what that request gave it', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  let answer: (response: Response) => void = () => {}
  const asked = serve(
    routes({
      [SUBSCRIBE]: () =>
        new Promise<Response>((resolve) => {
          answer = resolve
        }),
    }),
  )
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))

  // The server has the request and does not answer it.
  await click(screen.getByRole('button', { name: 'Turn notifications off' }))

  // Off on the device, the browser's subscription let go of, and the address
  // noted for the server to delete, with the request still out.
  await waitFor(() => expect(device.subscription('mock')).toBeNull())
  expect(wanted()).toEqual({ on: null, told: null, forget: [MOCK_SERVER.told] })
  expect(
    await screen.findByText(
      /Beacon’s server has not yet answered that it deleted this device’s address\./,
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText(/Notifications are on for this device/)).toBeNull()
  expect(of(asked, UNSUBSCRIBE)).toEqual([])

  await act(async () => answer(Response.json(SUBSCRIBED)))

  // The telling that was waiting says nothing of an address the server has:
  // it has the server delete it.
  await waitFor(() => expect(wanted()).toEqual(OFF))
  expect(
    asked
      .filter((request) => request.key.includes('/api/push/'))
      .map((request) => [request.key, request.body]),
  ).toEqual([
    [SUBSCRIBE, { endpoint: ADDRESS, places: [FIRE] }],
    [UNSUBSCRIBE, { endpoint: ADDRESS }],
  ])
  expect(
    await screen.findByText(
      /has answered that it deleted this device’s address/,
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText(/has this device’s address and where/)).toBeNull()
})

test('a telling that was waiting for the server when notifications were turned off gives the server nothing after', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  const device = browser({ permission: 'granted' })
  let answer: (response: Response) => void = () => {}
  const asked = serve(
    routes({
      [INFO]: () =>
        new Promise<Response>((resolve) => {
          answer = resolve
        }),
    }),
  )

  const telling = syncPush('mock')
  await waitFor(() => expect(of(asked, INFO)).toHaveLength(1))
  expect(await turnOff()).toBe('off')
  expect(pushSnapshot().on).toBeNull()
  answer(Response.json(info()))
  expect(await telling).toBe(true)

  expect(device.registered).toEqual([])
  expect(device.subscribe).not.toHaveBeenCalled()
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(wanted()).toEqual(OFF)
})

test('a telling whose request gave the server the address as notifications were turned off has the server delete it before it ends', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  const device = browser({ permission: 'granted' })
  let answer: (response: Response) => void = () => {}
  const asked = serve(
    routes({
      [SUBSCRIBE]: () =>
        new Promise<Response>((resolve) => {
          answer = resolve
        }),
    }),
  )

  const telling = syncPush('mock')
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  expect(await turnOff()).toBe('off')
  expect(device.subscription('mock')).toBeNull()
  answer(Response.json(SUBSCRIBED))
  // No other telling is needed for it: this one does not end with the server
  // holding an address of a device that turned notifications off.
  expect(await telling).toBe(true)

  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS },
  ])
  expect(wanted()).toEqual(OFF)
  expect(pushSnapshot()).toMatchObject({ on: null, told: 'not-yet' })
})

test('notifications turned off while the browser makes a subscription: the server is never given it, and the browser lets go of it', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  const device = browser({ permission: 'granted' })
  const asked = serve(routes())
  let came: string | null = null
  device.whileSubscribing = async () => {
    came = await turnOff()
  }

  expect(await syncPush('mock')).toBe(true)

  expect(came).toBe('off')
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(device.events).toEqual([
    'keep the words mock',
    'subscribe mock',
    `unsubscribe ${ADDRESS}`,
    'delete the words mock',
  ])
  expect(device.subscription('mock')).toBeNull()
  // The server was never given it. It is asked all the same: the device does
  // not rest on that.
  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: ADDRESS },
  ])
  expect(wanted()).toEqual(OFF)
})

test('turning on that waited its turn while notifications were turned off turns nothing on', async () => {
  await savePlace('Home', FIRE)
  browser({ permission: 'granted' })
  const asked = serve(routes())
  // Another tab holds the lock, as it does while it tells the server.
  let release = () => {}
  const held = navigator.locks.request(
    'beacon.push',
    () => new Promise<void>((resolve) => (release = resolve)),
  )

  const on = turnOn('mock')
  await waitFor(() => expect(navigator.locks.request).toHaveBeenCalledTimes(2))
  expect(await turnOff()).toBe('off')
  release()
  await held

  expect(await on).toBe('overtaken')
  expect(pushSnapshot().on).toBeNull()
  expect(await syncPush('mock')).toBe(true)
  expect(of(asked, SUBSCRIBE)).toEqual([])
  // Pressed again after, it turns them on: an earlier turning off does not
  // hold for good.
  expect(await turnOn('mock')).toBe('on')
  expect(pushSnapshot().on).toBe('mock')
})

test('a server that does not answer ends a telling after so long, with its request aborted, and turning off does not wait for it', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, turnedOn(null))
  browser({ permission: 'granted' })
  const asked = serve(routes({ [INFO]: () => new Promise<Response>(() => {}) }))
  vi.useFakeTimers()

  const telling = syncPush('mock')
  await vi.advanceTimersByTimeAsync(SYNC_MS - 1000)
  expect(of(asked, INFO)).toHaveLength(1)
  expect(of(asked, INFO)[0].signal?.aborted).toBe(false)
  await vi.advanceTimersByTimeAsync(1000)

  expect(await telling).toBe(false)
  expect(of(asked, INFO)[0].signal?.aborted).toBe(true)
  expect(pushSnapshot()).toMatchObject({
    told: 'failed',
    problem:
      'Beacon’s server could not be told of the change to your notifications.',
  })
  expect(of(asked, SUBSCRIBE)).toEqual([])

  // And with another telling waiting on the same server, off is off at once.
  const next = syncPush('mock')
  await vi.advanceTimersByTimeAsync(1000)
  expect(await turnOff()).toBe('off')
  expect(pushSnapshot().on).toBeNull()
  await vi.advanceTimersByTimeAsync(SYNC_MS)
  await next
  expect(of(asked, SUBSCRIBE)).toEqual([])
})

// What is not known of the browser is not said to be nothing.

test('a browser that does not answer when asked to let go is not waited for, and is said to maybe hold a subscription', async () => {
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  serve(routes())
  device
    .subscription('mock')!
    .unsubscribe.mockImplementation(() => new Promise<boolean>(() => {}))
  vi.useFakeTimers()

  const off = turnOff()
  await vi.advanceTimersByTimeAsync(BROWSER_MS)

  expect(await off).toBe('held')
  const old = { endpoint: `${ADDRESS}-old`, mode: 'mock' }
  expect(wanted()).toEqual({ on: null, told: null, forget: [old] })
  expect(pushSnapshot()).toMatchObject({ on: null, held: true })
})

test('a browser that cannot say what it holds is not said to hold nothing, and is asked again', async () => {
  window.localStorage.setItem(KEY, turnedOn(`${ADDRESS}-old`))
  const device = browser({
    permission: 'granted',
    subscribed: { mode: 'mock', key: KEY_BYTES },
  })
  const asked = serve(routes())
  device.says = false

  expect(await turnOff()).toBe('held')

  expect(pushSnapshot()).toMatchObject({ on: null, held: true })
  // The server is asked to delete all the same, and the telling says there is
  // more to do.
  expect(await syncPush('mock')).toBe(false)
  expect(of(asked, UNSUBSCRIBE).map((request) => request.body)).toEqual([
    { endpoint: `${ADDRESS}-old` },
  ])
  expect(pushSnapshot().held).toBe(true)
  expect(device.subscription('mock')).not.toBeNull()

  device.says = true
  expect(await syncPush('mock')).toBe(true)
  expect(device.subscription('mock')).toBeNull()
  expect(pushSnapshot().held).toBe(false)
})

// No address is dropped to make room.

test('with as many addresses unanswered as are kept, none is dropped and the server is given no new one', async () => {
  await savePlace('Home', FIRE)
  // Addresses the server for real sources has yet to answer about, which the
  // invented scenario's server cannot delete. More than are kept as a rule are
  // all read.
  const waiting = Array.from({ length: FORGET_MOST + 2 }, (_, index) => ({
    endpoint: `${ADDRESS}-live-${index}`,
    mode: 'live',
  }))
  const keeping = (forget: unknown[]) =>
    window.localStorage.setItem(
      KEY,
      JSON.stringify({ on: 'mock', told: null, forget }),
    )
  keeping(waiting)
  const device = browser({ permission: 'granted' })
  const asked = serve(routes())

  expect(await syncPush('mock')).toBe(false)

  expect(wanted()).toEqual({ on: 'mock', told: null, forget: waiting })
  expect(device.subscribe).not.toHaveBeenCalled()
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(of(asked, UNSUBSCRIBE)).toEqual([])
  expect(pushSnapshot().problem).toMatch(
    /still waiting for Beacon’s servers to answer that they deleted earlier addresses/,
  )

  // At the most that are kept, too: the address would be one more.
  keeping(waiting.slice(0, FORGET_MOST))
  expect(await syncPush('mock')).toBe(false)
  expect(of(asked, SUBSCRIBE)).toEqual([])

  // With room for it, the server is given the address, and nothing is dropped.
  keeping(waiting.slice(0, FORGET_MOST - 1))
  expect(await syncPush('mock')).toBe(true)
  expect(of(asked, SUBSCRIBE)).toHaveLength(1)
  expect(wanted()).toEqual({
    ...MOCK_SERVER,
    forget: waiting.slice(0, FORGET_MOST - 1),
  })
})

test('every address a server is to delete is kept until that server answers, however many there are', async () => {
  // As many as are kept as a rule wait for the server for real sources, and
  // the invented scenario's server was given one more.
  const waiting = Array.from({ length: FORGET_MOST }, (_, index) => ({
    endpoint: `${ADDRESS}-live-${index}`,
    mode: 'live',
  }))
  window.localStorage.setItem(
    KEY,
    JSON.stringify({ ...MOCK_SERVER, forget: waiting }),
  )
  browser({ permission: 'granted' })
  let answers: () => Response = busy
  const asked = serve(routes({ [UNSUBSCRIBE]: () => answers() }))

  expect(await turnOff()).toBe('off')
  expect(await syncPush('mock')).toBe(false)

  // One more than are kept as a rule, and none was dropped for it.
  const all = [...waiting, MOCK_SERVER.told]
  expect(wanted()).toEqual({ on: null, told: null, forget: all })
  expect(pushSnapshot().forget).toHaveLength(FORGET_MOST + 1)

  // Each is asked about, by its own server, and leaves only on its answer.
  answers = () => new Response(null, { status: 204 })
  expect(await syncPush('mock')).toBe(true)
  expect(wanted()).toEqual({ on: null, told: null, forget: waiting })
  asked.length = 0
  serve(
    routes({
      'GET /api/health': { ...MOCK_HEALTH, mode: 'live' },
      [INFO]: info({ mode: 'live' }),
      [UNSUBSCRIBE]: () => {
        asked.push({ key: UNSUBSCRIBE, body: null, headers: {}, signal: null })
        return new Response(null, { status: 204 })
      },
    }),
  )
  expect(await syncPush('live')).toBe(true)
  expect(of(asked, UNSUBSCRIBE)).toHaveLength(FORGET_MOST)
  expect(wanted()).toEqual(OFF)
})

// The service worker, which is plain JavaScript the browser loads: it is run
// here against a worker's globals as they are used.

function workerScope(
  mode: Mode | 'elsewhere',
  keptWords: Partial<Record<Mode, string>> = {},
) {
  const handlers: Record<string, (event: unknown) => void> = {}
  const shown: { title: string; options: NotificationOptions }[] = []
  const opened: string[] = []
  const fetched: { url: string; init: RequestInit }[] = []
  const scope = {
    addEventListener: (name: string, handler: (event: unknown) => void) => {
      handlers[name] = handler
    },
    skipWaiting: vi.fn(),
    fetch: async (url: string, init: RequestInit) => {
      fetched.push({ url, init })
      return new Response(null, { status: 204 })
    },
    caches: {
      open: async (name: string) => ({
        match: async (url: string) => {
          const words = MODES.find(
            (each) =>
              name === NOTICE_CACHE &&
              url === NOTICE_URL + each &&
              keptWords[each] !== undefined,
          )
          return words === undefined
            ? undefined
            : new Response(keptWords[words])
        },
      }),
    },
    registration: {
      scope:
        mode === 'elsewhere'
          ? 'https://beacon.example/'
          : `https://beacon.example${scopeOf(mode)}`,
      showNotification: async (title: string, options: NotificationOptions) => {
        shown.push({ title, options })
      },
    },
    clients: {
      windows: [] as { navigate: unknown; focus: unknown }[],
      matchAll: async () => scope.clients.windows,
      openWindow: async (url: string) => {
        opened.push(url)
      },
    },
  }
  new Function('self', worker)(scope)
  const fire = async (name: string, more: object = {}) => {
    const waited: Promise<unknown>[] = []
    handlers[name]({
      waitUntil: (work: Promise<unknown>) => waited.push(work),
      ...more,
    })
    await Promise.all(waited)
  }
  return { scope, shown, opened, fetched, fire }
}
const MODES: Mode[] = ['mock', 'live']
const LIVE_NOTICE = { title: 'Beacon: crisis mode', text: 'Real words.' }
const BOTH = {
  mock: JSON.stringify(NOTICE),
  live: JSON.stringify(LIVE_NOTICE),
}

test('a push shows the words kept for the mode of the worker it woke, and reads nothing the push holds', async () => {
  const data = {
    text: () => {
      throw new Error('A push from Beacon holds nothing, and is not read.')
    },
  }
  // Both modes' words are on the device. Which are shown is the worker's scope's
  // to say, and nothing that is kept can make it the other's.
  for (const [mode, words] of [
    ['mock', NOTICE],
    ['live', LIVE_NOTICE],
  ] as const) {
    const { shown, fire } = workerScope(mode, BOTH)

    await fire('push', { data })

    expect(shown).toEqual([
      {
        title: words.title,
        options: {
          body: words.text,
          tag: `beacon-crisis-${mode}`,
          renotify: true,
          icon: '/pwa-192x192.png',
          badge: '/pwa-192x192.png',
        },
      },
    ])
  }
})

test('the invented scenario’s worker never shows the words of real sources, and says “invented” with no words at all', async () => {
  const lost =
    'Beacon sent this device a notification, and the words for it are no longer kept here. Open Beacon to see what it has.'
  // Only the other mode's words are kept, or none, or words that cannot be read.
  for (const kept of [
    { live: JSON.stringify(LIVE_NOTICE) },
    {},
    { mock: 'not json' },
    { mock: JSON.stringify({ title: 7 }) },
  ]) {
    const { shown, fire } = workerScope('mock', kept)

    await fire('push')

    expect(shown).toHaveLength(1)
    expect(shown[0].title).toBe('Beacon: invented scenario')
    expect(shown[0].options.body).toBe(
      `Invented scenario, not a real event. ${lost}`,
    )
  }

  // A real server's worker with no words says only that Beacon sent one, and
  // never shows the invented scenario's.
  for (const mode of ['live', 'elsewhere'] as const) {
    const { shown, fire } = workerScope(mode, { mock: JSON.stringify(NOTICE) })
    await fire('push')
    expect(shown).toHaveLength(1)
    expect(shown[0].title).toBe('Beacon')
    expect(shown[0].options.body).toBe(lost)
    expect(shown[0].options.body).not.toMatch(/crisis|incident|fire|invented/i)
  }
})

test('an address the browser replaced while the app was closed is given to its own server to delete', async () => {
  const { fetched, fire } = workerScope('mock')

  await fire('pushsubscriptionchange', {
    oldSubscription: { endpoint: ADDRESS },
    newSubscription: { endpoint: `${ADDRESS}-new` },
  })

  // The old address in the body, for the server of the worker's mode. The new
  // one is the app's to give, with where the places are.
  expect(fetched).toHaveLength(1)
  expect(fetched[0].url).toBe('/api/push/unsubscribe')
  expect(fetched[0].init.method).toBe('POST')
  expect(fetched[0].init.headers).toEqual({
    'Content-Type': 'application/json',
    'Beacon-Mode': 'mock',
  })
  expect(JSON.parse(fetched[0].init.body as string)).toEqual({
    endpoint: ADDRESS,
  })
  // With no old address there is nothing to ask.
  const none = workerScope('live')
  await none.fire('pushsubscriptionchange', { oldSubscription: null })
  expect(none.fetched).toEqual([])
})

test('pressing a notification opens the saved places, in the app’s window when there is one', async () => {
  const closed = { notification: { close: vi.fn() } }
  const none = workerScope('mock')
  await none.fire('notificationclick', closed)
  expect(none.opened).toEqual(['/#/places'])
  expect(closed.notification.close).toHaveBeenCalledOnce()

  const open = workerScope('mock')
  const window_ = {
    navigate: vi.fn(async () => undefined),
    focus: vi.fn(async () => undefined),
  }
  open.scope.clients.windows = [window_]
  await open.fire('notificationclick', closed)
  expect(window_.navigate).toHaveBeenCalledWith('/#/places')
  expect(window_.focus).toHaveBeenCalledOnce()
  expect(open.opened).toEqual([])
})

test('the service worker reads the words from where the app keeps them, at the scopes the app registers it at', () => {
  expect(worker).toContain(`const NOTICE_CACHE = '${NOTICE_CACHE}'`)
  expect(worker).toContain(`const NOTICE_URL = '${NOTICE_URL}'`)
  for (const mode of MODES)
    expect(worker).toContain(`SCOPE.endsWith('${scopeOf(mode)}')`)
})
