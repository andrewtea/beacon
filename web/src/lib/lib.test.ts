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
  isInventedTap,
  KEPT_MS,
  keepTap,
  keptTaps,
  pruneTaps,
  resetTaps,
  subscribeTaps,
  sweepTaps,
  tapsSnapshot,
} from './tapStore.ts'
import { encodeWav, toBase64 } from './wav.ts'

beforeEach(() => window.localStorage.clear())
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

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

  expect(pruneTaps(now)).toBe('deleted')

  expect(stored()).toEqual(['fresh'])
  expect(pruneTaps(now)).toBe('nothing')
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

  // A tap kept while the app is open, with five seconds left, is swept in its
  // turn.
  keepTap({
    id: 'later',
    at: new Date(Date.now() - KEPT_MS + 5_000).toISOString(),
    photo: false,
    clip: false,
  })
  vi.advanceTimersByTime(4_000)
  expect(stored()).toEqual(['later', 'new'])
  vi.advanceTimersByTime(2_000)
  expect(stored()).toEqual(['new'])

  stop()
  vi.advanceTimersByTime(KEPT_MS)
  expect(stored()).toEqual(['new'])
})

test('storage that refuses a write is not asked again every millisecond', () => {
  vi.useFakeTimers()
  const now = Date.parse('2026-11-26T12:00:00Z')
  vi.setSystemTime(now)
  window.localStorage.setItem(
    'beacon.taps.v1',
    JSON.stringify([
      {
        id: 'stale',
        at: new Date(now - KEPT_MS - 1).toISOString(),
        photo: false,
        clip: false,
      },
    ]),
  )
  const refused = vi
    .spyOn(Storage.prototype, 'setItem')
    .mockImplementation(() => {
      throw new DOMException('Full.', 'QuotaExceededError')
    })
  expect(pruneTaps(now)).toBe('failed')
  refused.mockClear()

  const stop = sweepTaps()
  vi.advanceTimersByTime(1_000)

  // One try when the app started, and none on a timer after it failed.
  expect(refused).toHaveBeenCalledTimes(1)
  // The tap that ran out is not shown, though it could not be deleted.
  expect(keptTaps()).toEqual([])
  expect(tapsSnapshot()).toEqual([])
  // Looking at the page again is one more try, which storage now takes.
  refused.mockRestore()
  document.dispatchEvent(new Event('visibilitychange'))
  expect(stored()).toEqual([])
  stop()
})

test('a tap kept or forgotten in another tab is known here, and is swept here when it runs out', () => {
  vi.useFakeTimers()
  const now = Date.parse('2026-11-26T12:00:00Z')
  vi.setSystemTime(now)
  const stop = sweepTaps()
  const told = vi.fn()
  const unsubscribe = subscribeTaps(told)
  const written = vi.spyOn(Storage.prototype, 'setItem')

  // Another tab keeps a tap that has a minute left. The browser changes the
  // storage both tabs share, and tells this one.
  const other = [
    {
      id: 'theirs',
      at: new Date(now - KEPT_MS + 60_000).toISOString(),
      photo: false,
      clip: false,
    },
  ]
  written.mockRestore()
  window.localStorage.setItem('beacon.taps.v1', JSON.stringify(other))
  const writes = vi.spyOn(Storage.prototype, 'setItem')
  window.dispatchEvent(new StorageEvent('storage', { key: 'beacon.taps.v1' }))

  expect(told).toHaveBeenCalledTimes(1)
  expect(keptTaps().map((tap) => tap.id)).toEqual(['theirs'])
  // Being told writes nothing back.
  expect(writes).not.toHaveBeenCalled()
  // A change to another key is none of this.
  window.dispatchEvent(new StorageEvent('storage', { key: 'something.else' }))
  expect(told).toHaveBeenCalledTimes(1)

  // This tab had nothing to wait for before. It deletes the tap when it runs out.
  vi.advanceTimersByTime(61_000)
  expect(stored()).toEqual([])

  // Storage that is cleared elsewhere is told with no key.
  told.mockClear()
  window.dispatchEvent(new StorageEvent('storage', { key: null }))
  expect(told).toHaveBeenCalledTimes(1)
  unsubscribe()
  stop()
})

test('the list that is shown is the same list until a tap runs out, and then is without it', () => {
  const now = Date.parse('2026-11-26T12:00:00Z')
  const tap = (id: string, left: number) => ({
    id,
    at: new Date(now - KEPT_MS + left).toISOString(),
    photo: false,
    clip: false,
  })
  window.localStorage.setItem(
    'beacon.taps.v1',
    JSON.stringify([tap('long', 3_600_000), tap('short', 60_000)]),
  )

  const first = tapsSnapshot(now)
  expect(first.map((kept) => kept.id)).toEqual(['long', 'short'])
  // The same array while nothing changed, which is what React asks of it.
  expect(tapsSnapshot(now + 59_000)).toBe(first)

  const after = tapsSnapshot(now + 60_000)
  expect(after.map((kept) => kept.id)).toEqual(['long'])
  expect(tapsSnapshot(now + 61_000)).toBe(after)
  // Storage was not touched: showing is not deleting.
  expect(stored()).toEqual(['long', 'short'])
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

  expect(stop).toHaveBeenCalled()
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
  expect(stop).toHaveBeenCalled()
  expect(made).not.toHaveBeenCalled()
})

test('a tap called off while the recorder is being set up lets go of the microphone at once', async () => {
  const { stop } = microphone()
  // The recorder's module is still loading, and goes on loading.
  const loading = vi.fn(() => new Promise<void>(() => undefined))
  const closed = vi.fn(async () => undefined)
  vi.stubGlobal(
    'AudioContext',
    class {
      audioWorklet = { addModule: loading }
      close = closed
    },
  )
  URL.createObjectURL ??= () => 'blob:beacon'
  URL.revokeObjectURL ??= () => undefined
  const controller = new AbortController()

  const recording = recordClip(1, controller.signal)
  await vi.waitFor(() => expect(loading).toHaveBeenCalled())
  expect(stop).not.toHaveBeenCalled()
  controller.abort()

  // Let go of as the tap is called off, not when the module has loaded.
  expect(stop).toHaveBeenCalled()
  await expect(recording).rejects.toMatchObject({ name: 'AbortError' })
  expect(closed).toHaveBeenCalled()
})

test('a recorder that will not start ends the recording and lets go of the microphone', async () => {
  const { stop } = microphone()
  vi.stubGlobal(
    'AudioWorkletNode',
    class {
      port = {}
    },
  )
  const closed = vi.fn(async () => undefined)
  vi.stubGlobal(
    'AudioContext',
    class {
      sampleRate = 48_000
      audioWorklet = { addModule: async () => undefined }
      createMediaStreamSource = () => ({ connect: () => undefined })
      // The browser refuses to start the context.
      resume = () =>
        Promise.reject(new DOMException('Closed.', 'InvalidStateError'))
      close = closed
    },
  )
  URL.createObjectURL ??= () => 'blob:beacon'
  URL.revokeObjectURL ??= () => undefined
  // The test runs in Node, which is where a rejection nobody handled is told.
  const node = (
    globalThis as unknown as {
      process: Record<'on' | 'off', (event: string, told: () => void) => void>
    }
  ).process
  const unhandled = vi.fn()
  node.on('unhandledRejection', unhandled)

  try {
    // The refusal is the recording's failure, in a sentence, and not an error
    // nobody was waiting for.
    await expect(recordClip(1)).rejects.toThrow(
      'Beacon could not record a clip.',
    )
    await new Promise((resolve) => setTimeout(resolve, 20))
  } finally {
    node.off('unhandledRejection', unhandled)
  }

  expect(stop).toHaveBeenCalled()
  expect(closed).toHaveBeenCalled()
  expect(unhandled).not.toHaveBeenCalled()
})

test.each([
  ['storage keeps it', false],
  ['storage refuses it', true],
])(
  'a tap is known as an invented one only while the server answers for it, when %s',
  (_name, refuses) => {
    resetTaps()
    const now = Date.parse('2026-11-26T12:00:00Z')
    const refused = refuses
      ? vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
          throw new DOMException('Full.', 'QuotaExceededError')
        })
      : null
    const at = new Date(now).toISOString()
    keepTap({ id: 'spot', at, photo: false, clip: false, invented: true }, now)
    keepTap({ id: 'own', at, photo: false, clip: false }, now)

    expect(stored().length).toBe(refuses ? 0 : 2)
    expect(isInventedTap('spot', now)).toBe(true)
    expect(isInventedTap('own', now)).toBe(false)
    expect(isInventedTap('spot', now + KEPT_MS - 1)).toBe(true)
    // Run out: not known, whether or not anything has swept it away.
    expect(isInventedTap('spot', now + KEPT_MS)).toBe(false)
    refused?.mockRestore()
    resetTaps()
  },
)

test('a tap that was forgotten is not known as an invented one, whoever forgot it', () => {
  resetTaps()
  const at = new Date().toISOString()
  keepTap({ id: 'spot', at, photo: false, clip: false, invented: true })
  keepTap({ id: 'other', at, photo: false, clip: false, invented: true })

  forgetTap('spot')
  expect(isInventedTap('spot')).toBe(false)
  // Another tab forgot the other one: storage no longer has it.
  window.localStorage.setItem('beacon.taps.v1', '[]')
  expect(isInventedTap('other')).toBe(false)

  // And one storage never took is forgotten from the page that held it.
  const refused = vi
    .spyOn(Storage.prototype, 'setItem')
    .mockImplementation(() => {
      throw new DOMException('Full.', 'QuotaExceededError')
    })
  keepTap({ id: 'unkept', at, photo: false, clip: false, invented: true })
  expect(isInventedTap('unkept')).toBe(true)
  forgetTap('unkept')
  expect(isInventedTap('unkept')).toBe(false)
  refused.mockRestore()
  resetTaps()
})

test('the page holds no more taps storage refused than storage would have kept', () => {
  resetTaps()
  const refused = vi
    .spyOn(Storage.prototype, 'setItem')
    .mockImplementation(() => {
      throw new DOMException('Full.', 'QuotaExceededError')
    })
  const at = new Date().toISOString()
  for (let count = 0; count <= 20; count += 1)
    keepTap({
      id: `tap-${count}`,
      at,
      photo: false,
      clip: false,
      invented: true,
    })

  expect(isInventedTap('tap-20')).toBe(true)
  expect(isInventedTap('tap-1')).toBe(true)
  expect(isInventedTap('tap-0')).toBe(false)
  refused.mockRestore()
  resetTaps()
})

test('a tap storage refused is taken off what is known at the moment it runs out', () => {
  resetTaps()
  vi.useFakeTimers()
  const now = Date.parse('2026-11-26T12:00:00Z')
  vi.setSystemTime(now)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('Full.', 'QuotaExceededError')
  })
  const at = new Date(now).toISOString()
  keepTap({ id: 'spot', at, photo: false, clip: false, invented: true })
  const stop = sweepTaps()
  const told = vi.fn()
  const unsubscribe = subscribeTaps(told)

  vi.advanceTimersByTime(KEPT_MS - 1)
  expect(told).not.toHaveBeenCalled()
  expect(isInventedTap('spot')).toBe(true)
  // Whoever shows the tap's view is told, and no longer finds it known.
  vi.advanceTimersByTime(2)
  expect(told).toHaveBeenCalled()
  expect(isInventedTap('spot')).toBe(false)

  unsubscribe()
  stop()
  resetTaps()
})
