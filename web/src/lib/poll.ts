import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/http.ts'

export type Polled<T> = {
  // The latest answer, kept while a later request fails.
  data: T | null
  // Why the latest request failed, or null when it did not.
  error: ApiError | null
  // When the latest answer arrived, by this device's clock.
  fetchedAt: number | null
}

/**
 * Asks a route now and again for as long as the view is mounted. `every` says
 * how many milliseconds to wait after an answer, or null to stop asking: a tap
 * that is pending is asked about every second, an incident that is over is not
 * asked about again. A request that fails is tried again after `retryMs`, unless
 * the server said the thing is not there (404), which ends the asking.
 */
export function usePolled<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
  every: (data: T) => number | null,
  retryMs = 5_000,
): Polled<T> {
  const [state, setState] = useState<Polled<T> & { key: string }>({
    key,
    data: null,
    error: null,
    fetchedAt: null,
  })
  // The latest functions, so that a caller need not memoise them.
  const latest = useRef({ load, every })
  useEffect(() => {
    latest.current = { load, every }
  })

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const ask = async () => {
      let wait: number | null
      try {
        const data = await latest.current.load(controller.signal)
        if (controller.signal.aborted) return
        setState({ key, data, error: null, fetchedAt: Date.now() })
        wait = latest.current.every(data)
      } catch (error) {
        if (controller.signal.aborted) return
        const failure =
          error instanceof ApiError
            ? error
            : new ApiError('The server could not be reached.', null)
        setState((before) => ({
          ...(before.key === key
            ? before
            : { key, data: null, fetchedAt: null }),
          error: failure,
        }))
        wait = failure.status === 404 ? null : retryMs
      }
      if (wait !== null) timer = setTimeout(ask, wait)
    }
    void ask()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [key, retryMs])

  // An answer for another key is not this key's: show nothing until its own.
  if (state.key !== key) return { data: null, error: null, fetchedAt: null }
  return { data: state.data, error: state.error, fetchedAt: state.fetchedAt }
}
