import { fetchJson, MODE_HEADER } from './http.ts'
import type { IncidentSummary, Mode } from './incidents.ts'
import { roundToBlock } from '../lib/geo.ts'

// The shape of `POST /api/places/check`, field for field as
// `server/beacon/api/places.py` documents it.

/** An open incident Beacon has within a kilometre of a place. */
export type NearPlace = {
  // When `incident.point_source` is not null, the distance is measured from
  // that row's point, which is a 511 closure's: its confidence and its credit
  // are shown beside `text`.
  incident: IncidentSummary
  distance_m: number
  // The distance in the server's sentence, which says so when it is measured
  // from a point of 511's, and nothing else.
  text: string
}

/** What Beacon has near one place. The place itself is not repeated: an answer
 * belongs to the place at the same position in the request. */
export type PlaceAnswer = {
  near: NearPlace[]
  // The server's sentence when nothing is listed, which says it is no all-clear.
  text: string | null
}

export type PlacesAnswer = {
  mode: Mode
  as_of: string
  partial: boolean
  reach_m: number
  // What a distance is and is not, in the server's sentence. Shown with every
  // answer.
  caveat_text: string
  places: PlaceAnswer[]
}

/**
 * Asks what Beacon has near each place. Only where a place is goes, rounded to
 * about a block here whatever was kept: never its name. The places go in the
 * body and not in the address, and the server keeps nothing of them. `shown` is
 * the mode the app is showing, which a server in the other mode refuses unread.
 */
export function checkPlaces(
  places: { lat: number; lon: number }[],
  shown: Mode,
  signal?: AbortSignal,
): Promise<PlacesAnswer> {
  return fetchJson<PlacesAnswer>('/api/places/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', [MODE_HEADER]: shown },
    body: JSON.stringify({
      places: places.map((place) => ({
        lat: roundToBlock(place.lat),
        lon: roundToBlock(place.lon),
      })),
    }),
    signal,
  })
}
