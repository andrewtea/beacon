import { act, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App.tsx'
import type { Feature, MapAnswer, Source } from './api/incidents.ts'
import { MapCanvas } from './components/MapCanvas.tsx'
import { APART, coversView, DISC, marksOf, SIZE } from './lib/marks.ts'
import { parseRoute } from './lib/router.ts'
import {
  frameOf,
  gridOf,
  type MapFrame,
  OSM_TILES,
  tilesFrom,
} from './lib/tiles.ts'
import { fireIncident } from './test/fixtures.ts'
import { MOCK_HEALTH, serve } from './test/server.ts'

const fire = fireIncident.incident
const ID = fire.id
const MAP = `GET /api/incidents/${ID}/map`
const HAYES = { lat: 37.7766, lon: -122.4236 }

const source = (from: Partial<Source>): Source => ({
  kind: 'radio',
  source: 'openmhz',
  label: 'Fire and EMS dispatch radio',
  confidence: 'medium',
  at: '2026-11-26T23:24:25Z',
  evidence_id: 3,
  url: null,
  stand_in: true,
  model: 'fake',
  credit: null,
  ...from,
})
const CREDIT = { text: 'data provided by 511.org', url: 'https://511.org' }

// Sentences of the server's, which the view shows as they are given.
const PLACE =
  'The point Beacon’s list of blocks has for the block named on the radio. It marks the block, not an address.'
const EVENT = '511 lists: Hayes St closed between Gough St and Octavia St.'
const AIRCRAFT = 'A helicopter (MOCKH1) circled nearby for about 6 min.'
const ALERT = 'The Department issued an alert, Shelter In Place Warning.'
const CUT = 'Only its newest 240 positions are drawn.'

const feature = (from: Partial<Feature>): Feature => ({
  kind: 'place',
  text: PLACE,
  at: '2026-11-26T23:24:25Z',
  confidence: 'medium',
  sources: [source({})],
  point: null,
  path: [],
  polygons: [],
  note: null,
  ...from,
})
const features: Feature[] = [
  feature({ point: HAYES }),
  feature({
    kind: 'traffic_event',
    text: EVENT,
    confidence: 'official',
    sources: [
      source({
        kind: 'traffic',
        source: '511',
        label: '511 SF Bay traffic events',
        confidence: 'official',
        model: null,
        credit: CREDIT,
      }),
    ],
    // On the same block as the incident.
    point: HAYES,
  }),
  feature({
    kind: 'aircraft',
    text: AIRCRAFT,
    confidence: 'high',
    sources: [
      source({
        kind: 'aircraft',
        source: 'opensky',
        label: 'OpenSky Network aircraft positions',
        confidence: 'high',
        model: null,
      }),
    ],
    path: [
      { lat: 37.779, lon: -122.42, at: '2026-11-26T23:30:00Z' },
      { lat: 37.774, lon: -122.427, at: '2026-11-26T23:30:30Z' },
    ],
    note: CUT,
  }),
  feature({
    kind: 'alert_area',
    text: ALERT,
    confidence: 'official',
    sources: [
      source({
        kind: 'alert',
        source: 'nws',
        label: 'National Weather Service alert feed',
        confidence: 'official',
        model: null,
        url: 'https://alerts.example/shelter',
      }),
    ],
    polygons: [
      [
        [
          { lat: 37.774, lon: -122.427 },
          { lat: 37.774, lon: -122.42 },
          { lat: 37.779, lon: -122.42 },
          { lat: 37.779, lon: -122.427 },
          { lat: 37.774, lon: -122.427 },
        ],
      ],
    ],
  }),
]
const answer = (from: Partial<MapAnswer> = {}): MapAnswer => ({
  mode: 'mock',
  as_of: '2026-11-26T23:44:34Z',
  partial: false,
  incident: fire,
  features,
  text: null,
  ...from,
})

const tiles = () =>
  [...document.querySelectorAll<HTMLImageElement>('img.map-tile')].map(
    (tile) => tile.src,
  )
const click = (element: HTMLElement) => act(async () => element.click())

beforeEach(() => {
  window.localStorage.clear()
  window.location.hash = `#/incident/${ID}/map`
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('the map is a view of one incident, named in the fragment', () => {
  expect(parseRoute(`#/incident/${ID}/map`)).toEqual({ view: 'map', id: ID })
  expect(parseRoute(`#/incident/${ID}/map/more`)).toEqual({ view: 'unknown' })
  expect(parseRoute('#/map')).toEqual({ view: 'unknown' })
})

test('only an incident’s id names an incident’s page or its map, so a tap’s id is never asked of the incident routes', async () => {
  // What a tap's id looks like, and other things that are no incident's id.
  const TAP = 'q3J9xT-4bWm_Zk2A'
  for (const id of [
    TAP,
    'inc-0942f7593037801',
    'inc-0942f75930378016a',
    'inc-0942F75930378016',
    'INC-0942f75930378016',
    '0942f75930378016',
  ]) {
    expect(parseRoute(`#/incident/${id}`)).toEqual({ view: 'unknown' })
    expect(parseRoute(`#/incident/${id}/map`)).toEqual({ view: 'unknown' })
  }
  // A tap's own views take it as before.
  expect(parseRoute(`#/tap/${TAP}`)).toEqual({ view: 'tap', id: TAP })
  expect(parseRoute(`#/tap/${TAP}/trace`)).toEqual({ view: 'trace', id: TAP })

  for (const fragment of [`#/incident/${TAP}/map`, `#/incident/${TAP}`]) {
    window.location.hash = fragment
    const asked = serve({ 'GET /api/health': MOCK_HEALTH })
    const view = render(<App />)
    // The app is up, and has asked nothing that holds the id.
    await waitFor(() =>
      expect(asked.map((request) => request.key)).toContain('GET /api/health'),
    )
    expect(asked.filter((request) => request.key.includes(TAP))).toEqual([])
    expect(
      asked.filter((request) => request.key.includes('/api/incidents')),
    ).toEqual([])
    view.unmount()
  }
})

test('a position falls among the tiles where the tile grid has it', () => {
  // Worked out by hand from the grid's definition, for the 400 block of Hayes
  // St at zoom 15: x = (lon + 180) / 360 * 2^15, y by the Mercator latitude.
  const at = gridOf(HAYES, 15)
  expect(at.x).toBeCloseTo(5240.7319, 3)
  expect(at.y).toBeCloseTo(12665.3223, 3)

  const frame = frameOf(HAYES, 15, OSM_TILES)
  // Three tiles across with the incident in the middle: parts of four each way.
  expect(frame.tiles.map((tile) => tile.key)).toEqual(
    [12663, 12664, 12665, 12666].flatMap((y) =>
      [5239, 5240, 5241, 5242].map((x) => `15/${x}/${y}`),
    ),
  )
  expect(frame.tiles[5].src).toBe(
    'https://tile.openstreetmap.org/15/5240/12664.png',
  )
  expect(frame.project(HAYES)).toEqual({ x: 0.5, y: 0.5 })
  // North is up and east is right.
  const north = frame.project({ lat: HAYES.lat + 0.005, lon: HAYES.lon })
  const east = frame.project({ lat: HAYES.lat, lon: HAYES.lon + 0.005 })
  expect(north.x).toBeCloseTo(0.5)
  expect(north.y).toBeLessThan(0.4)
  expect(east.y).toBeCloseTo(0.5)
  expect(east.x).toBeGreaterThan(0.6)
  // The tile the incident is in is where its corner says it is.
  const own = frame.tiles.find((tile) => tile.key === '15/5240/12665')!
  expect(own.left).toBeCloseTo((5240 - (at.x - 1.5)) / 3)
  expect(own.top).toBeCloseTo((12665 - (at.y - 1.5)) / 3)
  // With no street map, no tile is asked for and positions fall the same.
  const plain = frameOf(HAYES, 15, null)
  expect(plain.tiles).toEqual([])
  expect(plain.project(HAYES)).toEqual({ x: 0.5, y: 0.5 })
})

test('the tiles are OpenStreetMap’s unless a build names others with their credit, and none without a credit', () => {
  expect(tilesFrom({})).toBe(OSM_TILES)
  expect(tilesFrom({ VITE_MAP_TILES: ' ' })).toBe(OSM_TILES)
  expect(OSM_TILES.credit).toEqual({
    text: '© OpenStreetMap contributors',
    url: 'https://www.openstreetmap.org/copyright',
  })
  expect(tilesFrom({ VITE_MAP_TILES: 'none' })).toBeNull()
  const other = 'https://tiles.example/{z}/{x}/{y}.png'
  expect(tilesFrom({ VITE_MAP_TILES: other })).toBeNull()
  expect(
    tilesFrom({ VITE_MAP_TILES: other, VITE_MAP_TILES_CREDIT: 'Example' }),
  ).toBeNull()
  expect(
    tilesFrom({
      VITE_MAP_TILES: other,
      VITE_MAP_TILES_CREDIT: 'Example',
      VITE_MAP_TILES_CREDIT_URL: 'https://example.org/terms',
    }),
  ).toEqual({
    url: other,
    credit: { text: 'Example', url: 'https://example.org/terms' },
  })
  expect(
    tilesFrom({
      VITE_MAP_TILES: 'http://tiles.example/{z}/{x}/{y}.png',
      VITE_MAP_TILES_CREDIT: 'Example',
      VITE_MAP_TILES_CREDIT_URL: 'https://example.org/terms',
    }),
  ).toBeNull()
})

test('the map is opened from an incident’s card, and only for an incident Beacon has a point for', async () => {
  window.location.hash = `#/incident/${ID}`
  let incident = fireIncident
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    [`GET /api/incidents/${ID}`]: () => Response.json(incident),
  })
  const view = render(<App />)

  expect(
    await screen.findByRole('link', { name: 'Open the map of this incident' }),
  ).toHaveAttribute('href', `#/incident/${ID}/map`)
  // Nothing of the map is asked for until it is opened.
  expect(asked.map((request) => request.key)).not.toContain(MAP)
  expect(tiles()).toEqual([])
  view.unmount()

  // No point, no map to open.
  incident = { ...fireIncident, incident: { ...fire, point: null } }
  const nowhere = render(<App />)
  expect(await screen.findByText(fire.title)).toBeInTheDocument()
  await waitFor(() =>
    expect(asked.filter((r) => r.key.endsWith(ID)).length).toBeGreaterThan(1),
  )
  expect(screen.queryByRole('link', { name: /Open the map/ })).toBeNull()
  nowhere.unmount()

  // A medical response has none either, though Beacon has its block.
  incident = {
    ...fireIncident,
    incident: { ...fire, category: 'medical response' },
  }
  render(<App />)
  await waitFor(() =>
    expect(asked.filter((r) => r.key.endsWith(ID)).length).toBeGreaterThan(2),
  )
  expect(await screen.findByText(/as its crews and its block/)).toBeVisible()
  expect(screen.queryByRole('link', { name: /Open the map/ })).toBeNull()
})

test('everything on the map is listed under its number with the server’s sentence, its confidence, and its sources', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH, [MAP]: answer() })

  render(<App />)

  const legend = (
    await screen.findByRole('heading', {
      name: 'What is on the map',
    })
  ).closest('section')!
  const items = within(legend)
    .getAllByRole('listitem')
    .filter((item) => item.classList.contains('line'))
  expect(items).toHaveLength(4)
  const [place, event, aircraft, alert] = items
  // Each under its number, which is the number on the picture.
  expect(
    items.map((item) => item.querySelector('.map-key')?.textContent),
  ).toEqual(['1', '2', '3', '4'])
  expect(
    [...document.querySelectorAll('.map-mark text')].map(
      (mark) => mark.textContent,
    ),
  ).toEqual(['1', '2', '3', '4'])

  expect(within(place).getByText(PLACE)).toBeInTheDocument()
  expect(within(place).getByText('Medium confidence')).toBeInTheDocument()
  expect(
    within(place).getByText('Fire and EMS dispatch radio'),
  ).toBeInTheDocument()
  // A 511 event is not called a closure by the app, and carries 511's credit.
  expect(
    within(event).getByText('Traffic event listed by 511'),
  ).toBeInTheDocument()
  expect(within(event).getByText(EVENT)).toBeInTheDocument()
  expect(within(event).getByText('Official source')).toBeInTheDocument()
  expect(
    within(event).getByRole('link', { name: CREDIT.text }),
  ).toHaveAttribute('href', CREDIT.url)
  expect(within(place).queryByText(CREDIT.text)).toBeNull()
  // What is not drawn of a thing is said in the server's sentence.
  expect(within(aircraft).getByText(AIRCRAFT)).toBeInTheDocument()
  expect(within(aircraft).getByText(CUT)).toBeInTheDocument()
  expect(within(aircraft).getByText('High confidence')).toBeInTheDocument()
  expect(within(alert).getByText(ALERT)).toBeInTheDocument()
  expect(
    within(alert).getByRole('link', { name: /Open the source/ }),
  ).toHaveAttribute('href', 'https://alerts.example/shelter')
  // Every row of the invented scenario is marked as that.
  for (const item of items)
    expect(within(item).getAllByText(/^Invented/).length).toBeGreaterThan(0)

  // The incident itself is its card, which opens its own page.
  expect(screen.getByRole('link', { name: fire.title })).toHaveAttribute(
    'href',
    `#/incident/${ID}`,
  )
})

test('the street map is OpenStreetMap’s tiles around the incident, with their credit on the picture', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH, [MAP]: answer() })

  render(<App />)

  const credit = await screen.findByRole('link', {
    name: '© OpenStreetMap contributors',
  })
  expect(credit).toHaveAttribute(
    'href',
    'https://www.openstreetmap.org/copyright',
  )
  expect(credit.closest('.map-canvas')).not.toBeNull()
  // 511's credit is on the picture too, beside the tiles': something of 511's
  // is drawn.
  expect(
    within(credit.closest('.map-canvas') as HTMLElement).getByRole('link', {
      name: CREDIT.text,
    }),
  ).toHaveAttribute('href', CREDIT.url)
  // Sixteen tiles at most, all around the incident.
  expect(tiles()).toHaveLength(16)
  expect(tiles()).toContain('https://tile.openstreetmap.org/15/5240/12665.png')
  expect(
    tiles().every((src) =>
      src.startsWith('https://tile.openstreetmap.org/15/'),
    ),
  ).toBe(true)
  // The tile server is told the site and never the page.
  expect(
    [...document.querySelectorAll('img.map-tile')].map((tile) =>
      tile.getAttribute('referrerpolicy'),
    ),
  ).toEqual(Array(16).fill('strict-origin-when-cross-origin'))
  // The streets are real and what is drawn is not: the picture says so itself,
  // and the words under it say which is which.
  expect(
    within(credit.closest('.map-canvas') as HTMLElement).getByText(
      'Invented scenario',
    ),
  ).toBeInTheDocument()
  expect(
    screen.getByText(
      /^The streets are real\. What is drawn on them is invented\./,
    ),
  ).toBeInTheDocument()

  // Closer in, and back: other tiles, around the same place.
  await click(screen.getByRole('button', { name: 'Closer in' }))
  expect(tiles()).toContain('https://tile.openstreetmap.org/16/10481/25330.png')
  expect(tiles().length).toBeLessThanOrEqual(16)
  expect(screen.getByRole('button', { name: 'Closer in' })).toBeDisabled()
  await click(screen.getByRole('button', { name: 'Wider area' }))
  await click(screen.getByRole('button', { name: 'Wider area' }))
  expect(tiles()).toContain('https://tile.openstreetmap.org/14/2620/6332.png')
  expect(screen.getByRole('button', { name: 'Wider area' })).toBeDisabled()
})

test('a real incident’s map does not say it is invented', async () => {
  serve({
    'GET /api/health': { ...MOCK_HEALTH, mode: 'live' },
    [MAP]: answer({ mode: 'live' }),
  })

  render(<App />)

  expect(await screen.findByText(PLACE)).toBeInTheDocument()
  expect(screen.queryByText(/Invented scenario/)).toBeNull()
  expect(screen.queryByText(/What is drawn on them is invented/)).toBeNull()
})

const canvas = (drawn: Feature[], invented = false) =>
  render(
    <MapCanvas
      centre={HAYES}
      features={drawn}
      tiles={null}
      invented={invented}
    />,
  )
const numbers = () =>
  [...document.querySelectorAll('.map-mark')].map((mark) => {
    const [x, y] = /translate\(([\d.]+) ([\d.]+)\)/
      .exec(mark.getAttribute('transform') ?? '')!
      .slice(1)
      .map(Number)
    return { x, y }
  })
const spotsOf = (selector: string, x: string, y: string) =>
  [...document.querySelectorAll(selector)].map((found) => ({
    x: Number(found.getAttribute(x)),
    y: Number(found.getAttribute(y)),
  }))
// Where the frame puts a position, in the picture's own units.
const projected = (place: { lat: number; lon: number }) => {
  const { x, y } = frameOf(HAYES, 15, null).project(place)
  return { x: Number((x * SIZE).toFixed(2)), y: Number((y * SIZE).toFixed(2)) }
}

test('with no street map, what Beacon places is drawn on a plain ground, no tile is asked for, and 511’s credit is still on the picture', () => {
  canvas(features, true)

  expect(tiles()).toEqual([])
  expect(document.querySelectorAll('.map-mark')).toHaveLength(4)
  // No tiles, so no tiles' credit; and 511's, because its event is drawn.
  const picture = document.querySelector('.map-canvas') as HTMLElement
  const links = within(picture).getAllByRole('link')
  expect(links.map((link) => link.textContent)).toEqual([CREDIT.text])
  expect(links[0]).toHaveAttribute('href', CREDIT.url)
  // The invented scenario is called that whatever it is drawn on, and is not
  // said to be on a street map that is not there.
  expect(within(picture).getByText('Invented scenario')).toBeInTheDocument()
  expect(within(picture).queryByText(/street map/)).toBeNull()
})

test('a picture with nothing of a credited source on it shows no credit', () => {
  canvas([features[0], features[2], features[3]])

  expect(screen.queryByRole('link')).toBeNull()
  expect(document.querySelector('.map-credits')).toBeNull()
})

test('a place whose point is 511’s puts 511’s credit on the picture', () => {
  canvas([
    feature({
      point: HAYES,
      confidence: 'official',
      sources: [
        source({ kind: 'traffic', source: '511', model: null, credit: CREDIT }),
      ],
    }),
  ])

  expect(screen.getByRole('link', { name: CREDIT.text })).toHaveAttribute(
    'href',
    CREDIT.url,
  )
})

test('a thing’s dot is at its own point and only its number is moved, joined to the dot by a line', () => {
  canvas(features)

  const spots = numbers()
  const dots = spotsOf('.map-dot', 'cx', 'cy')
  // The picture is 300 across, and the incident's own point is its middle.
  // The 511 event is on the same point: both dots are there, and neither moved.
  expect(dots.slice(0, 2)).toEqual([
    { x: 150, y: 150 },
    { x: 150, y: 150 },
  ])
  // The aircraft's dot is the newest position it was seen at. An area has no
  // dot: its number names an outline and no point.
  expect(dots[2]).toEqual(projected(features[2].path[1]))
  expect(dots).toHaveLength(3)
  // No number is on a dot, and no two numbers cover one another.
  for (const [index, spot] of spots.entries()) {
    for (const dot of dots)
      expect(Math.hypot(spot.x - dot.x, spot.y - dot.y)).toBeGreaterThan(DISC)
    for (const other of spots.slice(index + 1))
      expect(Math.hypot(spot.x - other.x, spot.y - other.y)).toBeGreaterThan(
        2 * DISC,
      )
  }
  // Each number of a thing with a point is joined to that thing's dot.
  expect(spotsOf('.map-leader', 'x1', 'y1').slice(0, 3)).toEqual(dots)
  expect(spotsOf('.map-leader', 'x2', 'y2').slice(0, 3)).toEqual(
    spots.slice(0, 3),
  )
  // An area is drawn with its outline closed, and an aircraft as a line
  // through where it was seen.
  expect(
    document.querySelector('.map-area-outline')?.getAttribute('d'),
  ).toMatch(/^M[\d. ]+( L[\d. ]+){4} Z$/)
  expect(
    document.querySelector('.map-path')?.getAttribute('points')?.split(' '),
  ).toHaveLength(2)
})

test('things at one point near the edge keep their dots there, and their numbers stay whole inside the picture', () => {
  // Three 511 events at one point, in the north-east corner of the view, each
  // about two units from the edges.
  const corner = { lat: HAYES.lat + 0.01289, lon: HAYES.lon + 0.01626 }
  const event = feature({ ...features[1], point: corner })
  canvas([features[0], event, event, event])

  const there = projected(corner)
  expect(there.x).toBeGreaterThan(SIZE - 4)
  expect(there.x).toBeLessThan(SIZE)
  expect(there.y).toBeGreaterThan(0)
  expect(there.y).toBeLessThan(4)
  const dots = spotsOf('.map-dot', 'cx', 'cy')
  expect(dots).toEqual([{ x: 150, y: 150 }, there, there, there])
  const spots = numbers()
  expect(spots).toHaveLength(4)
  for (const [index, spot] of spots.entries()) {
    // Whole inside the picture: the disc, not only its middle.
    for (const value of [spot.x, spot.y]) {
      expect(value).toBeGreaterThanOrEqual(DISC)
      expect(value).toBeLessThanOrEqual(SIZE - DISC)
    }
    for (const other of spots.slice(index + 1))
      expect(Math.hypot(spot.x - other.x, spot.y - other.y)).toBeGreaterThan(
        2 * DISC,
      )
  }
  expect(spotsOf('.map-leader', 'x1', 'y1')).toEqual(dots)
  expect(spotsOf('.map-leader', 'x2', 'y2')).toEqual(spots)
})

// A frame whose positions are the picture's own units: `lon` across and `lat`
// down, from 0 to 300.
const UNITS: MapFrame = {
  zoom: 15,
  tiles: [],
  project: (place) => ({ x: place.lon / SIZE, y: place.lat / SIZE }),
}
const ring = (...corners: [number, number][]) =>
  [...corners, corners[0]].map(([x, y]) => ({ lat: y, lon: x }))
const area = (...polygons: { lat: number; lon: number }[][][]) =>
  feature({ kind: 'alert_area', polygons })
// How far a spot is from the nearest stretch of a ring.
const offOutline = (
  spot: { x: number; y: number },
  rings: { lat: number; lon: number }[][],
) =>
  Math.min(
    ...rings.flatMap((corners) =>
      corners.slice(1).map((b, index) => {
        const a = corners[index]
        const [dx, dy] = [b.lon - a.lon, b.lat - a.lat]
        const along = Math.max(
          0,
          Math.min(
            1,
            ((spot.x - a.lon) * dx + (spot.y - a.lat) * dy) /
              (dx * dx + dy * dy),
          ),
        )
        return Math.hypot(
          spot.x - (a.lon + along * dx),
          spot.y - (a.lat + along * dy),
        )
      }),
    ),
  )
const DIAMOND = ring([150, 70], [230, 150], [150, 230], [70, 150])

test('an area’s number is about a point of its own outline, and stands on it or is joined to it', () => {
  // A diamond: the corner of the box around it is on neither its outline nor
  // its fill, and no number is put there.
  const [alone] = marksOf(UNITS, [area([DIAMOND])])
  expect(alone).not.toBeNull()
  expect(alone!.dot).toBe(false)
  expect(offOutline(alone!.anchor, [DIAMOND])).toBeLessThan(1e-9)
  expect(alone!.number).toEqual(alone!.anchor)
  expect(alone!.number).not.toEqual({ x: 70, y: 70 })

  // Two areas with one outline: the second number gives way, and is joined to
  // the outline it is about.
  const [first, second] = marksOf(UNITS, [area([DIAMOND]), area([DIAMOND])])
  expect(second!.anchor).toEqual(first!.anchor)
  expect(offOutline(second!.anchor, [DIAMOND])).toBeLessThan(1e-9)
  expect(
    Math.hypot(
      second!.number.x - first!.number.x,
      second!.number.y - first!.number.y,
    ),
  ).toBeGreaterThanOrEqual(APART)

  // A hole's edge is outline too: with the outer edge out of view, the number
  // is on the hole's.
  const far = ring([-500, -500], [800, -500], [800, 800], [-500, 800])
  const hole = ring([100, 100], [200, 100], [200, 200], [100, 200])
  const [holed] = marksOf(UNITS, [area([far, hole])])
  expect(offOutline(holed!.anchor, [hole])).toBeLessThan(1e-9)
  expect(coversView(UNITS, area([far, hole]))).toBe(false)

  // An outline that is in view only at the picture's very edge: the anchor is
  // on it, and the number stands whole inside, joined to it.
  const sliver = ring([-50, 4], [350, 4], [350, -50], [-50, -50])
  const [edge] = marksOf(UNITS, [area([sliver])])
  expect(edge!.anchor.y).toBeCloseTo(4)
  expect(edge!.number.y).toBeGreaterThanOrEqual(DISC)
  expect(edge!.number).not.toEqual(edge!.anchor)
})

test('a number that is not on its area’s outline is drawn with a line to it, and one that is on it has none', () => {
  // A diamond around the incident, in positions.
  const diamond = [
    { lat: HAYES.lat + 0.004, lon: HAYES.lon },
    { lat: HAYES.lat, lon: HAYES.lon + 0.005 },
    { lat: HAYES.lat - 0.004, lon: HAYES.lon },
    { lat: HAYES.lat, lon: HAYES.lon - 0.005 },
    { lat: HAYES.lat + 0.004, lon: HAYES.lon },
  ]
  const one = feature({ kind: 'alert_area', polygons: [[diamond]] })
  const drawn = diamond.map(projected).map(({ x, y }) => ({ lat: y, lon: x }))
  const view = canvas([one])
  expect(numbers()).toHaveLength(1)
  expect(offOutline(numbers()[0], [drawn])).toBeLessThan(0.02)
  expect(document.querySelectorAll('.map-leader')).toHaveLength(0)
  expect(document.querySelectorAll('.map-dot')).toHaveLength(0)
  view.unmount()

  canvas([one, one])
  const [first, second] = numbers()
  expect(offOutline(first, [drawn])).toBeLessThan(0.02)
  expect(offOutline(second, [drawn])).toBeGreaterThan(DISC)
  // The line starts on the outline and ends at the number.
  const [start] = spotsOf('.map-leader', 'x1', 'y1')
  expect(spotsOf('.map-leader', 'x1', 'y1')).toHaveLength(1)
  expect(offOutline(start, [drawn])).toBeLessThan(0.02)
  expect(spotsOf('.map-leader', 'x2', 'y2')).toEqual([second])
  expect(document.querySelectorAll('.map-dot')).toHaveLength(0)
})

test('an area that holds the whole picture has no number on it, and the picture says which area that is', () => {
  const far = ring([-500, -500], [800, -500], [800, 800], [-500, 800])
  expect(coversView(UNITS, area([far]))).toBe(true)
  expect(marksOf(UNITS, [area([far])])).toEqual([null])
  // The picture is in a hole of it, or away from it: it does not hold it.
  const hole = ring([-10, -10], [310, -10], [310, 310], [-10, 310])
  expect(coversView(UNITS, area([far, hole]))).toBe(false)
  expect(marksOf(UNITS, [area([far, hole])])).toEqual([null])
  const away = ring([400, 400], [500, 400], [500, 500], [400, 500])
  expect(coversView(UNITS, area([away]))).toBe(false)
  // In the hole of one polygon and inside another: held.
  const other = ring([-20, -20], [320, -20], [320, 320], [-20, 320])
  expect(coversView(UNITS, area([far, hole], [other]))).toBe(true)
  expect(coversView(UNITS, area([DIAMOND]))).toBe(false)
  expect(coversView(UNITS, features[0])).toBe(false)

  // On the picture: a county around the incident, listed second.
  const county = feature({
    kind: 'alert_area',
    text: ALERT,
    polygons: [
      [
        [
          { lat: 37.6, lon: -122.6 },
          { lat: 37.6, lon: -122.3 },
          { lat: 37.9, lon: -122.3 },
          { lat: 37.9, lon: -122.6 },
          { lat: 37.6, lon: -122.6 },
        ],
      ],
    ],
  })
  canvas([features[0], county])
  expect(
    [...document.querySelectorAll('.map-mark text')].map(
      (mark) => mark.textContent,
    ),
  ).toEqual(['1'])
  expect(
    screen.getByText(
      'The area numbered 2 in the list below holds all of this picture: no part of its outline is in view, so it has no number on the picture.',
    ),
  ).toBeInTheDocument()
  // It is filled all the same.
  expect(document.querySelectorAll('.map-area-fill rect')).toHaveLength(1)
})

test('an area whose outline is in view is not said to hold the picture', () => {
  canvas(features)
  expect(screen.queryByText(/holds all of this picture/)).toBeNull()
})

test('each polygon of an area is filled with its own holes taken out, and a second polygon is no hole in the first', () => {
  const outer = features[3].polygons[0][0]
  const inner = [
    { lat: 37.775, lon: -122.426 },
    { lat: 37.775, lon: -122.425 },
    { lat: 37.776, lon: -122.425 },
    { lat: 37.775, lon: -122.426 },
  ]
  const masks = () =>
    [...document.querySelectorAll('.map-area mask')].map((mask) => ({
      id: mask.id,
      inside: mask.querySelectorAll('.map-area-in').length,
      out: mask.querySelectorAll('.map-area-out').length,
    }))
  const fills = () =>
    [...document.querySelectorAll('.map-area-fill rect')].map((fill) =>
      fill.getAttribute('mask'),
    )

  // The smaller ring as a hole of the larger.
  const holed = canvas([
    feature({ kind: 'alert_area', polygons: [[outer, inner]] }),
  ])
  expect(masks().map(({ inside, out }) => [inside, out])).toEqual([[1, 1]])
  expect(fills()).toEqual(masks().map(({ id }) => `url(#${id})`))
  expect(document.querySelectorAll('.map-area-outline')).toHaveLength(2)
  // No path holds two rings, to be filled by parity.
  for (const path of document.querySelectorAll('.map-area path'))
    expect(path.getAttribute('d')?.match(/M/g)).toHaveLength(1)
  holed.unmount()

  // The same two rings as two polygons: each is filled, and nothing is taken
  // out of either.
  canvas([feature({ kind: 'alert_area', polygons: [[outer], [inner]] })])
  expect(masks().map(({ inside, out }) => [inside, out])).toEqual([
    [1, 0],
    [1, 0],
  ])
  expect(fills()).toEqual(masks().map(({ id }) => `url(#${id})`))
  expect(new Set(fills()).size).toBe(2)
  for (const { id } of masks()) expect(id).toMatch(/^[A-Za-z][A-Za-z0-9_-]*$/)
})

test('many things at one point in a corner each get a place of their own, wherever in the picture there is one', () => {
  const corner = { lat: 2, lon: 298 }
  const crowd = Array.from({ length: 15 }, () => feature({ point: corner }))

  const marks = marksOf(UNITS, crowd)

  expect(marks.every((mark) => mark !== null)).toBe(true)
  for (const [index, mark] of marks.entries()) {
    expect(mark!.anchor).toEqual({ x: 298, y: 2 })
    for (const value of [mark!.number.x, mark!.number.y]) {
      expect(value).toBeGreaterThanOrEqual(DISC)
      expect(value).toBeLessThanOrEqual(SIZE - DISC)
    }
    for (const other of marks.slice(index + 1))
      expect(
        Math.hypot(
          mark!.number.x - other!.number.x,
          mark!.number.y - other!.number.y,
        ),
      ).toBeGreaterThanOrEqual(APART)
  }

  // As many as the picture has places for, and still no two on one another.
  const full = marksOf(
    UNITS,
    Array.from({ length: 150 }, () => feature({ point: corner })),
  )
  const seen = new Set(
    full.map((mark) => `${mark!.number.x} ${mark!.number.y}`),
  )
  expect(seen.size).toBe(150)
  // Past that there is nowhere left, and a number stands on another rather
  // than outside the picture.
  const over = marksOf(
    UNITS,
    Array.from({ length: 400 }, () => feature({ point: corner })),
  )
  for (const mark of over)
    for (const value of [mark!.number.x, mark!.number.y]) {
      expect(value).toBeGreaterThanOrEqual(DISC)
      expect(value).toBeLessThanOrEqual(SIZE - DISC)
    }
})

test('a medical response has no map: the server’s sentence is shown and nothing else', async () => {
  const none = 'Beacon draws no map of a medical response.'
  let reply: () => Response = () => Response.json(answer())
  vi.useFakeTimers({ shouldAdvanceTime: true })
  serve({ 'GET /api/health': MOCK_HEALTH, [MAP]: () => reply() })
  render(<App />)
  expect(await screen.findByText(PLACE)).toBeInTheDocument()

  // The server stops having a map at this address. The last reading goes.
  reply = () => Response.json({ detail: none }, { status: 404 })
  await act(() => vi.advanceTimersByTimeAsync(10_100))

  expect(await screen.findByText(none)).toBeInTheDocument()
  expect(screen.queryByText(PLACE)).toBeNull()
  expect(screen.queryByText(fire.title)).toBeNull()
  expect(document.querySelector('.map-canvas')).toBeNull()
  expect(tiles()).toEqual([])
  expect(screen.queryByText(/may be out of date/)).toBeNull()
})

test('an incident with no point shows why nothing is drawn, in the server’s sentence, and its card', async () => {
  const why = 'Beacon has no point for this incident, so it draws no map of it.'
  serve({
    'GET /api/health': MOCK_HEALTH,
    [MAP]: answer({
      features: [],
      text: why,
      incident: { ...fire, point: null },
    }),
  })

  render(<App />)

  expect(await screen.findByText(why)).toBeInTheDocument()
  expect(document.querySelector('.map-canvas')).toBeNull()
  expect(tiles()).toEqual([])
  expect(screen.queryByText('What is on the map')).toBeNull()
  expect(screen.getByRole('link', { name: fire.title })).toBeInTheDocument()
})

test('a map that could not be read again stays, and says it may be out of date', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  let reply: () => Response = () => Response.json(answer())
  serve({ 'GET /api/health': MOCK_HEALTH, [MAP]: () => reply() })
  render(<App />)
  expect(await screen.findByText(PLACE)).toBeInTheDocument()

  reply = () => Response.json({ detail: 'Down.' }, { status: 503 })
  await act(() => vi.advanceTimersByTimeAsync(10_100))

  expect(
    await screen.findByText('Down. What is shown may be out of date.'),
  ).toBeInTheDocument()
  expect(screen.getByText(PLACE)).toBeInTheDocument()
})
