import { act, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App.tsx'
import type { PlacesAnswer } from './api/places.ts'
import { PLACES_MS } from './lib/cadence.ts'
import {
  MOST_PLACES,
  placeName,
  placesSnapshot,
  removePlace,
  savePlace,
  subscribePlaces,
} from './lib/placeStore.ts'
import { fireAnswer } from './test/fixtures.ts'
import { MOCK_HEALTH, serve } from './test/server.ts'

const KEY = 'beacon.places.v1'
const CHECK = 'POST /api/places/check'
const FIRE = { lat: 37.776, lon: -122.424 }
const FAR = { lat: 37.8, lon: -122.41 }
const fire = fireAnswer.incident!

const CAVEAT =
  'A distance here is between the block Beacon has for an incident and this place as it was saved.'
const NOTHING =
  'Beacon has no open incident with a known place within a kilometre of this place. That is not an official all-clear.'
const DISTANCE = 'Beacon has this incident about 50 m from this place.'

const nothing = { near: [], text: NOTHING, credit: null }
const nearFire = {
  near: [{ incident: fire, distance_m: 50, text: DISTANCE }],
  text: null,
  credit: null,
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
const click = (element: HTMLElement) => act(() => element.click())

beforeEach(() => {
  window.localStorage.clear()
  window.location.hash = '#/places'
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

test('the button’s page links to the saved places and lists none of them', async () => {
  savePlace('Home', FIRE)
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
  click(spot)

  // On the device: the name, tidied, and the place.
  expect(stored()).toEqual([
    { id: expect.any(String), name: 'My flat', ...FIRE },
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

test('a place with nothing near it says so in the server’s sentence, which is no all-clear', async () => {
  savePlace('Home', FIRE)
  savePlace('Work', FAR)
  serve({ 'GET /api/health': MOCK_HEALTH, [CHECK]: answer(nearFire, nothing) })

  render(<App />)

  const work = (await screen.findByRole('heading', { name: 'Work' })).closest(
    'section',
  )!
  expect(await within(work).findByText(NOTHING)).toBeInTheDocument()
  expect(within(work).queryByText(DISTANCE)).not.toBeInTheDocument()
  const home = screen.getByRole('heading', { name: 'Home' }).closest('section')!
  expect(within(home).getByText(DISTANCE)).toBeInTheDocument()
  expect(within(home).queryByText(NOTHING)).not.toBeInTheDocument()
})

test('511’s credit is shown with a place whose distance is measured from a place of 511’s', async () => {
  savePlace('Home', FIRE)
  savePlace('Work', FAR)
  const credit = { text: 'Data provided by 511.org', url: 'https://511.org' }
  serve({
    'GET /api/health': MOCK_HEALTH,
    [CHECK]: answer({ ...nearFire, credit }, nothing),
  })

  render(<App />)

  const home = (await screen.findByRole('heading', { name: 'Home' })).closest(
    'section',
  )!
  expect(
    await within(home).findByRole('link', { name: credit.text }),
  ).toHaveAttribute('href', credit.url)
  const work = screen.getByRole('heading', { name: 'Work' }).closest('section')!
  expect(within(work).queryByText(credit.text)).not.toBeInTheDocument()
})

test('no place is sent until the server has said which mode it is in', async () => {
  savePlace('Home', FIRE)
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

test('a server in another mode than is shown answers nothing, and the mode is asked for again at once', async () => {
  savePlace('Home', FIRE)
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
  savePlace('Home', FIRE)
  savePlace('Work', FAR)
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

  click(screen.getByRole('button', { name: 'Remove Home from this device' }))

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
  savePlace('Home', FIRE)
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

  click(save)
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
  click(screen.getByRole('button', { name: 'Save where I am now' }))
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

  click(await screen.findByRole('button', { name: 'Save where I am now' }))

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

  click(
    await screen.findByRole('button', {
      name: 'Save “Across town, North Beach”',
    }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'This browser would not keep the place. Nothing was saved.',
  )
  expect(placesSnapshot()).toEqual([])

  refuse.mockRestore()
  click(screen.getByRole('button', { name: 'Save “Across town, North Beach”' }))
  expect(await screen.findByText(NOTHING)).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()

  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('full', 'QuotaExceededError')
  })
  click(
    screen.getByRole('button', {
      name: 'Remove Across town, North Beach from this device',
    }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The place is still saved on this device.',
  )
  expect(placesSnapshot()).toHaveLength(1)
})

test('no more places are kept than the server checks at once', async () => {
  for (let index = 0; index < MOST_PLACES; index += 1)
    expect(savePlace(`Place ${index}`, FAR)).toBe('saved')
  expect(savePlace('One more', FAR)).toBe('full')
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

test('a place saved or removed in another tab is known here', () => {
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

  expect(removePlace('a')).toBe(true)
  expect(heard).toHaveBeenCalledTimes(2)
  expect(placesSnapshot()).toEqual([])
  stop()
  window.dispatchEvent(new StorageEvent('storage', { key: KEY }))
  expect(heard).toHaveBeenCalledTimes(2)
})
