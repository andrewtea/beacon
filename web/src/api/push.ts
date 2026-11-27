import { fetchJson, MODE_HEADER } from './http.ts'
import type { Mode } from './incidents.ts'
import { roundToBlock } from '../lib/geo.ts'

// The shapes of `GET /api/push`, `POST /api/push/subscribe`, and
// `POST /api/push/unsubscribe`, field for field as `server/beacon/api/push.py`
// documents them.

/** What a device shows when a push arrives, in the server's words. A push
 * carries no words, so the app keeps these for its service worker. */
export type PushNotice = { title: string; text: string }

export type PushInfo = {
  mode: Mode
  // Whether this server sends notifications. When it does not, `text` says so.
  enabled: boolean
  text: string | null
  // The key a browser subscribes with. The invented scenario's server and a
  // real one have a key each.
  public_key: string | null
  // What Beacon keeps to notify a device, when it notifies, and who learns
  // what, in the server's words. Shown before notifications are turned on.
  about_text: string | null
  notice: PushNotice | null
  reach_m: number
  max_places: number
  kept_days: number
}

export function fetchPushInfo(signal?: AbortSignal): Promise<PushInfo> {
  return fetchJson<PushInfo>('/api/push', { signal })
}

/**
 * Has the server keep, or renew, this device's subscription: the address the
 * browser's push service made for it, and where the saved places are, each
 * rounded to about a block and without its name. They take the place of any
 * the server had. `shown` is the mode the app is showing, which a server in the
 * other mode refuses unread.
 */
export function subscribePush(
  endpoint: string,
  places: { lat: number; lon: number }[],
  shown: Mode,
): Promise<{ mode: Mode; kept_until: string }> {
  return fetchJson('/api/push/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', [MODE_HEADER]: shown },
    body: JSON.stringify({
      endpoint,
      places: places.map((place) => ({
        lat: roundToBlock(place.lat),
        lon: roundToBlock(place.lon),
      })),
    }),
  })
}

/** Has the server delete the subscription kept under an address. The address
 * goes in the body, never in the request's own address. */
export async function unsubscribePush(endpoint: string): Promise<void> {
  let response: Response
  try {
    response = await fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint }),
    })
  } catch {
    throw new Error('The server could not be reached.')
  }
  // 503 from a server that sends no notifications: it keeps none to delete.
  if (!response.ok && response.status !== 503)
    throw new Error(`The server answered with HTTP ${response.status}.`)
}
