import { roundToBlock } from './geo.ts'

// The places a person saved, such as home. They are kept here, on the device,
// with the names the person gave them, and nowhere else: Beacon's server has no
// account to keep them under and is given none. To check a place the app sends
// where it is, rounded to about a block and without its name, and the server
// keeps nothing of it (`api/places.ts`).

export type SavedPlace = {
  id: string
  // The person's own word for it. It never leaves the device.
  name: string
  // Rounded to about a block when it was saved.
  lat: number
  lon: number
}

const KEY = 'beacon.places.v1'
// The most the server checks in one request.
export const MOST_PLACES = 5
export const NAME_MOST = 40

type Listener = () => void
const listeners = new Set<Listener>()
let cache: { raw: string | null; places: SavedPlace[] } | null = null

function isSaved(value: unknown): value is SavedPlace {
  if (typeof value !== 'object' || value === null) return false
  const place = value as Record<string, unknown>
  return (
    typeof place.id === 'string' &&
    typeof place.name === 'string' &&
    typeof place.lat === 'number' &&
    Number.isFinite(place.lat) &&
    typeof place.lon === 'number' &&
    Number.isFinite(place.lon)
  )
}

function read(): SavedPlace[] {
  let raw: string | null = null
  try {
    raw = window.localStorage.getItem(KEY)
  } catch {
    // Storage that is switched off keeps nothing, and no place can be saved.
  }
  if (cache !== null && cache.raw === raw) return cache.places
  let places: SavedPlace[] = []
  try {
    const parsed: unknown = raw === null ? [] : JSON.parse(raw)
    places = Array.isArray(parsed)
      ? parsed.filter(isSaved).slice(0, MOST_PLACES)
      : []
  } catch {
    places = []
  }
  cache = { raw, places }
  return places
}

function notify(): void {
  for (const listener of listeners) listener()
}

/** Writes the list, and answers with whether storage took it. */
function write(places: SavedPlace[]): boolean {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(places))
  } catch {
    return false
  }
  cache = null
  notify()
  return true
}

function newId(): string {
  // Only to tell one saved place from another on this device.
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** A name as it is kept: trimmed, no longer than a name may be, and never empty. */
export function placeName(typed: string): string {
  const name = typed.trim().replace(/\s+/g, ' ').slice(0, NAME_MOST)
  return name === '' ? 'A saved place' : name
}

/**
 * Save a place under a name. Where it is is rounded to about a block before it
 * is kept. Answers with what came of it: `saved`, `full` when five are kept
 * already, or `failed` when the browser would not keep it.
 */
export function savePlace(
  typed: string,
  where: { lat: number; lon: number },
): 'saved' | 'full' | 'failed' {
  const places = read()
  if (places.length >= MOST_PLACES) return 'full'
  const place: SavedPlace = {
    id: newId(),
    name: placeName(typed),
    lat: roundToBlock(where.lat),
    lon: roundToBlock(where.lon),
  }
  return write([...places, place]) ? 'saved' : 'failed'
}

/** Remove a place from this device. Answers with whether storage took it. */
export function removePlace(id: string): boolean {
  return write(read().filter((place) => place.id !== id))
}

/** Another tab of the app saved or removed a place, or storage was cleared. */
function onStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== KEY) return
  cache = null
  notify()
}

/** Be told when the saved places change, in this tab or another. */
export function subscribePlaces(listener: Listener): () => void {
  if (listeners.size === 0) window.addEventListener('storage', onStorage)
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) window.removeEventListener('storage', onStorage)
  }
}

/** The saved places, for `useSyncExternalStore`: the same array while nothing
 * changed. */
export function placesSnapshot(): SavedPlace[] {
  return read()
}
