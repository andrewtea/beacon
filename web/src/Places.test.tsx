import { act, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App.tsx'
import type { IncidentSummary, Source } from './api/incidents.ts'
import type { PlacesAnswer } from './api/places.ts'
import { PLACES_MS } from './lib/cadence.ts'
import { covered } from './lib/geo.ts'
import {
  MOST_PLACES,
  placeName,
  placesSnapshot,
  removePlace,
  savePlace,
  subscribePlaces,
} from './lib/placeStore.ts'
import { fireAnswer } from './test/fixtures.ts'
import { fakeLocks } from './test/locks.ts'
import { MOCK_HEALTH, serve } from './test/server.ts'

const KEY = 'beacon.places.v1'
const CHECK = 'POST /api/places/check'
const FIRE = { lat: 37.776, lon: -122.424 }
const FAR = { lat: 37.8, lon: -122.41 }
const fire = fireAnswer.incident!

// Sentences of the server's, which the view shows as they are given.
const CAVEAT =
  'A distance does not tell you whether this place is inside or outside the area of an official order or alert.'
const NOTHING =
  'Beacon lists no incident within a kilometre of this saved place in this reading. That is not an official all-clear.'
const DISTANCE = 'Beacon has this incident about 50 m from this place.'

const nothing = { near: [], text: NOTHING }
const nearFire = {
  near: [{ incident: fire, distance_m: 50, text: DISTANCE }],
  text: null,
}
const answer = (...places: PlacesAnswer['places']): PlacesAnswer => ({
  mode: 'mock',
  as_of: '2026-11-26T23:44:34Z',
  partial: false,
  reach_m: 1000,
  caveat_text: CAVEAT,
  places,
})

const stored = (): unknown =>
  JSON.parse(window.localStorage.getItem(KEY) ?? '[]')
// A press, and whatever it starts: saving and removing are waited for.
const click = (element: HTMLElement) => act(async () => element.click())

beforeEach(() => {
  window.localStorage.clear()
  window.location.hash = '#/places'
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test('the button’s page links to the saved places and lists none of them', async () => {
  await savePlace('Home', FIRE)
  window.location.hash = ''
  const asked = serve({ 'GET /api/health': MOCK_HEALTH })

  render(<App />)

  const link = await screen.findByRole('link', { name: 'Open your places' })
  expect(link).toHaveAttribute('href', '#/places')
  expect(screen.queryByText('Home')).not.toBeInTheDocument()
  // Nothing is asked about a place until its own page is opened.
  expect(asked.filter((request) => request.key === CHECK)).toEqual([])
})

test('a place is kept on the device under its name, and only where it is is sent', async () => {
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: answer(nearFire),
  })
  render(<App />)

  // Nothing is saved yet, so nothing is asked.
  const spot = await screen.findByRole('button', {
    name: 'Save “Near the invented fire, Hayes Valley”',
  })
  expect(asked.filter((request) => request.key === CHECK)).toEqual([])
  const name = screen.getByLabelText('Its name, for you only')
  await act(async () => {
    // As a person types it.
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      'value',
    )?.set?.call(name, '  My   flat ')
    name.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click(spot)

  // On the device: the name, tidied, and the place.
  // And that it was saved with the invented place's own button.
  expect(stored()).toEqual([
    { id: expect.any(String), name: 'My flat', ...FIRE, spot: true },
  ])
  // To the server: where it is, the mode that is shown, and nothing else.
  const section = (
    await screen.findByRole('heading', { name: 'My flat' })
  ).closest('section')!
  expect(await within(section).findByText(DISTANCE)).toBeInTheDocument()
  const sent = asked.filter((request) => request.key === CHECK)
  expect(sent).toHaveLength(1)
  expect(sent[0].body).toEqual({ places: [FIRE] })
  expect(sent[0].headers['beacon-mode']).toBe('mock')
  expect(JSON.stringify(sent[0])).not.toContain('flat')

  // The answer is the server's: its distance, its caveat, and the incident's
  // card, which opens the incident's own page.
  expect(screen.getByText(CAVEAT)).toBeInTheDocument()
  expect(
    within(section).getByRole('link', { name: fire.title }),
  ).toHaveAttribute('href', `#/incident/${fire.id}`)
  // The invented scenario's incident is labeled as that on its card.
  expect(within(section).getByText('Invented')).toBeInTheDocument()
})

test('what is said of the server is on the places’ page, under the control that saves a place', async () => {
  const front = 'On this server a tap is sent from one of two invented places.'
  serve({
    'GET /api/health': {
      ...MOCK_HEALTH,
      input: 'invented',
      front_text: front,
      front_more_text: 'The rest.',
    },
  })
  render(<App />)
  window.location.hash = '#/places'

  const about = await screen.findByRole('region', { name: 'About this server' })
  const follows = (first: Element, second: Element) =>
    Boolean(
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
    )
  // The page's own heading and its first control come first, after a line
  // that says the scenario is invented.
  const line = screen.getByText(/Nothing Beacon shows here happened/)
  const heading = screen.getByRole('heading', { name: 'Your places' })
  const save = screen.getByRole('heading', { name: 'Save a place' })
  expect(follows(line, heading)).toBe(true)
  expect(follows(save, about)).toBe(true)
  // Whole, and without a press.
  expect(within(about).getByText(front)).toBeInTheDocument()
  expect(
    within(about).getByText(/running on an invented scenario/),
  ).toBeInTheDocument()
})

test('a server that takes only the invented places is sent no place of a person’s own, and none is asked of the phone', async () => {
  // A place saved when another server took it: somebody's own.
  const OWN = { lat: 37.775, lon: -122.42 }
  await savePlace('Home', OWN)
  // And one saved from where somebody was, a block from the invented fire,
  // when another server took that: it is kept at the invented place's own
  // rounded position, and is somebody's own all the same.
  await savePlace('My flat', { lat: 37.7761, lon: -122.4239 })
  expect(stored()).toContainEqual({
    id: expect.any(String),
    name: 'My flat',
    ...FIRE,
  })
  // Only this one was saved with the invented place's own button.
  await savePlace('The fire', FIRE, true)
  const asked = serve({
    'GET /api/health': {
      ...MOCK_HEALTH,
      input: 'invented',
      front_text:
        'On this server a tap is sent from one of two invented places.',
      front_more_text: 'The rest.',
    },
    [CHECK]: answer(nearFire),
  })
  render(<App />)

  // Once the server has said what it takes, the invented place is asked about,
  // and answered under its own name.
  await screen.findByText(/This server is asked only about the two invented/)
  const section = (
    await screen.findByRole('heading', { name: 'The fire' })
  ).closest('section')!
  expect(await within(section).findByText(DISTANCE)).toBeInTheDocument()
  const sent = asked.filter((request) => request.key === CHECK)
  // One place, once: the flat at the same position is not a second.
  expect(sent.map((request) => request.body)).toEqual([{ places: [FIRE] }])
  expect(JSON.stringify(asked)).not.toContain('37.775')
  // The other stays on the device, is said not to be sent, and can be removed.
  expect(
    screen.getByText(/This server is asked only about the two invented places/),
  ).toHaveTextContent('Nothing of the places below is sent to it.')
  for (const name of ['Home', 'My flat']) {
    const kept = screen.getByRole('heading', { name }).closest('section')!
    expect(within(kept).queryByText(DISTANCE)).not.toBeInTheDocument()
    expect(
      within(kept).getByRole('button', {
        name: `Remove ${name} from this device`,
      }),
    ).toBeInTheDocument()
  }
  // Where the phone is cannot be saved here: only the two invented places can.
  expect(
    screen.queryByRole('button', { name: 'Save where I am now' }),
  ).not.toBeInTheDocument()
  expect(
    screen.getByRole('button', {
      name: 'Save “Across town, North Beach”',
    }),
  ).toBeInTheDocument()
})

test('the phone is not asked where it is until the server has said it takes that, or after it stopped saying', async () => {
  // On a server that takes only the invented places, a place saved from where
  // the phone is would be somebody's own. Until the server has said which it
  // is, and whenever its latest answer failed, it is not known to be the other.
  await savePlace('The fire', FIRE)
  const located = vi.fn()
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: { getCurrentPosition: located },
  })
  let health: (response: Response) => void = () => undefined
  const asked = serve({
    'GET /api/health': () =>
      new Promise<Response>((resolve) => (health = resolve)),
    [CHECK]: answer(nearFire),
  })
  render(<App />)

  // Before the server answers: the saved place is listed and can be removed,
  // and nothing offers to save where the phone is.
  expect(await screen.findByText('The fire')).toBeInTheDocument()
  expect(
    screen.getByText('A place can be saved once Beacon’s server has answered.'),
  ).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: 'Save where I am now' }),
  ).not.toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'Remove The fire from this device' }),
  ).toBeInTheDocument()

  // An answer that does not say what the server takes is not taken for `own`:
  // nothing is offered, and nothing of a place is sent.
  const silent: Record<string, unknown> = { ...MOCK_HEALTH }
  delete silent.input
  await act(async () => health(Response.json(silent)))
  await screen.findByText('Invented scenario')
  expect(
    screen.queryByRole('button', { name: 'Save where I am now' }),
  ).not.toBeInTheDocument()
  expect(asked.filter((request) => request.key === CHECK)).toEqual([])
  expect(located).not.toHaveBeenCalled()
})

test('a server that says it takes a visitor’s own place offers to save where the phone is', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH })
  render(<App />)

  expect(
    await screen.findByRole('button', { name: 'Save where I am now' }),
  ).toBeEnabled()
})

test('a place with nothing near it says so in the server’s sentence, which is no all-clear', async () => {
  await savePlace('Home', FIRE)
  await savePlace('Work', FAR)
  serve({ 'GET /api/health': MOCK_HEALTH, [CHECK]: answer(nearFire, nothing) })

  render(<App />)

  const work = (await screen.findByText(NOTHING)).closest('section')!
  expect(
    within(work).getByRole('heading', { name: 'Work' }),
  ).toBeInTheDocument()
  expect(within(work).queryByText(DISTANCE)).not.toBeInTheDocument()
  const home = screen.getByRole('heading', { name: 'Home' }).closest('section')!
  expect(within(home).getByText(DISTANCE)).toBeInTheDocument()
  expect(within(home).queryByText(NOTHING)).not.toBeInTheDocument()
})

test('a distance measured from a point taken from a 511 event shows that row’s confidence and 511’s credit, beside that distance only', async () => {
  await savePlace('Home', FIRE)
  const credit = {
    text: 'Data provided by 511.org',
    url: 'https://511.org',
    parts: [],
  }
  const closure: Source = {
    kind: 'traffic',
    source: '511',
    label: '511 SF Bay traffic events',
    confidence: 'official',
    at: '2026-11-26T23:30:00Z',
    evidence_id: 12,
    url: null,
    stand_in: true,
    model: null,
    credit,
  }
  // Two incidents near one place: one where its block is, one at a point taken from a 511 event that listed
  // a closure on its street.
  const other: IncidentSummary = {
    ...fire,
    id: 'inc-00000000000000aa',
    title: 'Alarm on Oak St',
    point_from: 'closure',
    point_source: closure,
  }
  const BY_511 =
    'Beacon has this incident about 300 m from this place, measured from an approximate point Beacon took from a 511 event that listed a road closure on its street.'
  expect(fire.point_source).toBeNull()
  serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: answer({
      near: [
        { incident: fire, distance_m: 50, text: DISTANCE },
        { incident: other, distance_m: 300, text: BY_511 },
      ],
      text: null,
    }),
  })

  render(<App />)

  const by511 = (await screen.findByText(BY_511)).closest('li')!
  expect(
    within(by511).getByRole('link', { name: credit.text }),
  ).toHaveAttribute('href', credit.url)
  expect(within(by511).getByText('Official source')).toBeInTheDocument()
  expect(
    within(by511).getByText('511 SF Bay traffic events'),
  ).toBeInTheDocument()
  // The distance from the block's own point has nothing of 511's beside it.
  const byBlock = screen.getByText(DISTANCE).closest('li')!
  expect(within(byBlock).queryByText(credit.text)).not.toBeInTheDocument()
  expect(within(byBlock).queryByText('Official source')).not.toBeInTheDocument()
  expect(screen.getAllByRole('link', { name: credit.text })).toHaveLength(1)
})

test('no place is sent until the server has said which mode it is in', async () => {
  await savePlace('Home', FIRE)
  let health: (response: Response) => void = () => undefined
  const asked = serve({
    'GET /api/health': () =>
      new Promise<Response>((resolve) => (health = resolve)),
    [CHECK]: answer(nearFire),
  })
  render(<App />)

  // The place is listed from the device, and nothing is asked of the server.
  expect(await screen.findByText('Home')).toBeInTheDocument()
  expect(
    screen.getByText(/once it has heard from its\s+server/),
  ).toBeInTheDocument()
  expect(asked.filter((request) => request.key === CHECK)).toEqual([])
  expect(
    screen.queryByRole('button', { name: /invented fire/ }),
  ).not.toBeInTheDocument()

  health(Response.json({ ...MOCK_HEALTH, mode: 'live' }))
  expect(await screen.findByText(DISTANCE)).toBeInTheDocument()
  const sent = asked.filter((request) => request.key === CHECK)
  expect(sent[0].headers['beacon-mode']).toBe('live')
  // A server on real sources offers no invented place to save.
  expect(
    screen.queryByRole('button', { name: /invented fire/ }),
  ).not.toBeInTheDocument()
  expect(screen.queryByText(/made-up fire/)).not.toBeInTheDocument()
  expect(screen.queryByText('Invented scenario')).not.toBeInTheDocument()
})

test('a place can be removed while the server has not said its mode, and is then never sent', async () => {
  await savePlace('Home', FIRE)
  let health: (response: Response) => void = () => undefined
  const asked = serve({
    'GET /api/health': () =>
      new Promise<Response>((resolve) => (health = resolve)),
    [CHECK]: answer(nearFire),
  })
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Remove Home from this device' }),
  )

  expect(stored()).toEqual([])
  expect(
    screen.queryByRole('heading', { name: 'Home' }),
  ).not.toBeInTheDocument()
  await act(async () => health(Response.json(MOCK_HEALTH)))
  expect(
    await screen.findByRole('button', { name: /Save “Near the invented fire/ }),
  ).toBeInTheDocument()
  expect(asked.filter((request) => request.key === CHECK)).toEqual([])
})

test('a server in another mode than is shown answers nothing, and the mode is asked for again at once', async () => {
  await savePlace('Home', FIRE)
  const refusal =
    'This server is not in the mode the sender was shown. Nothing of the request was read.'
  // The app is told the invented scenario at its first asking. The server has
  // since gone over to real sources, and says so when it is asked again, which
  // here it answers only when it is let.
  let healthAsks = 0
  let again: (response: Response) => void = () => undefined
  let live = false
  const asked = serve({
    'GET /api/health': () =>
      ++healthAsks === 1
        ? Response.json(MOCK_HEALTH)
        : new Promise<Response>((resolve) => (again = resolve)),
    [CHECK]: () =>
      live
        ? Response.json({ ...answer(nearFire), mode: 'live' })
        : Response.json({ detail: refusal }, { status: 409 }),
  })
  render(<App />)

  // The refusal has the mode asked for again now, not at the next half minute.
  // Until that is answered the mode is not known, and nothing is asked or shown.
  await waitFor(() => expect(healthAsks).toBe(2))
  expect(
    await screen.findByText(/once it has heard from its\s+server/),
  ).toBeInTheDocument()
  expect(screen.queryByText(DISTANCE)).not.toBeInTheDocument()
  expect(asked.filter((request) => request.key === CHECK)).toHaveLength(1)
  // Then the places are asked about under the mode the server is in.
  live = true
  await act(async () => again(Response.json({ ...MOCK_HEALTH, mode: 'live' })))
  expect(await screen.findByText(DISTANCE)).toBeInTheDocument()
  const sent = asked.filter((request) => request.key === CHECK)
  expect(sent.map((request) => request.headers['beacon-mode'])).toEqual([
    'mock',
    'live',
  ])
  expect(screen.queryByText('Invented scenario')).not.toBeInTheDocument()
})

test('removing a place takes it off the device, and an answer for the old list is shown under nobody', async () => {
  await savePlace('Home', FIRE)
  await savePlace('Work', FAR)
  // The second asking is answered only when it is let.
  let second: (response: Response) => void = () => undefined
  let asks = 0
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: () =>
      ++asks === 1
        ? Response.json(answer(nearFire, nothing))
        : new Promise<Response>((resolve) => (second = resolve)),
  })
  render(<App />)
  expect(await screen.findByText(DISTANCE)).toBeInTheDocument()

  await click(
    screen.getByRole('button', { name: 'Remove Home from this device' }),
  )

  expect(stored()).toEqual([{ id: expect.any(String), name: 'Work', ...FAR }])
  expect(
    screen.queryByRole('heading', { name: 'Home' }),
  ).not.toBeInTheDocument()
  // What was said of Home is not shown under Work while Work is asked about.
  expect(screen.queryByText(DISTANCE)).not.toBeInTheDocument()
  expect(screen.queryByText(NOTHING)).not.toBeInTheDocument()
  await waitFor(() => expect(asks).toBe(2))
  expect(asked.at(-1)?.body).toEqual({ places: [FAR] })

  // An answer that does not have one entry for each place is shown under none.
  await act(async () => second(Response.json(answer(nearFire, nothing))))
  expect(screen.queryByText(DISTANCE)).not.toBeInTheDocument()
  expect(screen.queryByText(NOTHING)).not.toBeInTheDocument()
})

test('what is near a place is asked for again while its page is open, and not after', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  await savePlace('Home', FIRE)
  let reply = answer(nothing)
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: () => Response.json(reply),
  })
  const checks = () => asked.filter((request) => request.key === CHECK).length
  render(<App />)
  expect(await screen.findByText(NOTHING)).toBeInTheDocument()

  reply = answer(nearFire)
  await act(() => vi.advanceTimersByTimeAsync(PLACES_MS + 100))
  expect(await screen.findByText(DISTANCE)).toBeInTheDocument()
  expect(screen.queryByText(NOTHING)).not.toBeInTheDocument()
  expect(checks()).toBe(2)

  // Back at the button, no place is sent again.
  await act(async () => {
    window.location.hash = ''
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
  await act(() => vi.advanceTimersByTimeAsync(PLACES_MS * 3))
  expect(checks()).toBe(2)
})

test('where the phone is now is rounded to about a block before it is kept, and nothing is kept once the page is left', async () => {
  let found: PositionCallback = () => undefined
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: {
      getCurrentPosition: (callback: PositionCallback) => (found = callback),
    },
  })
  const here = {
    coords: { latitude: 37.776412, longitude: -122.424391, accuracy: 8 },
  } as GeolocationPosition
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: answer(nearFire),
  })
  render(<App />)
  const save = await screen.findByRole('button', {
    name: 'Save where I am now',
  })

  await click(save)
  expect(
    await screen.findByRole('button', { name: 'Finding where you are…' }),
  ).toBeDisabled()
  await act(async () => found(here))

  expect(stored()).toEqual([
    { id: expect.any(String), name: 'A saved place', ...FIRE },
  ])
  expect(await screen.findByText(DISTANCE)).toBeInTheDocument()
  expect(asked.find((request) => request.key === CHECK)?.body).toEqual({
    places: [FIRE],
  })
  expect(JSON.stringify(asked)).not.toContain('776412')

  // Pressed again, and the page is left before the phone answers.
  await click(screen.getByRole('button', { name: 'Save where I am now' }))
  await act(async () => {
    window.location.hash = ''
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
  await act(async () => found(here))
  expect(stored()).toHaveLength(1)
})

test('a phone that will not say where it is saves nothing, and says why', async () => {
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: {
      getCurrentPosition: (
        _found: PositionCallback,
        failed: PositionErrorCallback,
      ) =>
        failed({ code: 1, PERMISSION_DENIED: 1 } as GeolocationPositionError),
    },
  })
  serve({ 'GET /api/health': MOCK_HEALTH })
  render(<App />)

  await click(
    await screen.findByRole('button', { name: 'Save where I am now' }),
  )

  expect(await screen.findByRole('alert')).toHaveTextContent(
    /Allow location for this site/,
  )
  expect(stored()).toEqual([])
})

test('a browser that will not keep a place says nothing was saved, and one that will not remove it says it is still there', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH, [CHECK]: answer(nothing) })
  const refuse = vi
    .spyOn(Storage.prototype, 'setItem')
    .mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
  render(<App />)

  await click(
    await screen.findByRole('button', {
      name: 'Save “Across town, North Beach”',
    }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'This browser would not keep the place. Nothing was saved.',
  )
  expect(placesSnapshot()).toEqual([])

  refuse.mockRestore()
  await click(
    screen.getByRole('button', { name: 'Save “Across town, North Beach”' }),
  )
  expect(await screen.findByText(NOTHING)).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()

  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('full', 'QuotaExceededError')
  })
  await click(
    screen.getByRole('button', {
      name: 'Remove Across town, North Beach from this device',
    }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The place is still saved on this device.',
  )
  expect(placesSnapshot()).toHaveLength(1)
})

test('a phone outside the area Beacon covers saves nothing, and the places already saved go on being answered', async () => {
  await savePlace('Home', FIRE)
  // Oakland.
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: {
      getCurrentPosition: (found: PositionCallback) =>
        found({
          coords: { latitude: 37.8044, longitude: -122.2712, accuracy: 8 },
        } as GeolocationPosition),
    },
  })
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: answer(nearFire),
  })
  render(<App />)
  expect(await screen.findByText(DISTANCE)).toBeInTheDocument()

  await click(screen.getByRole('button', { name: 'Save where I am now' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Beacon covers San Francisco, and this place is somewhere else. Nothing was saved.',
  )
  expect(stored()).toEqual([{ id: expect.any(String), name: 'Home', ...FIRE }])
  expect(screen.getByText(DISTANCE)).toBeInTheDocument()
  // Nothing of the place that was refused was sent.
  const sent = asked.filter((request) => request.key === CHECK)
  expect(sent.map((request) => request.body)).toEqual(
    sent.map(() => ({ places: [FIRE] })),
  )
  expect(JSON.stringify(asked)).not.toContain('122.27')
})

test('a place outside the area Beacon covers is not saved, and one in storage is not read', async () => {
  // The edge is inside, as the server has it, and a block past it is not.
  expect(covered({ lat: 37.84, lon: -122.35 })).toBe(true)
  expect(covered({ lat: 37.841, lon: -122.35 })).toBe(false)
  expect(covered({ lat: 37.84, lon: -122.349 })).toBe(false)
  expect(covered({ lat: 37.699, lon: -122.53 })).toBe(false)
  expect(covered({ lat: 37.7, lon: -122.531 })).toBe(false)
  // As it is rounded before it is sent.
  expect(covered({ lat: 37.8404, lon: -122.35 })).toBe(true)
  expect(await savePlace('Oakland', { lat: 37.8044, lon: -122.2712 })).toBe(
    'elsewhere',
  )
  expect(stored()).toEqual([])
  // A place kept by an earlier version that the server would refuse is not
  // read: sent with the others, it would have every one of them refused.
  window.localStorage.setItem(
    KEY,
    JSON.stringify([
      { id: 'a', name: 'Oakland', lat: 37.804, lon: -122.271 },
      { id: 'b', name: 'Home', ...FIRE },
    ]),
  )
  expect(placesSnapshot().map((place) => place.name)).toEqual(['Home'])
})

test('a place another tab removed while this one waited to save is not put back', async () => {
  await savePlace('Home', FIRE)
  await savePlace('Work', FAR)
  const names = () =>
    (stored() as { name: string }[]).map((place) => place.name)
  const locks = fakeLocks()
  vi.stubGlobal('navigator', { ...navigator, locks })

  // Another tab has the list to itself, and is about to take Home off it.
  let holding = false
  let done: () => void = () => undefined
  void locks.request(
    'beacon.places',
    () =>
      new Promise<void>((resolve) => {
        holding = true
        done = resolve
      }),
  )
  await waitFor(() => expect(holding).toBe(true))

  // This tab saves a place and removes one meanwhile. Neither is done yet.
  const saving = savePlace('Gym', FAR)
  const removing = removePlace(placesSnapshot()[1].id)
  await Promise.resolve()
  expect(names()).toEqual(['Home', 'Work'])
  expect(locks.request).toHaveBeenLastCalledWith(
    'beacon.places',
    expect.any(Function),
  )

  // The other tab writes the list without Home, and is done.
  window.localStorage.setItem(
    KEY,
    JSON.stringify(
      (stored() as { name: string }[]).filter((place) => place.name !== 'Home'),
    ),
  )
  done()

  // Each change is made to the list as it stood at its turn: Home stays
  // removed, Gym is saved, and Work is removed.
  expect(await saving).toBe('saved')
  expect(await removing).toBe(true)
  expect(names()).toEqual(['Gym'])
})

test('a browser with no lock for its tabs to share, or one that will not give it, changes nothing and says so', async () => {
  await savePlace('Home', FIRE)
  const before = window.localStorage.getItem(KEY)
  const write = vi.spyOn(Storage.prototype, 'setItem')
  const id = placesSnapshot()[0].id

  // No lock at all: the list is not read and written with nothing to keep
  // another tab's change from being undone.
  vi.stubGlobal('navigator', { geolocation: navigator.geolocation })
  expect(await savePlace('Work', FAR)).toBe('failed')
  expect(await removePlace(id)).toBe(false)

  // A lock the browser refuses.
  vi.stubGlobal('navigator', {
    locks: { request: () => Promise.reject(new DOMException('no')) },
  })
  expect(await savePlace('Work', FAR)).toBe('failed')
  expect(await removePlace(id)).toBe(false)

  expect(write).not.toHaveBeenCalled()
  expect(window.localStorage.getItem(KEY)).toBe(before)
})

test('with no lock, the page says that nothing was saved and that a place is still there', async () => {
  await savePlace('Home', FIRE)
  serve({ 'GET /api/health': MOCK_HEALTH, [CHECK]: answer(nearFire) })
  vi.stubGlobal('navigator', { geolocation: navigator.geolocation })
  render(<App />)

  await click(
    await screen.findByRole('button', {
      name: 'Save “Across town, North Beach”',
    }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'This browser would not keep the place. Nothing was saved.',
  )

  await click(
    screen.getByRole('button', { name: 'Remove Home from this device' }),
  )
  expect(
    await screen.findByText(/The place is still\s+saved on this device\./),
  ).toBeInTheDocument()
  expect(stored()).toEqual([{ id: expect.any(String), name: 'Home', ...FIRE }])
})

test('no more places are kept than the server checks at once', async () => {
  for (let index = 0; index < MOST_PLACES; index += 1)
    expect(await savePlace(`Place ${index}`, FAR)).toBe('saved')
  expect(await savePlace('One more', FAR)).toBe('full')
  serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: answer(...Array.from({ length: MOST_PLACES }, () => nothing)),
  })

  render(<App />)

  expect(
    await screen.findByText(/Beacon keeps up to 5 places/),
  ).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: 'Save where I am now' }),
  ).not.toBeInTheDocument()
  expect(placesSnapshot()).toHaveLength(MOST_PLACES)
})

test('what is not a list of places in storage is read as none, and a name is never empty', () => {
  window.localStorage.setItem(KEY, '{"not": "a list"}')
  expect(placesSnapshot()).toEqual([])
  window.localStorage.setItem(KEY, 'not json')
  expect(placesSnapshot()).toEqual([])
  window.localStorage.setItem(
    KEY,
    JSON.stringify([
      { id: 'a', name: 'Home', lat: 37.776, lon: -122.424 },
      { id: 'b', name: 'No place' },
      { id: 'c', name: 'Not a number', lat: '37.7', lon: null },
    ]),
  )
  expect(placesSnapshot().map((place) => place.name)).toEqual(['Home'])
  expect(placeName('   ')).toBe('A saved place')
  expect(placeName('x'.repeat(100))).toHaveLength(40)
})

test('a place saved or removed in another tab is known here', async () => {
  const heard = vi.fn()
  const stop = subscribePlaces(heard)
  const first = placesSnapshot()
  // The same list while nothing changed.
  expect(placesSnapshot()).toBe(first)

  // Another tab writes, and the browser says so here.
  window.localStorage.setItem(
    KEY,
    JSON.stringify([{ id: 'a', name: 'Home', ...FIRE }]),
  )
  window.dispatchEvent(new StorageEvent('storage', { key: KEY }))
  expect(heard).toHaveBeenCalledTimes(1)
  expect(placesSnapshot().map((place) => place.name)).toEqual(['Home'])

  // Another key's change is not this list's.
  window.dispatchEvent(new StorageEvent('storage', { key: 'beacon.taps.v1' }))
  expect(heard).toHaveBeenCalledTimes(1)

  expect(await removePlace('a')).toBe(true)
  expect(heard).toHaveBeenCalledTimes(2)
  expect(placesSnapshot()).toEqual([])
  stop()
  window.dispatchEvent(new StorageEvent('storage', { key: KEY }))
  expect(heard).toHaveBeenCalledTimes(2)
})
