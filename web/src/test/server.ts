import { vi } from 'vitest'

export type Asked = {
  key: string
  body: unknown
  // The request's headers, by their names in lower case.
  headers: Record<string, string>
  // The signal the request was made with, which says whether it was aborted.
  signal: AbortSignal | null
}

/**
 * Stands where the server is: answers `fetch` from `routes`, which are keyed by
 * method and path (`GET /api/health`), and with 404 for anything else. An
 * answer is the JSON to send, or a function that makes the reply, at once or
 * when it likes. Returns the
 * requests that were made, in order.
 */
export function serve(routes: Record<string, unknown>): Asked[] {
  const asked: Asked[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? 'GET'} ${String(input)}`
      asked.push({
        key,
        body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
        headers: Object.fromEntries(new Headers(init?.headers).entries()),
        signal: init?.signal ?? null,
      })
      if (!(key in routes))
        return Response.json(
          { detail: 'There is no such thing.' },
          { status: 404 },
        )
      const answer = routes[key]
      return typeof answer === 'function'
        ? (answer as () => Response | Promise<Response>)()
        : Response.json(answer)
    }),
  )
  return asked
}

export const MOCK_HEALTH = {
  status: 'ok',
  version: '1.2.3',
  mode: 'mock',
  repeats_text: null,
}
