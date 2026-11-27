import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App.tsx'
import { CrisisPanel } from './components/IncidentBody.tsx'
import { IncidentCard } from './components/IncidentCard.tsx'
import { Sources } from './components/Provenance.tsx'
import { OPEN_MS } from './lib/cadence.ts'
import { drawPhoto } from './lib/photo.ts'
import { keepTap, keptTaps, resetTaps, sweepTaps } from './lib/tapStore.ts'
import {
  FIRE_TAP,
  fireAnswer,
  fireIncident,
  fireRun,
  QUIET_TAP,
  quietAnswer,
} from './test/fixtures.ts'
import { MOCK_HEALTH, serve } from './test/server.ts'
import { Home } from './views/Home.tsx'

vi.mock('./lib/recorder.ts', () => ({
  CLIP_SECONDS: 5,
  ClipError: class extends Error {},
  // A clip of four bytes, in place of five seconds of a microphone.
  recordClip: vi.fn(async () => new Uint8Array([82, 73, 70, 70])),
}))

vi.mock('./lib/photo.ts', async (original) => ({
  ...(await original<typeof import('./lib/photo.ts')>()),
  drawPhoto: vi.fn(),
}))

const FIRE_ID = fireIncident.incident.id
const fireRoutes = {
  'GET /api/health': MOCK_HEALTH,
  [`GET /api/taps/${FIRE_TAP}`]: fireAnswer,
  [`GET /api/taps/${FIRE_TAP}/run`]: fireRun,
  [`GET /api/incidents/${FIRE_ID}`]: fireIncident,
}
const receipt = (id: string, inventedOnly = false) => ({
  id,
  received_at: new Date().toISOString(),
  mode: 'mock',
  photo: false,
  clip: false,
  media_kept_until: null,
  investigating: true,
  invented_only: inventedOnly,
})

beforeEach(() => {
  window.localStorage.clear()
  resetTaps()
  window.location.hash = ''
})
afterEach(() => vi.useRealTimers())

const ASK = 'What’s that?'
const INVENTED = /running on an invented scenario, not real sources/

const click = (element: HTMLElement) => element.click()

test('says that it runs on an invented scenario before anybody taps', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH })

  render(<App />)

  expect(await screen.findByText(INVENTED)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: ASK })).toBeEnabled()
})

test('says, in the server’s sentence, that the invented scenario is told again and what becomes of taps', async () => {
  const repeats =
    'On this server the invented scenario is told again from its beginning when it is over.'
  serve({ 'GET /api/health': { ...MOCK_HEALTH, repeats_text: repeats } })

  render(<App />)

  // Beside what says the scenario is invented, before anybody taps.
  const banner = await screen.findByText(INVENTED)
  expect(banner).toHaveTextContent(repeats)
})

const FRONT =
  'On this server a tap is sent from one of two invented places, not from where you are.'
const FRONT_MORE = 'The address a tap’s answer is read at holds the tap’s id.'
const INVENTED_ONLY = {
  ...MOCK_HEALTH,
  input: 'invented',
  front_text: FRONT,
  front_more_text: FRONT_MORE,
}

test('says, in the server’s sentences, what a server behind an unchecked host takes and why', async () => {
  serve({ 'GET /api/health': INVENTED_ONLY })

  render(<App />)

  // On the page a tap is sent from, before anybody taps, apart from what says
  // the scenario is invented: it is about what a person sends, not the incidents.
  expect(await screen.findByText(FRONT)).toBeInTheDocument()
  expect(screen.getByText(INVENTED)).not.toHaveTextContent(FRONT)
  // The rest is the server's too, and is there for whoever opens it.
  expect(screen.getByText('More about this')).toBeInTheDocument()
  expect(screen.getByText(FRONT_MORE)).toBeInTheDocument()
})

test('on a server that takes only the invented places, the button sends one of them and asks the phone nothing', async () => {
  const asked = serve({
    ...fireRoutes,
    'GET /api/health': INVENTED_ONLY,
    'POST /api/taps': () =>
      Response.json(receipt(FIRE_TAP, true), { status: 201 }),
  })
  // A phone that would say where it is, to the metre, if it were asked.
  const located = vi.fn((found: PositionCallback) =>
    found({
      coords: { latitude: 37.775123, longitude: -122.419876, accuracy: 8 },
    } as GeolocationPosition),
  )
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: { getCurrentPosition: located },
  })
  render(<App />)

  await screen.findByText(FRONT)
  // Nothing can be added to a tap, and nothing says a tap sends where you are.
  expect(
    screen.queryByRole('checkbox', { name: '5 seconds of sound' }),
  ).not.toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: 'A photo' }),
  ).not.toBeInTheDocument()
  expect(
    screen.queryByText(/A tap sends where you are/),
  ).not.toBeInTheDocument()
  // The button says what it sends here.
  expect(
    screen.getByText(/the button sends a tap from an invented place/),
  ).toHaveTextContent('Where you are is not asked for and not sent.')
  click(screen.getByRole('button', { name: ASK }))

  await screen.findByRole('heading', { level: 1, name: /Fire at/ })
  const sent = asked.filter((request) => request.key === 'POST /api/taps')
  expect(sent).toHaveLength(1)
  // The invented place to the digit, and nothing else: no accuracy, no clip.
  expect(sent[0].body).toEqual({ location: { lat: 37.776, lon: -122.424 } })
  expect(located).not.toHaveBeenCalled()
  // What the server said of the tap is kept with its id on the device.
  expect(keptTaps()).toEqual([
    expect.objectContaining({ id: FIRE_TAP, invented: true }),
  ])
  vi.unstubAllGlobals()
})

const hashOf = (view: string, id: string) =>
  view === 'trace' ? `#/tap/${id}/trace` : `#/tap/${id}`
const tapRequests = (asked: { key: string }[]) =>
  asked.filter((request) => request.key.startsWith('GET /api/taps/'))

test.each(['tap', 'trace'])(
  'a %s is not asked for, and its id not sent, until the server has said what it takes',
  async (view) => {
    // Asking names the tap's id in the request's address. A link to an answer
    // that is opened sends nothing until it is known whether this server is
    // one the id may be sent to.
    let health: (response: Response) => void = () => undefined
    const asked = serve({
      ...fireRoutes,
      'GET /api/health': () =>
        new Promise<Response>((resolve) => (health = resolve)),
    })
    window.location.hash = hashOf(view, FIRE_TAP)
    render(<App />)

    expect(
      await screen.findByText(
        'Beacon asks for this tap once it has heard from its server.',
      ),
    ).toBeInTheDocument()
    expect(tapRequests(asked)).toEqual([])

    // A server that does not say what it takes is not taken for one that
    // takes a visitor's own.
    const silent: Record<string, unknown> = { ...MOCK_HEALTH }
    delete silent.input
    await act(async () => health(Response.json(silent)))
    await screen.findByText('Invented scenario')
    expect(tapRequests(asked)).toEqual([])
    expect(JSON.stringify(asked)).not.toContain(FIRE_TAP)
  },
)

test.each(['tap', 'trace'])(
  'a server that takes only the invented places is asked for no %s this device does not know it took so',
  async (view) => {
    // One this device sent from where somebody was, to a server that took
    // that, and one a link names that this device never sent.
    keepTap({
      id: FIRE_TAP,
      at: new Date().toISOString(),
      photo: false,
      clip: false,
    })
    const asked = serve({ ...fireRoutes, 'GET /api/health': INVENTED_ONLY })
    for (const id of [FIRE_TAP, QUIET_TAP]) {
      window.location.hash = hashOf(view, id)
      const { unmount } = render(<App />)

      expect(
        await screen.findByRole('heading', {
          name: 'This tap is not asked for here',
        }),
      ).toBeInTheDocument()
      expect(screen.getByRole('status')).toHaveTextContent(
        'Beacon does not ask this server for it.',
      )
      // Nothing is said of what was sent before: a view of this tap can have
      // been open when the server changed what it takes.
      expect(screen.getByRole('status')).not.toHaveTextContent(/was not sent/)
      unmount()
    }
    expect(tapRequests(asked)).toEqual([])
    expect(JSON.stringify(asked)).not.toContain(FIRE_TAP)
    expect(JSON.stringify(asked)).not.toContain(QUIET_TAP)
    // The tap is still kept on the device: another server may answer for it.
    expect(keptTaps().map((tap) => tap.id)).toEqual([FIRE_TAP])
  },
)

test('a server that takes only the invented places is asked for a tap this device knows it took so', async () => {
  keepTap({
    id: FIRE_TAP,
    at: new Date().toISOString(),
    photo: false,
    clip: false,
    invented: true,
  })
  const asked = serve({ ...fireRoutes, 'GET /api/health': INVENTED_ONLY })
  window.location.hash = `#/tap/${FIRE_TAP}`
  render(<App />)

  await screen.findByRole('heading', { level: 1, name: /Fire at/ })
  expect(tapRequests(asked).map((request) => request.key)).toContain(
    `GET /api/taps/${FIRE_TAP}`,
  )
})

test.each(['tap', 'trace'])(
  'a %s the invented-only server no longer has stays said to be gone, and is not asked for again',
  async (view) => {
    // The story was told again: the server answers that it has no such tap,
    // and the app forgets it. That the device no longer knows the tap does
    // not take the answer away, or what the server says of why.
    const repeats =
      'On this server the invented scenario is told again from its beginning when it is over.'
    keepTap({
      id: FIRE_TAP,
      at: new Date().toISOString(),
      photo: false,
      clip: false,
      invented: true,
    })
    const gone = () =>
      Response.json({ detail: 'There is no such tap.' }, { status: 404 })
    const asked = serve({
      'GET /api/health': { ...INVENTED_ONLY, repeats_text: repeats },
      [`GET /api/taps/${FIRE_TAP}`]: gone,
      [`GET /api/taps/${FIRE_TAP}/run`]: gone,
    })
    window.location.hash = hashOf(view, FIRE_TAP)
    render(<App />)

    await waitFor(() => expect(keptTaps()).toEqual([]))
    expect(
      await screen.findByRole('heading', { name: 'This tap is gone' }),
    ).toBeInTheDocument()
    await act(async () => undefined)
    expect(screen.getByRole('status')).toHaveTextContent(
      'Beacon does not have this tap. An answer is kept for 23 hours at most.',
    )
    expect(
      screen.getByText(repeats, { selector: 'p.meta' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: 'This tap is not asked for here' }),
    ).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/was not sent/)
    // Asked once, answered that it is gone, and not asked again.
    expect(tapRequests(asked)).toHaveLength(1)
  },
)

test('a server that takes a visitor’s own is asked for any tap, once it has said so', async () => {
  const asked = serve(fireRoutes)
  window.location.hash = `#/tap/${FIRE_TAP}`
  render(<App />)

  await screen.findByRole('heading', { level: 1, name: /Fire at/ })
  // The health was asked before the tap was.
  const keys = asked.map((request) => request.key)
  expect(keys.indexOf('GET /api/health')).toBeLessThan(
    keys.indexOf(`GET /api/taps/${FIRE_TAP}`),
  )
})

test('a tap that is gone says the most an answer is kept, and on a server that tells its story again, that', async () => {
  const repeats =
    'On this server the invented scenario is told again from its beginning when it is over.'
  serve({
    'GET /api/health': { ...MOCK_HEALTH, repeats_text: repeats },
    [`GET /api/taps/${FIRE_TAP}`]: () =>
      Response.json({ detail: 'There is no such tap.' }, { status: 404 }),
  })
  window.location.hash = `#/tap/${FIRE_TAP}`

  render(<App />)

  // No promise that an answer is kept for 23 hours: that is the most.
  expect(
    await screen.findByText(
      'Beacon does not have this tap. An answer is kept for 23 hours at most.',
    ),
  ).toBeInTheDocument()
  expect(await screen.findByText(repeats)).toBeInTheDocument()
})

test('takes no tap until the server has said whether it runs on real sources', async () => {
  // The server answers only when it is let: until then nobody has been told
  // whether a tap would go to the invented scenario.
  let answer: (response: Response) => void = () => undefined
  const asked = serve({
    'GET /api/health': () =>
      new Promise<Response>((resolve) => (answer = resolve)),
    'POST /api/taps': () => Response.json(receipt(FIRE_TAP), { status: 201 }),
  })
  render(<App />)

  const button = screen.getByRole('button', { name: ASK })
  expect(button).toBeDisabled()
  expect(
    screen.getByRole('checkbox', { name: '5 seconds of sound' }),
  ).toBeDisabled()
  click(button)
  expect(asked.filter((request) => request.key === 'POST /api/taps')).toEqual(
    [],
  )

  answer(Response.json(MOCK_HEALTH))
  expect(await screen.findByText(INVENTED)).toBeInTheDocument()
  expect(button).toBeEnabled()
})

test('takes no tap, and asks the phone nothing, from a server that has not said what it takes', async () => {
  // Not said is not `own`: the server may be one that takes only the invented
  // places, and a tap from where the phone is would be somebody's own.
  const silent: Record<string, unknown> = { ...MOCK_HEALTH }
  delete silent.input
  const asked = serve({ 'GET /api/health': silent })
  const located = vi.fn()
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: { getCurrentPosition: located },
  })
  render(<App />)

  await screen.findByText(INVENTED)
  expect(screen.getByRole('button', { name: ASK })).toBeDisabled()
  expect(
    screen.getByRole('button', {
      name: 'Near the invented fire, Hayes Valley',
    }),
  ).toBeDisabled()
  expect(
    screen.getByRole('checkbox', { name: '5 seconds of sound' }),
  ).toBeDisabled()
  expect(
    screen.getByText('Beacon takes a tap once it has heard from its server.'),
  ).toBeInTheDocument()
  expect(asked.filter((request) => request.key === 'POST /api/taps')).toEqual(
    [],
  )
  expect(located).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
})

test('takes no tap from a server that could not say which mode it is in', async () => {
  serve({
    'GET /api/health': () => Response.json({ detail: 'No.' }, { status: 500 }),
  })
  render(<App />)

  expect(await screen.findByText('No.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: ASK })).toBeDisabled()
  expect(screen.queryByText(INVENTED)).not.toBeInTheDocument()
})

test('offers no invented places and no invented label on a live server', async () => {
  serve({ 'GET /api/health': { ...MOCK_HEALTH, mode: 'live' } })

  render(<App />)

  expect(await screen.findByText('Server v1.2.3')).toBeInTheDocument()
  expect(screen.queryByText(/invented/i)).not.toBeInTheDocument()
})

test('stops taking taps when the server stops answering, and takes them again when it is back', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  let health: () => Response = () => Response.json(MOCK_HEALTH)
  serve({ 'GET /api/health': () => health() })
  render(<App />)
  const button = screen.getByRole('button', { name: ASK })
  await waitFor(() => expect(button).toBeEnabled())

  // The next asking fails. What the server said before is not carried over:
  // the one that comes back can be on other sources.
  health = () => Response.json({ detail: 'Down.' }, { status: 503 })
  await vi.advanceTimersByTimeAsync(30_100)
  await waitFor(() => expect(button).toBeDisabled())
  expect(screen.queryByText(INVENTED)).not.toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: /Near the invented fire/ }),
  ).not.toBeInTheDocument()

  // It is back, as a server on real sources.
  health = () => Response.json({ ...MOCK_HEALTH, mode: 'live' })
  await vi.advanceTimersByTimeAsync(5_100)
  await waitFor(() => expect(button).toBeEnabled())
  expect(screen.queryByText(/invented/i)).not.toBeInTheDocument()
})

test('a tap that was waiting when the server’s mode changed is called off, and sends nothing', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  let health: () => Response = () => Response.json(MOCK_HEALTH)
  const asked = serve({
    'GET /api/health': () => health(),
    'POST /api/taps': () => Response.json(receipt(FIRE_TAP), { status: 201 }),
  })
  // The phone says where it is only when it is let.
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
  const posts = () =>
    asked.filter((request) => request.key === 'POST /api/taps')
  render(<App />)
  const button = screen.getByRole('button', { name: ASK })
  await waitFor(() => expect(button).toBeEnabled())

  // Pressed while the server said it runs on the invented scenario.
  click(button)
  expect(await screen.findByText('Finding where you are…')).toBeInTheDocument()

  // Before the phone answers, the server is found to be on real sources.
  health = () => Response.json({ ...MOCK_HEALTH, mode: 'live' })
  await vi.advanceTimersByTimeAsync(30_100)
  await waitFor(() =>
    expect(screen.queryByText(/invented/i)).not.toBeInTheDocument(),
  )
  // The tap is called off: the button is as it was before it was pressed.
  expect(screen.queryByText('Finding where you are…')).not.toBeInTheDocument()
  await waitFor(() => expect(button).toBeEnabled())

  // The phone answers now. Nothing is sent to the server the tap was not for.
  found(here)
  await vi.advanceTimersByTimeAsync(1_000)
  expect(posts()).toEqual([])
  expect(keptTaps()).toEqual([])
  expect(window.location.hash).not.toContain('tap')

  // The same when the mode stops being known while a tap waits.
  click(button)
  expect(await screen.findByText('Finding where you are…')).toBeInTheDocument()
  health = () => Response.json({ detail: 'Down.' }, { status: 503 })
  await vi.advanceTimersByTimeAsync(30_100)
  await waitFor(() => expect(button).toBeDisabled())
  found(here)
  await vi.advanceTimersByTimeAsync(1_000)
  expect(posts()).toEqual([])
})

test('a tap the server refuses for being in another mode is said in its sentence, and the mode is asked for again at once', async () => {
  // The app was told the invented scenario. The server has since gone over to
  // real sources, and the app has not asked again yet.
  const refusal =
    'This server is not in the mode the tap’s sender was shown. Nothing of the tap was read or kept.'
  let health = MOCK_HEALTH
  const asked = serve({
    'GET /api/health': () => Response.json(health),
    'POST /api/taps': () => Response.json({ detail: refusal }, { status: 409 }),
  })
  render(<App />)
  const spot = await screen.findByRole('button', {
    name: 'Near the invented fire, Hayes Valley',
  })
  const healthAsks = () =>
    asked.filter((request) => request.key === 'GET /api/health').length
  expect(healthAsks()).toBe(1)
  health = { ...MOCK_HEALTH, mode: 'live' }

  click(spot)

  // The server's own sentence, and no tap kept.
  expect(await screen.findByRole('alert')).toHaveTextContent(refusal)
  const sent = asked.find((request) => request.key === 'POST /api/taps')
  expect(sent?.headers['beacon-mode']).toBe('mock')
  expect(keptTaps()).toEqual([])
  // Asked again now, not at the next half minute, and shown as it is: no
  // invented scenario, and none of its places to tap from.
  await waitFor(() => expect(healthAsks()).toBe(2))
  await waitFor(() =>
    expect(screen.queryByText(/invented/i)).not.toBeInTheDocument(),
  )
  expect(
    screen.queryByRole('button', { name: /Near the invented fire/ }),
  ).not.toBeInTheDocument()
  // Why the tap was not taken is still on the screen.
  expect(screen.getByRole('alert')).toHaveTextContent(refusal)
  expect(screen.getByRole('button', { name: ASK })).toBeEnabled()
})

test('a tap that ran out is taken off the list at that moment, though storage will not delete it', async () => {
  // The button's view alone, with the sweep the app starts: nothing else draws
  // the view again, so the list changes only if it is told that a tap ran out.
  vi.useFakeTimers()
  // Three quarters of a minute left of the 23 hours.
  const at = new Date(Date.now() - 23 * 60 * 60 * 1000 + 45_000).toISOString()
  keepTap({ id: QUIET_TAP, at, photo: false, clip: false })
  const refused = vi
    .spyOn(Storage.prototype, 'setItem')
    .mockImplementation(() => {
      throw new DOMException('Full.', 'QuotaExceededError')
    })
  const stop = sweepTaps()
  try {
    render(<Home mode="mock" input="own" />)
    const list = screen.getByRole('region', { name: 'Your taps' })
    expect(within(list).getAllByRole('link')).toHaveLength(1)

    await act(() => vi.advanceTimersByTimeAsync(44_000))
    expect(screen.getByRole('region', { name: 'Your taps' })).toBe(list)
    expect(refused).not.toHaveBeenCalled()
    await act(() => vi.advanceTimersByTimeAsync(2_000))

    expect(
      screen.queryByRole('region', { name: 'Your taps' }),
    ).not.toBeInTheDocument()
    expect(document.querySelector(`a[href*="${QUIET_TAP}"]`)).toBeNull()
    // Storage was asked once, at the moment the tap ran out, and still has it.
    expect(refused).toHaveBeenCalledTimes(1)
    expect(window.localStorage.getItem('beacon.taps.v1')).toContain(QUIET_TAP)
    // And not again on a timer.
    await act(() => vi.advanceTimersByTimeAsync(10_000))
    expect(refused).toHaveBeenCalledTimes(1)
  } finally {
    stop()
    refused.mockRestore()
  }
})

test('sends a tap, keeps its id on the device, and shows the incident it was matched to', async () => {
  const asked = serve({
    ...fireRoutes,
    'POST /api/taps': () => Response.json(receipt(FIRE_TAP), { status: 201 }),
  })
  render(<App />)

  click(
    await screen.findByRole('button', {
      name: 'Near the invented fire, Hayes Valley',
    }),
  )

  const title = await screen.findByRole('heading', {
    level: 1,
    name: 'Fire at the 400 block of Hayes St',
  })
  // The status, the match, and who investigated are the server's sentences.
  expect(title.closest('article')).toHaveTextContent(
    'The radio said: crews at work.',
  )
  expect(screen.getAllByText(fireAnswer.match!.text)).not.toHaveLength(0)
  expect(screen.getAllByText(fireAnswer.stand_in_text!)).not.toHaveLength(0)
  for (const ground of fireAnswer.match!.grounds)
    expect(screen.getByText(ground.text)).toBeInTheDocument()
  // The ground's name says what its check checked: open, or lately updated.
  expect(screen.getByText('Open or recently updated')).toBeInTheDocument()
  expect(screen.queryByText(/going on at the time/i)).not.toBeInTheDocument()
  // Only a place went with the tap: nothing was added to it.
  const sent = asked.find((request) => request.key === 'POST /api/taps')
  expect(sent?.body).toEqual({ location: { lat: 37.776, lon: -122.424 } })
  // The tap names the mode that was on the screen when it was made.
  expect(sent?.headers['beacon-mode']).toBe('mock')
  expect(keptTaps().map((tap) => tap.id)).toEqual([FIRE_TAP])
})

test('rounds the phone’s position to about a block before it is sent, and sends a clip only when asked', async () => {
  const asked = serve({
    ...fireRoutes,
    'POST /api/taps': () => Response.json(receipt(FIRE_TAP), { status: 201 }),
  })
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: {
      getCurrentPosition: (found: PositionCallback) =>
        found({
          coords: { latitude: 37.776412, longitude: -122.424391, accuracy: 8 },
        } as GeolocationPosition),
    },
  })
  render(<App />)

  await screen.findByText(INVENTED)
  click(screen.getByRole('checkbox', { name: '5 seconds of sound' }))
  click(screen.getByRole('button', { name: ASK }))

  await screen.findByRole('heading', { level: 1, name: /Fire at/ })
  const sent = asked.find((request) => request.key === 'POST /api/taps')
  expect(sent?.body).toEqual({
    location: { lat: 37.776, lon: -122.424, accuracy_m: 70 },
    clip: 'UklGRg==',
  })
})

test('shows why a tap was refused, in the server’s sentence', async () => {
  const refusal =
    'Beacon covers San Francisco, and this tap is from somewhere else.'
  serve({
    'GET /api/health': MOCK_HEALTH,
    'POST /api/taps': () => Response.json({ detail: refusal }, { status: 422 }),
  })
  render(<App />)

  click(await screen.findByRole('button', { name: 'Across town, North Beach' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(refusal)
  expect(keptTaps()).toEqual([])
})

test('goes on asking while a tap is being looked into, and then shows its answer', async () => {
  let asks = 0
  serve({
    'GET /api/health': MOCK_HEALTH,
    [`GET /api/taps/${QUIET_TAP}`]: () =>
      Response.json(
        ++asks === 1
          ? {
              ...quietAnswer,
              status: 'pending',
              reason: null,
              text: 'Beacon is looking into this tap.',
              watching: false,
            }
          : quietAnswer,
      ),
  })
  window.location.hash = `#/tap/${QUIET_TAP}`
  render(<App />)

  expect(
    await screen.findByText('Beacon is looking into this tap.'),
  ).toBeInTheDocument()
  // Thin evidence is said to be unconfirmed, with no incident shown as a guess.
  expect(
    await screen.findByRole(
      'heading',
      { level: 1, name: quietAnswer.text },
      { timeout: 3000 },
    ),
  ).toBeInTheDocument()
  expect(screen.queryByRole('article')).not.toBeInTheDocument()
  expect(screen.getByText(quietAnswer.stand_in_text!)).toBeInTheDocument()
})

test('shows crisis mode as the investigator’s decision, ahead of the tap’s own sections', async () => {
  serve(fireRoutes)
  window.location.hash = `#/tap/${FIRE_TAP}`
  render(<App />)

  const crisis = await screen.findByRole('region', {
    name: 'Crisis mode is on',
  })
  expect(crisis).toHaveTextContent(fireIncident.incident.crisis!.text)
  expect(crisis).toHaveTextContent(fireIncident.incident.crisis!.stand_in_text!)
  const match = screen.getByRole('region', { name: 'Why this incident' })
  expect(
    crisis.compareDocumentPosition(match) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy()
  expect(
    screen.getByRole('heading', { name: 'Crisis timeline' }),
  ).toBeInTheDocument()
})

test('shows an alert’s instruction as the authority’s words and a notice as Beacon’s reading beside its document', async () => {
  serve(fireRoutes)
  window.location.hash = `#/incident/${FIRE_ID}`
  render(<App />)

  const [alert, notice] = fireIncident.incident.official
  const alertItem = (
    await screen.findByRole('heading', { name: alert.type })
  ).closest('li')!
  expect(
    within(alertItem).getByText('The authority’s instruction, in its words'),
  ).toBeInTheDocument()
  expect(within(alertItem).getByText(alert.instruction!)).toBeInTheDocument()
  expect(within(alertItem).getByText('In force')).toBeInTheDocument()

  const noticeItem = screen
    .getByRole('heading', { name: 'Evacuation order' })
    .closest('li')!
  // A notice is a document that was published, and no more is said of it.
  expect(within(noticeItem).getByText('Published')).toBeInTheDocument()
  expect(within(noticeItem).queryByText('In force')).not.toBeInTheDocument()
  expect(
    within(noticeItem).getByText(/Beacon’s reading of the document, not the/),
  ).toBeInTheDocument()
  expect(
    within(noticeItem).getByText('Instruction, as read'),
  ).toBeInTheDocument()
  expect(
    within(noticeItem).getByRole('link', { name: 'Read the document' }),
  ).toHaveAttribute('href', notice.source.url)
  // What the redaction pass held back is said to be missing.
  expect(
    within(noticeItem).getByText(/left part of its reading out/),
  ).toBeInTheDocument()
})

test('shows 511’s credit beside every line that is from 511', async () => {
  serve(fireRoutes)
  window.location.hash = `#/incident/${FIRE_ID}`
  render(<App />)

  const timeline = await screen.findByRole('region', { name: /timeline/i })
  const from511 = fireIncident.incident.timeline.filter((entry) =>
    entry.sources.some((source) => source.source === '511'),
  )
  expect(from511.length).toBeGreaterThan(0)
  const credits = within(timeline).getAllByRole('link', {
    name: 'data provided by 511.org',
  })
  expect(credits).toHaveLength(from511.length)
  expect(credits[0]).toHaveAttribute('href', 'https://511.org')
})

// The notice adsb.lol's license asks for, as the server gives it for a position
// adsb.lol answered with: its words in pieces, three of them links.
const SKY_CREDIT = {
  text: "Contains information from adsb.lol, which is made available here under the Open Database License (ODbL). Beacon's aircraft rows are under the same license: what Beacon keeps of adsb.lol's data, and how.",
  url: 'https://www.adsb.lol',
  parts: [
    { text: 'Contains information from ', url: null },
    { text: 'adsb.lol', url: 'https://www.adsb.lol' },
    { text: ', which is made available here under the ', url: null },
    {
      text: 'Open Database License (ODbL)',
      url: 'https://opendatacommons.org/licenses/odbl/1-0/',
    },
    {
      text: ". Beacon's aircraft rows are under the same license: ",
      url: null,
    },
    {
      text: "what Beacon keeps of adsb.lol's data, and how",
      url: 'https://github.com/andrewtea/beacon/blob/main/docs/aircraft-data.md',
    },
    { text: '.', url: null },
  ],
}

test('the invented scenario’s aircraft are not said to contain information from adsb.lol', async () => {
  serve(fireRoutes)
  window.location.hash = `#/incident/${FIRE_ID}`
  render(<App />)

  const timeline = await screen.findByRole('region', { name: /timeline/i })
  const fromTheSky = fireIncident.incident.timeline.filter((entry) =>
    entry.sources.some((source) => source.kind === 'aircraft'),
  )
  expect(fromTheSky.length).toBeGreaterThan(0)
  expect(
    within(timeline).getAllByText('Aircraft positions').length,
  ).toBeGreaterThan(0)
  expect(screen.queryByText(/adsb\.lol/)).not.toBeInTheDocument()
})

test('the notice adsb.lol’s license asks for links the database, the license, and how Beacon’s rows differ, each to its own address', async () => {
  const incident = {
    ...fireIncident.incident,
    timeline: fireIncident.incident.timeline.map((entry) => ({
      ...entry,
      sources: entry.sources.map((source) =>
        source.kind === 'aircraft'
          ? {
              ...source,
              label: 'adsb.lol aircraft positions',
              stand_in: false,
              credit: SKY_CREDIT,
            }
          : source,
      ),
    })),
  }
  serve({
    ...fireRoutes,
    [`GET /api/incidents/${FIRE_ID}`]: { ...fireIncident, incident },
  })
  window.location.hash = `#/incident/${FIRE_ID}`
  render(<App />)

  const timeline = await screen.findByRole('region', { name: /timeline/i })
  const fromTheSky = incident.timeline.filter((entry) =>
    entry.sources.some((source) => source.kind === 'aircraft'),
  )
  expect(fromTheSky.length).toBeGreaterThan(0)
  for (const [name, address] of [
    ['adsb.lol', 'https://www.adsb.lol'],
    [
      'Open Database License (ODbL)',
      'https://opendatacommons.org/licenses/odbl/1-0/',
    ],
    [
      "what Beacon keeps of adsb.lol's data, and how",
      'https://github.com/andrewtea/beacon/blob/main/docs/aircraft-data.md',
    ],
  ]) {
    const links = within(timeline).getAllByRole('link', { name })
    expect(links.length).toBeGreaterThanOrEqual(fromTheSky.length)
    for (const link of links) expect(link).toHaveAttribute('href', address)
  }
  // The whole notice is there, the words that are no link included.
  const notice = within(timeline)
    .getAllByRole('link', { name: 'adsb.lol' })[0]
    .closest('.credit')
  expect(notice).toHaveTextContent(SKY_CREDIT.text)
})

test('the trace shows the credit of what the grounds of a match rested on', async () => {
  serve({
    ...fireRoutes,
    [`GET /api/taps/${FIRE_TAP}/run`]: {
      ...fireRun,
      outcome: { ...fireRun.outcome, credits: [SKY_CREDIT] },
    },
  })
  window.location.hash = `#/tap/${FIRE_TAP}/trace`
  render(<App />)

  await screen.findByRole('heading', { name: 'Totals' })
  expect(
    screen.getByRole('link', { name: 'Open Database License (ODbL)' }),
  ).toHaveAttribute('href', 'https://opendatacommons.org/licenses/odbl/1-0/')
})

test('the trace shows each tool call in the server’s words and never calls the stand-in Muse Ember', async () => {
  serve(fireRoutes)
  window.location.hash = `#/tap/${FIRE_TAP}/trace`
  render(<App />)

  await screen.findByRole('heading', { name: 'Totals' })
  for (const round of fireRun.rounds)
    for (const call of round.calls) {
      expect(screen.getAllByText(call.text)).not.toHaveLength(0)
      expect(screen.getAllByText(call.returned_text)).not.toHaveLength(0)
    }
  const answerers = screen
    .getAllByText('Answered by')
    .map((label) => label.nextElementSibling?.textContent)
  expect(answerers).toHaveLength(fireRun.rounds.length + 1)
  expect(new Set(answerers)).toEqual(new Set(['The stand-in']))
  expect(screen.getByText(fireRun.stand_in_text!)).toBeInTheDocument()
  // The calls listed are the ones that were kept, which a round can have asked
  // for more than.
  expect(
    screen.getAllByText(/tool calls? of this round (was|were) kept/),
  ).toHaveLength(fireRun.rounds.length)
  expect(screen.queryByText(/came back asking for/)).not.toBeInTheDocument()
  // The count of closures a tool returned is 511's, and says so.
  expect(
    screen.getByRole('link', { name: 'data provided by 511.org' }),
  ).toBeInTheDocument()
})

test('forgets a tap on the device once the server no longer has it', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH })
  keepTap({
    id: QUIET_TAP,
    at: new Date().toISOString(),
    photo: false,
    clip: false,
  })
  window.location.hash = `#/tap/${QUIET_TAP}`
  render(<App />)

  expect(
    await screen.findByRole('heading', { name: 'This tap is gone' }),
  ).toBeInTheDocument()
  await waitFor(() => expect(keptTaps()).toEqual([]))
})

test('a medical response is shown as its crews and its block, and nothing else', () => {
  const medical = fireAnswer.nearby[0]
  expect(medical.category).toBe('medical response')

  const { container } = render(
    <IncidentCard
      incident={{
        ...medical,
        open: false,
        alarm: 2,
        taps: 4,
        status_text: 'The radio said: units cleared.',
      }}
    />,
  )

  expect(screen.getByText(medical.title)).toBeInTheDocument()
  expect(screen.getByText(medical.place!)).toBeInTheDocument()
  for (const unit of medical.units)
    expect(screen.getByText(unit)).toBeInTheDocument()
  // Where it is from is kept: the calls it rests on, and how far to trust them.
  expect(screen.getByText('Medium confidence')).toBeInTheDocument()
  expect(screen.getByText(medical.radio!.sources[0].label)).toBeInTheDocument()
  // Nothing else of it is shown, whatever the route sent: no alarm, no taps,
  // no status, no time, and not whether its record is open.
  const card = screen.getByRole('article')
  expect(card).not.toHaveTextContent(/alarm/i)
  expect(card).not.toHaveTextContent(/taps/i)
  expect(card).not.toHaveTextContent(/cleared/i)
  expect(card).not.toHaveTextContent(/record/i)
  expect(card).not.toHaveTextContent(/first radio call/i)
  expect(container.querySelector('time')).toBeNull()
  expect(card).not.toHaveClass('over')
})

test('a medical response’s own page is its card, with no timeline and no time', async () => {
  // Whatever the route sends of one: here, everything the fire has.
  serve({
    'GET /api/health': MOCK_HEALTH,
    [`GET /api/incidents/${FIRE_ID}`]: {
      ...fireIncident,
      incident: { ...fireIncident.incident, category: 'medical response' },
    },
  })
  window.location.hash = `#/incident/${FIRE_ID}`
  const { container } = render(<App />)

  expect(
    await screen.findByText(/shows a medical response as its crews/),
  ).toBeInTheDocument()
  expect(screen.getByRole('article')).toHaveTextContent(
    fireIncident.incident.place!,
  )
  expect(screen.queryByText(/timeline/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/Crisis mode/)).not.toBeInTheDocument()
  expect(screen.queryByText('From authorities')).not.toBeInTheDocument()
  expect(container.querySelector('time')).toBeNull()
})

test('a card says how Beacon’s record stands, and shows only what the radio gave, with the radio as its source', () => {
  const fire = fireAnswer.incident!
  expect(fire.taps).toBeGreaterThan(0)

  const { rerender } = render(<IncidentCard incident={fire} />)

  const card = screen.getByRole('article')
  expect(card).toHaveTextContent('Open in Beacon’s record')
  expect(card).toHaveTextContent('Medium confidence')
  expect(
    within(card).getAllByText(fire.radio!.sources[0].label),
  ).not.toHaveLength(0)
  // How many taps came and when the newest line is from are not the radio's,
  // and are left to the timeline, where each has its own source.
  expect(card).not.toHaveTextContent(/taps/i)
  expect(card).not.toHaveTextContent(/latest update/i)

  // A record that is closed is said to be that, and nothing is said of the
  // place: the radio can only have gone quiet.
  rerender(<IncidentCard incident={{ ...fire, open: false }} />)
  expect(card).toHaveTextContent('Closed in Beacon’s record')
  expect(card).not.toHaveTextContent(/going on/i)
})

test('crisis mode that is no longer on is said to have been on, and the app says nothing of the place', () => {
  const crisis = { ...fireIncident.incident.crisis!, active: false }

  render(<CrisisPanel crisis={crisis} />)

  const panel = screen.getByRole('region', { name: 'Crisis mode was on' })
  expect(panel).toHaveTextContent(crisis.text)
  expect(panel).not.toHaveTextContent(/going on/i)
})

test('shows nothing of an incident the server says it does not have, and keeps the tap’s match', async () => {
  // The tap's answer still names the incident; the incident's own route does not.
  serve({
    'GET /api/health': MOCK_HEALTH,
    [`GET /api/taps/${FIRE_TAP}`]: fireAnswer,
  })
  window.location.hash = `#/tap/${FIRE_TAP}`
  render(<App />)

  expect(
    await screen.findByRole('heading', {
      name: 'Beacon has no incident at this address',
    }),
  ).toBeInTheDocument()
  // No card of the incident from the tap's own answer, and none of its crisis.
  expect(
    screen.queryByRole('heading', { name: fireAnswer.incident!.title }),
  ).not.toBeInTheDocument()
  expect(screen.queryByText(/Crisis mode/)).not.toBeInTheDocument()
  expect(
    screen.getByRole('region', { name: 'Why this incident' }),
  ).toHaveTextContent(fireAnswer.match!.text)
})

test('takes an incident off the screen when the server stops having it', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  let gone = false
  serve({
    'GET /api/health': MOCK_HEALTH,
    [`GET /api/incidents/${FIRE_ID}`]: () =>
      gone
        ? Response.json(
            { detail: 'There is no such incident.' },
            { status: 404 },
          )
        : Response.json(fireIncident),
  })
  window.location.hash = `#/incident/${FIRE_ID}`
  render(<App />)

  expect(
    await screen.findByRole('region', { name: 'Crisis mode is on' }),
  ).toBeInTheDocument()
  expect(screen.getByText('In force')).toBeInTheDocument()

  gone = true
  await vi.advanceTimersByTimeAsync(OPEN_MS + 100)

  expect(
    await screen.findByRole('heading', {
      name: 'Beacon has no incident at this address',
    }),
  ).toBeInTheDocument()
  // What was read before is not left standing with nobody checking it.
  expect(screen.queryByText(/Crisis mode/)).not.toBeInTheDocument()
  expect(screen.queryByText('In force')).not.toBeInTheDocument()
  expect(screen.queryByRole('article')).not.toBeInTheDocument()
})

test('a story chosen from an earlier reading is said to be that, with no claim about which lines are newer', async () => {
  const story = fireIncident.incident.story!
  serve({
    ...fireRoutes,
    [`GET /api/incidents/${FIRE_ID}`]: {
      ...fireIncident,
      incident: {
        ...fireIncident.incident,
        story: { ...story, current: false, left_out: 2 },
      },
    },
  })
  window.location.hash = `#/incident/${FIRE_ID}`
  render(<App />)

  const points = await screen.findByRole('region', { name: 'Key points' })
  expect(points).toHaveTextContent(/at an earlier reading/)
  expect(points).toHaveTextContent(
    '2 more points that were chosen are not shown.',
  )
  expect(points).not.toHaveTextContent(/newer/)
  expect(points).not.toHaveTextContent(/did not show/)
})

test('a run whose totals may be incomplete says so in the server’s sentence, and claims no request went missing', async () => {
  const cost = 'These totals may be incomplete: the server’s own sentence.'
  serve({
    ...fireRoutes,
    [`GET /api/taps/${FIRE_TAP}/run`]: {
      ...fireRun,
      unaccounted_calls: 1,
      cost_incomplete: true,
      cost_text: cost,
    },
  })
  window.location.hash = `#/tap/${FIRE_TAP}/trace`
  render(<App />)

  const totals = await screen.findByRole('region', { name: 'Totals' })
  expect(totals).toHaveTextContent(cost)
  expect(totals).toHaveTextContent('Requests answered')
  expect(totals).not.toHaveTextContent(/not in these totals/)
})

test('takes no tap and no second picture while a chosen photo is still being drawn', async () => {
  let drawn: (photo: Blob) => void = () => undefined
  vi.mocked(drawPhoto).mockReturnValueOnce(
    new Promise<Blob>((resolve) => (drawn = resolve)),
  )
  const asked = serve({
    ...fireRoutes,
    'POST /api/taps': () => Response.json(receipt(FIRE_TAP), { status: 201 }),
  })
  const { container } = render(<App />)
  await screen.findByText(INVENTED)

  const picker = container.querySelector<HTMLInputElement>('input[type=file]')!
  fireEvent.change(picker, {
    target: { files: [new File(['raw'], 'a.jpg', { type: 'image/jpeg' })] },
  })

  // Until the picture is drawn there is no photo to send: a tap now would go
  // without it.
  const button = screen.getByRole('button', { name: ASK })
  await waitFor(() => expect(button).toBeDisabled())
  expect(screen.getByRole('button', { name: 'A photo' })).toBeDisabled()
  const spot = screen.getByRole('button', {
    name: 'Near the invented fire, Hayes Valley',
  })
  expect(spot).toBeDisabled()
  click(button)
  click(spot)
  expect(asked.some((request) => request.key === 'POST /api/taps')).toBe(false)

  drawn(new Blob([new Uint8Array([255, 216, 255])], { type: 'image/jpeg' }))
  await screen.findByRole('button', { name: 'Photo attached. Remove it' })
  click(
    screen.getByRole('button', {
      name: 'Near the invented fire, Hayes Valley',
    }),
  )

  await screen.findByRole('heading', { level: 1, name: /Fire at/ })
  const sent = asked.find((request) => request.key === 'POST /api/taps')
  expect(sent?.body).toMatchObject({ photo: '/9j/' })
})

test('incidents listed because of where they are carry 511’s credit when the place is 511’s', async () => {
  const credit = {
    text: 'data provided by 511.org',
    url: 'https://511.org',
    parts: [],
  }
  const since = { ...fireAnswer.incident!, crisis: null }
  serve({
    'GET /api/health': MOCK_HEALTH,
    [`GET /api/taps/${FIRE_TAP}`]: {
      ...fireAnswer,
      nearby_credit: credit,
    },
    [`GET /api/taps/${QUIET_TAP}`]: {
      ...quietAnswer,
      watching: true,
      since: [since],
      since_text: 'The server’s sentence about what began since.',
      since_credit: credit,
    },
    [`GET /api/incidents/${FIRE_ID}`]: fireIncident,
  })
  window.location.hash = `#/tap/${FIRE_TAP}`
  const { unmount } = render(<App />)

  const nearby = await screen.findByRole('region', { name: 'Also nearby' })
  expect(nearby).toHaveTextContent(fireAnswer.nearby_text!)
  expect(
    within(nearby).getByRole('link', { name: credit.text }),
  ).toHaveAttribute('href', credit.url)

  unmount()
  window.location.hash = `#/tap/${QUIET_TAP}`
  render(<App />)
  const began = await screen.findByRole('region', {
    name: 'Began near here since your tap',
  })
  expect(
    within(began).getByRole('link', { name: credit.text }),
  ).toHaveAttribute('href', credit.url)
})

test('a section of nearby incidents with no place of 511’s shows no credit', async () => {
  serve(fireRoutes)
  window.location.hash = `#/tap/${FIRE_TAP}`
  render(<App />)

  const nearby = await screen.findByRole('region', { name: 'Also nearby' })
  expect(fireAnswer.nearby_credit).toBeNull()
  expect(
    within(nearby).queryByRole('link', { name: /511/ }),
  ).not.toBeInTheDocument()
})

test('a source is linked only at a web address, and its credit is shown either way', () => {
  const source = {
    ...fireIncident.incident.official[0].source,
    url: 'javascript:alert(1)',
    credit: {
      text: 'data provided by 511.org',
      url: 'javascript:alert(2)',
      parts: [],
    },
  }

  render(<Sources sources={[source]} />)

  expect(screen.getByText(source.label)).toBeInTheDocument()
  expect(screen.getByText('data provided by 511.org')).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})
