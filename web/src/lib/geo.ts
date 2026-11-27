// Where a tap comes from. The server stores a tap's place rounded to three
// decimals of a degree, about a block, and investigates from that. The app
// rounds the same way before it sends, so nothing finer leaves the phone.

export type Place = { lat: number; lon: number; accuracy_m?: number }

/** Why the phone's position could not be had, in a sentence a person can act on. */
export class LocationError extends Error {}

export function roundToBlock(value: number): number {
  return Math.round(value * 1000) / 1000
}

export function rounded(place: Place): Place {
  const accuracy = place.accuracy_m
  return {
    lat: roundToBlock(place.lat),
    lon: roundToBlock(place.lon),
    // Rounding moves a position by up to about 70 m, so no finer accuracy is
    // claimed for it than that.
    ...(accuracy === undefined || !Number.isFinite(accuracy)
      ? {}
      : { accuracy_m: Math.max(70, Math.round(accuracy)) }),
  }
}

// The area Beacon covers: San Francisco with Treasure Island, and a margin over
// the water. It is the server's box (`SAN_FRANCISCO` in
// `server/beacon/ingest/area.py`), which a test of the server's holds this to
// (`server/tests/test_places.py`). The server
// refuses a place outside it; the app looks first, so that a place it would
// refuse is never saved.
export const COVERED = {
  south: 37.7,
  west: -122.53,
  north: 37.84,
  east: -122.35,
}

/** Whether a place, as it is rounded before it is sent, is in the area Beacon
 * covers. */
export function covered(place: { lat: number; lon: number }): boolean {
  const lat = roundToBlock(place.lat)
  const lon = roundToBlock(place.lon)
  return (
    COVERED.south <= lat &&
    lat <= COVERED.north &&
    COVERED.west <= lon &&
    lon <= COVERED.east
  )
}

/** The phone's position now, already rounded to about a block. */
export function locate(): Promise<Place> {
  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator)) {
      reject(new LocationError('This browser cannot say where it is.'))
      return
    }
    navigator.geolocation.getCurrentPosition(
      (position) =>
        resolve(
          rounded({
            lat: position.coords.latitude,
            lon: position.coords.longitude,
            accuracy_m: position.coords.accuracy,
          }),
        ),
      (error) =>
        reject(
          new LocationError(
            error.code === error.PERMISSION_DENIED
              ? 'Beacon needs to know roughly where you are to look near you. Allow location for this site, then tap again.'
              : 'Beacon could not find where you are just now. Try again in a moment.',
          ),
        ),
      { enableHighAccuracy: true, timeout: 10_000, maximumAge: 30_000 },
    )
  })
}

// Places in the invented scenario, offered only while the server runs on it, so
// that somebody who is not in San Francisco can see an answer.
// They are also all a server takes that is reached through a host nobody has
// checked (`input: 'invented'`), which holds the same two (`beacon.front.SPOTS`).
export const MOCK_SPOTS: { name: string; place: Place }[] = [
  {
    name: 'Near the invented fire, Hayes Valley',
    place: { lat: 37.776, lon: -122.424 },
  },
  {
    name: 'Across town, North Beach',
    place: { lat: 37.8, lon: -122.41 },
  },
]

/** Whether a place is one of the invented scenario's two, to the digit. */
export function isMockSpot(place: { lat: number; lon: number }): boolean {
  return MOCK_SPOTS.some(
    (spot) => spot.place.lat === place.lat && spot.place.lon === place.lon,
  )
}
