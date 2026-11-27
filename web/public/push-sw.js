// The part of Beacon's service worker that shows a notification. The generated
// service worker loads it (`workbox.importScripts` in vite.config.ts).
//
// A push from Beacon carries no content: nothing is sent through the push
// service but the fact of a message. The words shown are the server's, handed
// to the app when notifications were turned on and kept in a cache of their own
// (`lib/push.ts` writes it under the same two names). A subscription takes a
// push only from the server whose key it was made for, so the words kept are
// that server's: on the invented scenario they say that it is invented.

const NOTICE_CACHE = 'beacon-push-v1'
const NOTICE_URL = '/__beacon/push-notice'
// Shown when the words are no longer kept on the device. It says nothing of an
// incident, because nothing is known here of what the push was about.
const NO_WORDS = {
  title: 'Beacon',
  text: 'Beacon sent this device a notification, and the words for it are no longer kept here. Open Beacon to see what it has.',
}
// Where a notification opens the app: the saved places, with what is near each.
const OPENS = '/#/places'

async function notice() {
  try {
    const cache = await self.caches.open(NOTICE_CACHE)
    const kept = await cache.match(NOTICE_URL)
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

self.addEventListener('push', (event) => {
  // Whatever a push holds is not read: Beacon's hold nothing.
  event.waitUntil(
    notice().then((words) =>
      self.registration.showNotification(words.title, {
        body: words.text,
        // One notification from Beacon at a time: a later one takes the place
        // of an earlier one, and says so again.
        tag: 'beacon-crisis',
        renotify: true,
        icon: '/pwa-192x192.png',
        badge: '/pwa-192x192.png',
      }),
    ),
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
