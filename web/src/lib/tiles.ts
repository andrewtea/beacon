// The street map behind an incident's map: square tiles in the Web Mercator
// grid every tile server uses, and where a position falls among them.
//
// The tiles are OpenStreetMap's own (`tile.openstreetmap.org`), which may be
// used without a key under its tile usage policy
// (https://operations.osmfoundation.org/policies/tiles/): the attribution is
// shown on the map, a browser sends its own Referer and keeps its own cache,
// and only the tiles a person is looking at are asked for. Nothing here loads
// a tile ahead of time or keeps one for offline use, which the policy forbids.
// The address is a setting, as the policy asks, so that a deployment can use
// another server, or none.

export type Tiles = {
  // A tile's address, with `{z}`, `{x}`, and `{y}` where its numbers go.
  url: string
  // The words the tiles' licence requires on the map, and their link.
  credit: { text: string; url: string }
}

export const OSM_TILES: Tiles = {
  url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  credit: {
    text: '© OpenStreetMap contributors',
    url: 'https://www.openstreetmap.org/copyright',
  },
}

/**
 * The tiles a build draws: OpenStreetMap's unless `VITE_MAP_TILES` names another
 * address, whose credit `VITE_MAP_TILES_CREDIT` and `VITE_MAP_TILES_CREDIT_URL`
 * then have to give. `VITE_MAP_TILES=none` draws no street map: what Beacon
 * places is then drawn on a plain ground. Another address without both parts of
 * its credit draws none either, since tiles are not shown without their credit.
 */
export function tilesFrom(
  env: Record<string, string | undefined>,
): Tiles | null {
  const url = env.VITE_MAP_TILES?.trim()
  if (url === undefined || url === '') return OSM_TILES
  const text = env.VITE_MAP_TILES_CREDIT?.trim()
  const credit = env.VITE_MAP_TILES_CREDIT_URL?.trim()
  if (url === 'none' || !text || !credit) return null
  if (!/^https:\/\//.test(url) || !/^https:\/\//.test(credit)) return null
  return { url, credit: { text, url: credit } }
}

export const TILES: Tiles | null = tilesFrom(import.meta.env)

// How far in a map may be: a few blocks, the neighbourhood, and a wider look.
export const ZOOMS = [14, 15, 16] as const
export const DEFAULT_ZOOM = 15
// A view is this many tiles across and down, with the incident in its middle.
export const SPAN = 3

/** Where a position is in the grid of tiles at a zoom: whole numbers are the
 * tiles' corners, x growing east and y growing south. */
export function gridOf(
  place: { lat: number; lon: number },
  zoom: number,
): { x: number; y: number } {
  const tiles = 2 ** zoom
  const lat = (place.lat * Math.PI) / 180
  return {
    x: ((place.lon + 180) / 360) * tiles,
    y: ((1 - Math.asinh(Math.tan(lat)) / Math.PI) / 2) * tiles,
  }
}

export type MapTile = {
  key: string
  src: string
  // Where its corner is in the view, as fractions of the view's width.
  left: number
  top: number
}

export type MapFrame = {
  zoom: number
  // The tiles that show in the view, at most sixteen.
  tiles: MapTile[]
  /** Where a position is in the view, as fractions of its width and height:
   * (0, 0) is its north-west corner, and the centre is (0.5, 0.5). */
  project: (place: { lat: number; lon: number }) => { x: number; y: number }
}

/** A view `SPAN` tiles across with `centre` in its middle. */
export function frameOf(
  centre: { lat: number; lon: number },
  zoom: number,
  tiles: Tiles | null,
): MapFrame {
  const middle = gridOf(centre, zoom)
  const left = middle.x - SPAN / 2
  const top = middle.y - SPAN / 2
  const most = 2 ** zoom
  const shown: MapTile[] = []
  if (tiles !== null)
    for (let y = Math.floor(top); y < top + SPAN; y += 1)
      for (let x = Math.floor(left); x < left + SPAN; x += 1) {
        if (x < 0 || y < 0 || x >= most || y >= most) continue
        shown.push({
          key: `${zoom}/${x}/${y}`,
          src: tiles.url
            .replace('{z}', String(zoom))
            .replace('{x}', String(x))
            .replace('{y}', String(y)),
          left: (x - left) / SPAN,
          top: (y - top) / SPAN,
        })
      }
  return {
    zoom,
    tiles: shown,
    project: (place) => {
      const at = gridOf(place, zoom)
      return { x: (at.x - left) / SPAN, y: (at.y - top) / SPAN }
    },
  }
}
