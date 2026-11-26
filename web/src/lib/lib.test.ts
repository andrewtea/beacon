import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireAnswer, quietAnswer } from '../test/fixtures.ts'
import { OPEN_MS, OVER_MS, PENDING_MS, tapCadence } from './cadence.ts'
import { rounded } from './geo.ts'
import { withoutTapIds } from './logging.ts'
import { fitted } from './photo.ts'
import { recordClip } from './recorder.ts'
import { href, parseRoute } from './router.ts'
import {
  forgetTap,
  KEPT_MS,
  keepTap,
  keptTaps,
  pruneTaps,
  sweepTaps,
} from './tapStore.ts'
import { encodeWav, toBase64 } from './wav.ts'

beforeEach(() => window.localStorage.clear())
afterEach(() => vi.useRealTimers())

const stored = (): string[] =>
  (
    JSON.parse(window.localStorage.getItem('beacon.taps.v1') ?? '[]') as {
      id: string
    }[]
  ).map((tap) => tap.id)

test('a route is read from the fragment, and an id that is not one names nothing', () => {
  expect(parseRoute('')).toEqual({ view: 'home' })
  expect(parseRoute('#/')).toEqual({ view: 'home' })
  expect(parseRoute(href.tap('8JZ3tRoy4O9puB0n'))).toEqual({
    view: 'tap',
    id: '8JZ3tRoy4O9puB0n',
  })
  expect(parseRoute(href.trace('a-b_c'))).toEqual({
    view: 'trace',
    id: 'a-b_c',
  })
  expect(parseRoute(href.incident('inc-0942f75930378016'))).toEqual({
    view: 'incident',
    id: 'inc-0942f75930378016',
  })
  expect(parseRoute('#/tap/a%2Fb')).toEqual({ view: 'unknown' })
  expect(parseRoute('#/tap/abc/elsewhere')).toEqual({ view: 'unknown' })
  expect(parseRoute('#/incident/abc/trace')).toEqual({ view: 'unknown' })
})

test('a position is rounded to three decimals, and claims no finer accuracy than that', () => {
  expect(rounded({ lat: 37.776449, lon: -122.42351, accuracy_m: 5 })).toEqual({
    lat: 37.776,
    lon: -122.424,
    accuracy_m: 70,
  })
  expect(rounded({ lat: 37.7, lon: -122.4, accuracy_m: 350.4 })).toEqual({
    lat: 37.7,
    lon: -122.4,
    accuracy_m: 350,
  })
  expect(rounded({ lat: 37.7, lon: -122.4 })).toEqual({
    lat: 37.7,
    lon: -122.4,
  })
})

test('a clip is written as a mono 16-bit WAV file of its samples', () => {
  const file = encodeWav(new Float32Array([0, 1, -1, 0.5, 2]), 16_000)
  const view = new DataView(file.buffer)
  const ascii = (offset: number) =>
    String.fromCharCode(...file.subarray(offset, offset + 4))

  expect([ascii(0), ascii(8), ascii(12), ascii(36)]).toEqual([
    'RIFF',
    'WAVE',
    'fmt ',
    'data',
  ])
  expect(file.length).toBe(44 + 5 * 2)
  expect(view.getUint32(4, true)).toBe(file.length - 8)
  expect(view.getUint16(20, true)).toBe(1) // plain samples
  expect(view.getUint16(22, true)).toBe(1) // one channel
  expect(view.getUint32(24, true)).toBe(16_000)
  expect(view.getUint32(28, true)).toBe(32_000)
  expect(view.getUint16(34, true)).toBe(16)
  expect(view.getUint32(40, true)).toBe(10)
  // A sample louder than full scale is held to it, not wrapped round.
  expect(
    [0, 1, 2, 3, 4].map((index) => view.getInt16(44 + index * 2, true)),
  ).toEqual([0, 32767, -32767, 16384, 32767])
})

test('a microphone that runs faster than a clip may is thinned to a rate the server takes', () => {
  const file = encodeWav(new Float32Array(960), 96_000)
  const view = new DataView(file.buffer)

  expect(view.getUint32(24, true)).toBe(48_000)
  expect(view.getUint32(40, true)).toBe(480 * 2)
})

test('bytes are put in base64 whatever their number', () => {
  expect(toBase64(new Uint8Array([82, 73, 70, 70]))).toBe('UklGRg==')
  const long = new Uint8Array(100_000).fill(255)
  expect(atob(toBase64(long))).toHaveLength(100_000)
})

test('a photo is drawn no larger than 2048 on a side, in its own proportions', () => {
  expect(fitted(4032, 3024)).toEqual({ width: 2048, height: 1536 })
  expect(fitted(3024, 4032)).toEqual({ width: 1536, height: 2048 })
  expect(fitted(800, 600)).toEqual({ width: 800, height: 600 })
})

test('a tap is kept on the device for as long as the server answers for it', () => {
  const now = Date.parse('2026-11-26T12:00:00Z')
  const at = (msAgo: number) => new Date(now - msAgo).toISOString()
  keepTap({ id: 'old', at: at(KEPT_MS + 1), photo: false, clip: false }, now)
  keepTap(
    { id: 'early', at: at(KEPT_MS - 60_000), photo: false, clip: true },
    now,
  )
  keepTap({ id: 'late', at: at(1_000), photo: true, clip: false }, now)

  expect(keptTaps(now).map((tap) => tap.id)).toEqual(['late', 'early'])
  // Kept again, a tap is listed once.
  keepTap(
    { id: 'early', at: at(KEPT_MS - 60_000), photo: false, clip: true },
    now,
  )
  expect(keptTaps(now).map((tap) => tap.id)).toEqual(['early', 'late'])
  forgetTap('early')
  expect(keptTaps(now).map((tap) => tap.id)).toEqual(['late'])
})

test('what is not a list of taps in storage is read as none', () => {
  window.localStorage.setItem('beacon.taps.v1', '{"id": 1}')
  expect(keptTaps()).toEqual([])
  window.localStorage.setItem('beacon.taps.v1', 'not json')
  expect(keptTaps()).toEqual([])
})

test('a tap’s answer is asked for again while it can change, and not after', () => {
  expect(tapCadence({ ...quietAnswer, status: 'pending' })).toBe(PENDING_MS)
  expect(tapCadence(fireAnswer)).toBe(OPEN_MS)
  expect(
    tapCadence({
      ...fireAnswer,
      incident: { ...fireAnswer.incident!, open: false },
    }),
  ).toBe(OVER_MS)
  expect(tapCadence({ ...quietAnswer, watching: false })).toBeNull()
  expect(tapCadence({ ...quietAnswer, watching: true })).not.toBeNull()
})

test('a tap the server no longer answers for is deleted from storage, not only hidden', () => {
  const now = Date.parse('2026-11-26T12:00:00Z')
  const at = (msAgo: number) => new Date(now - msAgo).toISOString()
  const tap = (id: string, msAgo: number) => ({
    id,
    at: at(msAgo),
    photo: false,
    clip: false,
  })
  window.localStorage.setItem(
    'beacon.taps.v1',
    JSON.stringify([tap('fresh', 1_000), tap('stale', KEPT_MS + 1)]),
  )
  expect(stored()).toEqual(['fresh', 'stale'])

  expect(pruneTaps(now)).toBe(true)

  expect(stored()).toEqual(['fresh'])
  expect(pruneTaps(now)).toBe(false)
})

test('an open app deletes each tap from storage at the moment it runs out', () => {
  vi.useFakeTimers()
  const now = Date.parse('2026-11-26T12:00:00Z')
  vi.setSystemTime(now)
  const at = (msAgo: number) => new Date(now - msAgo).toISOString()
  const tap = (id: string, msAgo: number) => ({
    id,
    at: at(msAgo),
    photo: false,
    clip: false,
  })
  window.localStorage.setItem(
    'beacon.taps.v1',
    JSON.stringify([
      tap('new', 0),
      tap('soon', KEPT_MS - 60_000),
      tap('stale', KEPT_MS + 60_000),
    ]),
  )

  const stop = sweepTaps()
  // At the start, what had already run out is gone.
  expect(stored()).toEqual(['new', 'soon'])

  vi.advanceTimersByTime(59_000)
  expect(stored()).toEqual(['new', 'soon'])
  vi.advanceTimersByTime(2_000)
  expect(stored()).toEqual(['new'])

  // A tap kept while the app is open is swept in its turn.
  keepTap(tap('later', KEPT_MS - 5_000))
  vi.advanceTimersByTime(6_000)
  expect(stored()).toEqual(['new'])

  stop()
  vi.advanceTimersByTime(KEPT_MS)
  expect(stored()).toEqual(['new'])
})

test('a path of a tap’s own routes is written to a log without the tap’s id', () => {
  expect(
    withoutTapIds('http proxy error: /api/taps/f7fClvPJ0K8LF47x\nError: x'),
  ).toBe('http proxy error: /api/taps/[left out]\nError: x')
  expect(withoutTapIds('GET /api/taps/f7fClvPJ0K8LF47x/run 502')).toBe(
    'GET /api/taps/[left out] 502',
  )
  expect(withoutTapIds('/api/taps%2Ff7fClvPJ0K8LF47x')).toBe(
    '/api/taps/[left out]',
  )
  // Nothing else is touched: the route that takes a tap names none.
  expect(withoutTapIds('http proxy error: /api/taps')).toBe(
    'http proxy error: /api/taps',
  )
  expect(withoutTapIds('http proxy error: /api/incidents/inc-1')).toBe(
    'http proxy error: /api/incidents/inc-1',
  )
})

function microphone() {
  const stop = vi.fn()
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop }] }))
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.stubGlobal('AudioWorkletNode', class {})
  return { stop, getUserMedia }
}

test('the microphone is let go of when the recording cannot even begin', async () => {
  const { stop } = microphone()
  vi.stubGlobal(
    'AudioContext',
    class {
      constructor() {
        throw new Error('No audio device.')
      }
    },
  )

  await expect(recordClip(1)).rejects.toThrow('Beacon could not record a clip.')

  expect(stop).toHaveBeenCalledTimes(1)
})

test('a tap called off while the microphone was being asked for records nothing and lets go of it', async () => {
  const { stop } = microphone()
  const made = vi.fn()
  vi.stubGlobal(
    'AudioContext',
    class {
      constructor() {
        made()
      }
    },
  )
  const controller = new AbortController()

  const recording = recordClip(1, controller.signal)
  controller.abort()

  await expect(recording).rejects.toMatchObject({ name: 'AbortError' })
  expect(stop).toHaveBeenCalledTimes(1)
  expect(made).not.toHaveBeenCalled()
})
