// Beacon's service worker for notifications. The app registers it once for each
// mode it turns notifications on for (`lib/push.ts`), at a scope of its own:
// `/push/mock/` for the invented scenario and `/push/live/` for real sources. No
// page is in either scope: this worker serves no request, and only shows a
// notification when a push arrives.
//
// A push from Beacon carries no content: nothing is sent through the push
// service but the fact of a message. The words shown are the server's, handed
// to the app before it subscribed and kept in a cache (`lib/push.ts` writes it
// under the same names).
//
// **Which words are shown is decided by which worker was woken, and by nothing
// that can change.** A browser's subscription belongs to one registration and
// takes a push only from the key it was made for, and the invented scenario's
// server and a real one have a key each. So a push from the invented scenario's
// server wakes the worker at `/push/mock/`, which reads only the words kept for
// `mock`, and says "invented" even when it has no words at all.

const SCOPE = self.registration.scope
const MODE = SCOPE.endsWith('/push/mock/')
  ? 'mock'
  : SCOPE.endsWith('/push/live/')
    ? 'live'
    : null
const NOTICE_CACHE = 'beacon-push-v2'
const NOTICE_URL = '/__beacon/push-notice/'
// Shown when the words are no longer kept on the device. It says nothing of an
// incident, because nothing is known here of what the push was about.
const NO_WORDS_TEXT =
  'Beacon sent this device a notification, and the words for it are no longer kept here. Open Beacon to see what it has.'
const NO_WORDS =
  MODE === 'mock'
    ? {
        title: 'Beacon: invented scenario',
        text: `Invented scenario, not a real event. ${NO_WORDS_TEXT}`,
      }
    : { title: 'Beacon', text: NO_WORDS_TEXT }
// Where a notification opens the app: the saved places, with what is near each.
const OPENS = '/#/places'

async function notice() {
  if (MODE === null) return NO_WORDS
  try {
    const cache = await self.caches.open(NOTICE_CACHE)
    const kept = await cache.match(NOTICE_URL + MODE)
    if (kept !== undefined) {
      const words = await kept.json()
      if (typeof words.title === 'string' && typeof words.text === 'string')
        return { title: words.title, text: words.text }
    }
  } catch {
    // A cache that cannot be read has no words.
  }
  return NO_WORDS
}

// No page is waiting on an earlier version of this worker.
self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('push', (event) => {
  // Whatever a push holds is not read: Beacon's hold nothing. A notification is
  // shown every time: a browser takes its leave back from a site whose push
  // shows none.
  event.waitUntil(
    notice().then((words) =>
      self.registration.showNotification(words.title, {
        body: words.text,
        // One notification from Beacon at a time for a mode. A push says nothing
        // of which incident it is about, so a second one cannot be told from a
        // repeat of the first: it takes the first one's place and alerts again,
        // so that a second incident is not missed. A push that was sent twice
        // therefore alerts twice.
        tag: `beacon-crisis-${MODE ?? 'unknown'}`,
        renotify: true,
        icon: '/pwa-192x192.png',
        badge: '/pwa-192x192.png',
      }),
    ),
  )
})

self.addEventListener('pushsubscriptionchange', (event) => {
  // The browser replaced or dropped this device's address while the app was
  // closed. Beacon's server is asked to delete the old one. The new one is given
  // to the server when the app is next opened, because where the saved places
  // are is the app's to read, and until then no notification arrives.
  const old = event.oldSubscription
  if (MODE === null || !old || typeof old.endpoint !== 'string') return
  event.waitUntil(
    self
      .fetch('/api/push/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Beacon-Mode': MODE },
        body: JSON.stringify({ endpoint: old.endpoint }),
      })
      // The app asks again when it is next opened: it kept the address.
      .catch(() => undefined),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      })
      for (const open of windows) {
        if (typeof open.navigate === 'function')
          await open.navigate(OPENS).catch(() => undefined)
        if (typeof open.focus === 'function') return open.focus()
      }
      return self.clients.openWindow(OPENS)
    })(),
  )
})
