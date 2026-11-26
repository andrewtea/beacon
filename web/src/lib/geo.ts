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
