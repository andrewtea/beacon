import { useSyncExternalStore } from 'react'

// The app's views are named in the URL's fragment (`#/tap/<id>`), not its path.
// A tap's id is what its sender reads the answer with, and a fragment is never
// sent to a server: a path would put the id, beside the address it came from,
// in the access log of whatever serves the app. It also means the built app can
// be served from any static host without a fallback route.

export type Route =
  | { view: 'home' }
  | { view: 'tap'; id: string }
  | { view: 'trace'; id: string }
  | { view: 'incident'; id: string }
  | { view: 'map'; id: string }
  | { view: 'places' }
  | { view: 'results' }
  | { view: 'unknown' }

// Ids are the server's own, and anything else is not a route. A tap's is
// letters, digits, `-` and `_`. An incident's is `inc-` and sixteen hex digits,
// and only that is asked of the incident routes: a view's id goes into a
// request's path, which a server may log, and a tap's id put after
// `#/incident/` must never get there.
const TAP_ID = /^[A-Za-z0-9_-]{1,64}$/
const INCIDENT_ID = /^inc-[0-9a-f]{16}$/

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  if (parts.length === 0) return { view: 'home' }
  const [head, id, tail] = parts
  if (head === 'places' && parts.length === 1) return { view: 'places' }
  if (head === 'results' && parts.length === 1) return { view: 'results' }
  if (id === undefined) return { view: 'unknown' }
  if (head === 'tap' && TAP_ID.test(id)) {
    if (parts.length === 2) return { view: 'tap', id }
    if (tail === 'trace' && parts.length === 3) return { view: 'trace', id }
  }
  if (head === 'incident' && INCIDENT_ID.test(id)) {
    if (parts.length === 2) return { view: 'incident', id }
    if (tail === 'map' && parts.length === 3) return { view: 'map', id }
  }
  return { view: 'unknown' }
}

export const href = {
  home: '#/',
  tap: (id: string) => `#/tap/${id}`,
  trace: (id: string) => `#/tap/${id}/trace`,
  incident: (id: string) => `#/incident/${id}`,
  map: (id: string) => `#/incident/${id}/map`,
  places: '#/places',
  results: '#/results',
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

/** The route the URL's fragment names, kept current as it changes. */
export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  return parseRoute(hash)
}

export function navigate(to: string): void {
  window.location.hash = to
}
