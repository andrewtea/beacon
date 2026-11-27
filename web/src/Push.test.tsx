import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import worker from '../public/push-sw.js?raw'
import App from './App.tsx'
import type { PushInfo } from './api/push.ts'
import { removePlace, savePlace } from './lib/placeStore.ts'
import {
  NOTICE_CACHE,
  NOTICE_URL,
  OtherModeError,
  pushSnapshot,
  resetPush,
  RETRY_MS,
  syncPush,
} from './lib/push.ts'
import { type Asked, MOCK_HEALTH, serve } from './test/server.ts'

const KEY = 'beacon.push.v1'
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
  unsubscribe: ReturnType<typeof vi.fn>
}

/** The browser's side of web push, which jsdom has none of: a service worker's
 * registration with its push manager, the permission to notify, and the cache
 * the notification's words are kept in. `events` is what was done, in order. */
function browser({
  permission = 'default' as NotificationPermission,
  grants = 'granted' as NotificationPermission,
  registered = true,
  subscribedFor = null as Uint8Array | null,
} = {}) {
  const events: string[] = []
  const kept = new Map<string, string>()
  let subscription: FakeSubscription | null = null
  let made = 0
  const subscriptionFor = (key: Uint8Array, endpoint: string) => {
    const mine: FakeSubscription = {
      endpoint,
      options: { applicationServerKey: key.slice().buffer },
      unsubscribe: vi.fn(async () => {
        events.push(`unsubscribe ${endpoint}`)
        if (subscription === mine) subscription = null
        return true
      }),
    }
    return mine
  }
  if (subscribedFor !== null)
    subscription = subscriptionFor(subscribedFor, `${ADDRESS}-old`)
  const shown: { title: string; options: NotificationOptions }[] = []
  const registration = {
    pushManager: {
      getSubscription: vi.fn(async () => subscription),
      subscribe: vi.fn(
        async (options: {
          userVisibleOnly: boolean
          applicationServerKey: Uint8Array
        }) => {
          made += 1
          events.push('subscribe')
          subscription = subscriptionFor(
            options.applicationServerKey,
            made === 1 ? ADDRESS : `${ADDRESS}-${made}`,
          )
          return subscription
        },
      ),
    },
    showNotification: vi.fn(
      async (title: string, options: NotificationOptions) => {
        shown.push({ title, options })
      },
    ),
  }
  const notification = {
    permission,
    requestPermission: vi.fn(async () => {
      events.push('ask permission')
      notification.permission = grants
      return grants
    }),
  }
  vi.stubGlobal('Notification', notification)
  vi.stubGlobal('PushManager', class {})
  vi.stubGlobal('caches', {
    open: vi.fn(async (name: string) => ({
      put: async (url: string, response: Response) => {
        kept.set(`${name} ${url}`, await response.text())
        events.push('keep the words')
      },
      match: async (url: string) => {
        const body = kept.get(`${name} ${url}`)
        return body === undefined ? undefined : new Response(body)
      },
    })),
    delete: vi.fn(async (name: string) => {
      events.push('delete the words')
      for (const key of [...kept.keys()])
        if (key.startsWith(`${name} `)) kept.delete(key)
      return true
    }),
  })
  Object.defineProperty(window.navigator, 'serviceWorker', {
    value: {
      getRegistration: vi.fn(async () =>
        registered ? registration : undefined,
      ),
    },
    configurable: true,
  })
  return {
    events,
    kept,
    shown,
    registration,
    notification,
    get subscription() {
      return subscription
    },
  }
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
const wanted = (): unknown =>
  JSON.parse(window.localStorage.getItem(KEY) ?? 'null')

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
  // The browser is not asked for its leave until the person presses, and the
  // server is given nothing of this device.
  expect(device.events).toEqual([])
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(of(asked, UNSUBSCRIBE)).toEqual([])
  expect(wanted()).toBeNull()
})

test('turning notifications on keeps the server’s words, then sends the address and where the places are, and nothing else', async () => {
  await savePlace('Home', { lat: 37.77612345, lon: -122.42398765 })
  await savePlace('Work', FAR)
  const device = browser()
  const asked = serve(routes())
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
    'keep the words',
    'subscribe',
  ])
  const [subscribed] = device.registration.pushManager.subscribe.mock.calls
  expect(subscribed[0].userVisibleOnly).toBe(true)
  expect([...subscribed[0].applicationServerKey]).toEqual([...KEY_BYTES])
  expect(JSON.parse(device.kept.get(`${NOTICE_CACHE} ${NOTICE_URL}`)!)).toEqual(
    NOTICE,
  )
  const sent = of(asked, SUBSCRIBE)
  expect(sent).toHaveLength(1)
  // No name, no id, and each place rounded to about a block.
  expect(sent[0].body).toEqual({ endpoint: ADDRESS, places: [FIRE, FAR] })
  expect(sent[0].headers['beacon-mode']).toBe('mock')
  expect(wanted()).toEqual({ on: true, forget: [] })
  expect(JSON.stringify(asked)).not.toContain('Home')
})

test('the server is told again when a place is saved or removed, and keeps nothing once none is saved', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  const asked = serve(routes())
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))

  await act(async () => void (await savePlace('Work', FAR)))
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(2))
  expect(of(asked, SUBSCRIBE)[1].body).toEqual({
    endpoint: ADDRESS,
    places: [FIRE, FAR],
  })

  // Removed in another tab: storage says so, and the server is told.
  await act(async () => {
    window.localStorage.setItem('beacon.places.v1', '[]')
    window.dispatchEvent(
      new StorageEvent('storage', { key: 'beacon.places.v1' }),
    )
  })
  await waitFor(() => expect(of(asked, UNSUBSCRIBE)).toHaveLength(1))
  expect(of(asked, UNSUBSCRIBE)[0].body).toEqual({ endpoint: ADDRESS })
  expect(of(asked, SUBSCRIBE)).toHaveLength(2)
  expect(
    await screen.findByText(
      /With no place saved, Beacon’s server keeps nothing of this device/,
    ),
  ).toBeInTheDocument()
  // The browser's own subscription stays, for the next place.
  expect(device.subscription?.endpoint).toBe(ADDRESS)
  expect(pushSnapshot().wanted).toBe(true)
})

test('the app renews what the server keeps when it is opened, once', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, JSON.stringify({ on: true, forget: [] }))
  const device = browser({ permission: 'granted', subscribedFor: KEY_BYTES })
  const asked = serve(routes())

  render(<App />)

  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  expect(of(asked, SUBSCRIBE)[0].body).toEqual({
    endpoint: `${ADDRESS}-old`,
    places: [FIRE],
  })
  // The subscription the browser has is the one renewed: no other is made.
  expect(device.registration.pushManager.subscribe).not.toHaveBeenCalled()
  expect(device.notification.requestPermission).not.toHaveBeenCalled()
  // Nothing changes, so nothing more is sent.
  await act(async () => {
    window.dispatchEvent(new StorageEvent('storage', { key: KEY }))
  })
  await screen.findByText(/where your saved place is\./)
  expect(of(asked, SUBSCRIBE)).toHaveLength(1)
})

test('turning notifications off lets go of the address at once and has the server delete it', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  const asked = serve(routes())
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  device.events.length = 0

  await click(screen.getByRole('button', { name: 'Turn notifications off' }))

  await waitFor(() => expect(of(asked, UNSUBSCRIBE)).toHaveLength(1))
  expect(of(asked, UNSUBSCRIBE)[0].body).toEqual({ endpoint: ADDRESS })
  expect(device.events).toEqual([`unsubscribe ${ADDRESS}`, 'delete the words'])
  expect(device.subscription).toBeNull()
  expect(device.kept.size).toBe(0)
  await waitFor(() => expect(wanted()).toEqual({ on: false, forget: [] }))
  expect(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  ).toBeEnabled()
  // The places stay on the device: they are the person's, not the server's.
  expect(screen.getByRole('heading', { name: 'Home' })).toBeInTheDocument()
})

test('a server that could not be told to delete an address is told again until it hears', async () => {
  await savePlace('Home', FIRE)
  const device = browser()
  let reachable = true
  const asked = serve(
    routes({
      [UNSUBSCRIBE]: () => {
        if (!reachable) throw new TypeError('Failed to fetch')
        return new Response(null, { status: 204 })
      },
    }),
  )
  render(<App />)
  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )
  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  vi.useFakeTimers({ shouldAdvanceTime: true })
  reachable = false

  await click(screen.getByRole('button', { name: 'Turn notifications off' }))

  // Off on this device whatever the server heard.
  expect(device.subscription).toBeNull()
  expect(
    await screen.findByText(
      /Notifications are off on this device, and nothing can arrive\./,
    ),
  ).toBeInTheDocument()
  expect(wanted()).toEqual({ on: false, forget: [ADDRESS] })
  const tried = of(asked, UNSUBSCRIBE).length
  expect(tried).toBeGreaterThanOrEqual(1)

  reachable = true
  await act(() => vi.advanceTimersByTimeAsync(RETRY_MS + 100))

  await waitFor(() => expect(wanted()).toEqual({ on: false, forget: [] }))
  expect(of(asked, UNSUBSCRIBE).length).toBe(tried + 1)
  expect(of(asked, UNSUBSCRIBE).at(-1)?.body).toEqual({ endpoint: ADDRESS })
})

test('a browser that does not allow notifications is not subscribed, and the server is sent nothing', async () => {
  await savePlace('Home', FIRE)
  const device = browser({ grants: 'denied' })
  const asked = serve(routes())
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  expect(
    await screen.findByText(/Notifications from this site are blocked/),
  ).toBeInTheDocument()
  expect(device.registration.pushManager.subscribe).not.toHaveBeenCalled()
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(wanted()).toBeNull()
})

test('notifications that were on and are blocked in the browser since are said to be blocked, and the server is sent nothing', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, JSON.stringify({ on: true, forget: [] }))
  const device = browser({ permission: 'denied' })
  const asked = serve(routes())

  render(<App />)

  expect(
    await screen.findByText(/Notifications from this site are blocked/),
  ).toBeInTheDocument()
  await waitFor(() => expect(of(asked, CHECK)).toHaveLength(1))
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(device.events).toEqual([])
  // They can still be turned off.
  expect(
    screen.getByRole('button', { name: 'Turn notifications off' }),
  ).toBeEnabled()
  expect(screen.queryByRole('button', { name: /preview/ })).toBeNull()
})

test('a copy of the app with no service worker says so and subscribes nothing', async () => {
  await savePlace('Home', FIRE)
  browser({ registered: false })
  const asked = serve(routes())
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Turn notifications on' }),
  )

  expect(
    await screen.findByText(/This copy of Beacon has no service worker/),
  ).toBeInTheDocument()
  expect(of(asked, SUBSCRIBE)).toEqual([])
  expect(wanted()).toBeNull()
})

test('a subscription made for another server’s key is let go of, and one is made for this server with its words', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, JSON.stringify({ on: true, forget: [] }))
  // Subscribed to a real server before; this one is the invented scenario's.
  const other = KEY_BYTES.map((byte) => byte ^ 1)
  const device = browser({ permission: 'granted', subscribedFor: other })
  device.kept.set(
    `${NOTICE_CACHE} ${NOTICE_URL}`,
    JSON.stringify({ title: 'A real one', text: 'A real server’s words.' }),
  )
  const asked = serve(routes())

  render(<App />)

  await waitFor(() => expect(of(asked, SUBSCRIBE)).toHaveLength(1))
  expect(device.events).toEqual([
    `unsubscribe ${ADDRESS}-old`,
    'keep the words',
    'subscribe',
  ])
  expect(of(asked, SUBSCRIBE)[0].body).toEqual({
    endpoint: ADDRESS,
    places: [FIRE],
  })
  // The words kept are this server's now.
  expect(JSON.parse(device.kept.get(`${NOTICE_CACHE} ${NOTICE_URL}`)!)).toEqual(
    NOTICE,
  )
})

test('a server in another mode than is shown is sent nothing, and the app asks what the server is', async () => {
  await savePlace('Home', FIRE)
  window.localStorage.setItem(KEY, JSON.stringify({ on: true, forget: [] }))
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

test('a preview shows the words a notification would have, marked as a preview', async () => {
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

  expect(device.shown).toEqual([
    {
      title: NOTICE.title,
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
  device.registration.showNotification.mockRejectedValue(
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
  window.localStorage.setItem(KEY, JSON.stringify({ on: true, forget: [] }))
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

// The service worker's part, which is plain JavaScript the browser loads: it is
// run here against a worker's globals as they are used.

function workerScope(keptWords: string | null) {
  const handlers: Record<string, (event: unknown) => void> = {}
  const shown: { title: string; options: NotificationOptions }[] = []
  const opened: string[] = []
  const scope = {
    addEventListener: (name: string, handler: (event: unknown) => void) => {
      handlers[name] = handler
    },
    caches: {
      open: async (name: string) => ({
        match: async (url: string) =>
          keptWords !== null && name === NOTICE_CACHE && url === NOTICE_URL
            ? new Response(keptWords)
            : undefined,
      }),
    },
    registration: {
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
  return { scope, shown, opened, fire }
}

test('a push shows the words that were kept for it, and reads nothing the push holds', async () => {
  const { shown, fire } = workerScope(JSON.stringify(NOTICE))
  const data = {
    text: () => {
      throw new Error('A push from Beacon holds nothing, and is not read.')
    },
  }

  await fire('push', { data })

  expect(shown).toEqual([
    {
      title: NOTICE.title,
      options: {
        body: NOTICE.text,
        tag: 'beacon-crisis',
        renotify: true,
        icon: '/pwa-192x192.png',
        badge: '/pwa-192x192.png',
      },
    },
  ])
})

test('a push with no words kept says only that Beacon sent one, and nothing of an incident', async () => {
  for (const kept of [null, 'not json', JSON.stringify({ title: 7 })]) {
    const { shown, fire } = workerScope(kept)

    await fire('push')

    expect(shown).toHaveLength(1)
    expect(shown[0].title).toBe('Beacon')
    expect(shown[0].options.body).toBe(
      'Beacon sent this device a notification, and the words for it are no longer kept here. Open Beacon to see what it has.',
    )
    expect(shown[0].options.body).not.toMatch(/crisis|incident|fire/i)
  }
})

test('pressing a notification opens the saved places, in the app’s window when there is one', async () => {
  const closed = { notification: { close: vi.fn() } }
  const none = workerScope(null)
  await none.fire('notificationclick', closed)
  expect(none.opened).toEqual(['/#/places'])
  expect(closed.notification.close).toHaveBeenCalledOnce()

  const open = workerScope(null)
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

test('the service worker reads the words from where the app keeps them', () => {
  expect(worker).toContain(`const NOTICE_CACHE = '${NOTICE_CACHE}'`)
  expect(worker).toContain(`const NOTICE_URL = '${NOTICE_URL}'`)
})
