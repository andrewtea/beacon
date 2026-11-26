import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from './App.tsx'
import { IncidentCard } from './components/IncidentCard.tsx'
import { Sources } from './components/Provenance.tsx'
import { keepTap, keptTaps } from './lib/tapStore.ts'
import {
  FIRE_TAP,
  fireAnswer,
  fireIncident,
  fireRun,
  QUIET_TAP,
  quietAnswer,
} from './test/fixtures.ts'
import { MOCK_HEALTH, serve } from './test/server.ts'

vi.mock('./lib/recorder.ts', () => ({
  CLIP_SECONDS: 5,
  ClipError: class extends Error {},
  // A clip of four bytes, in place of five seconds of a microphone.
  recordClip: vi.fn(async () => new Uint8Array([82, 73, 70, 70])),
}))

const FIRE_ID = fireIncident.incident.id
const fireRoutes = {
  'GET /api/health': MOCK_HEALTH,
  [`GET /api/taps/${FIRE_TAP}`]: fireAnswer,
  [`GET /api/taps/${FIRE_TAP}/run`]: fireRun,
  [`GET /api/incidents/${FIRE_ID}`]: fireIncident,
}
const receipt = (id: string) => ({
  id,
  received_at: new Date().toISOString(),
  mode: 'mock',
  photo: false,
  clip: false,
  media_kept_until: null,
  investigating: true,
})

beforeEach(() => {
  window.localStorage.clear()
  window.location.hash = ''
})

const click = (element: HTMLElement) => element.click()

test('says that it runs on an invented scenario before anybody taps', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH })

  render(<App />)

  expect(
    await screen.findByText(
      /running on an invented scenario, not real sources/,
    ),
  ).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'What’s that?' }),
  ).toBeInTheDocument()
})

test('offers no invented places and no invented label on a live server', async () => {
  serve({ 'GET /api/health': { ...MOCK_HEALTH, mode: 'live' } })

  render(<App />)

  expect(await screen.findByText('Server v1.2.3')).toBeInTheDocument()
  expect(screen.queryByText(/invented/i)).not.toBeInTheDocument()
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

  expect(
    await screen.findByRole('heading', {
      level: 1,
      name: 'Fire at the 400 block of Hayes St',
    }),
  ).toBeInTheDocument()
  // The status, the match, and who investigated are the server's sentences.
  expect(screen.getByText('The radio said: crews at work.')).toBeInTheDocument()
  expect(screen.getAllByText(fireAnswer.match!.text)).not.toHaveLength(0)
  expect(screen.getAllByText(fireAnswer.stand_in_text!)).not.toHaveLength(0)
  for (const ground of fireAnswer.match!.grounds)
    expect(screen.getByText(ground.text)).toBeInTheDocument()
  // Only a place went with the tap: nothing was added to it.
  const sent = asked.find((request) => request.key === 'POST /api/taps')
  expect(sent?.body).toEqual({ location: { lat: 37.776, lon: -122.424 } })
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

  click(await screen.findByRole('checkbox', { name: '5 seconds of sound' }))
  click(screen.getByRole('button', { name: 'What’s that?' }))

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
    entry.sources.some((source) => source.credit !== null),
  )
  expect(from511.length).toBeGreaterThan(0)
  const credits = within(timeline).getAllByRole('link', {
    name: 'data provided by 511.org',
  })
  expect(credits).toHaveLength(from511.length)
  expect(credits[0]).toHaveAttribute('href', 'https://511.org')
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

  render(
    <IncidentCard
      incident={{ ...medical, alarm: 2, taps: 4, status_text: null }}
    />,
  )

  expect(screen.getByText(medical.place!)).toBeInTheDocument()
  for (const unit of medical.units)
    expect(screen.getByText(unit)).toBeInTheDocument()
  expect(screen.queryByText(/alarm/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/taps/i)).not.toBeInTheDocument()
})

test('a source is linked only at a web address, and its credit is shown either way', () => {
  const source = {
    ...fireIncident.incident.official[0].source,
    url: 'javascript:alert(1)',
    credit: { text: 'data provided by 511.org', url: 'javascript:alert(2)' },
  }

  render(<Sources sources={[source]} />)

  expect(screen.getByText(source.label)).toBeInTheDocument()
  expect(screen.getByText('data provided by 511.org')).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})
