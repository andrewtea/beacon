import { vi } from 'vitest'

export type Asked = { key: string; body: unknown }

/**
 * Stands where the server is: answers `fetch` from `routes`, which are keyed by
 * method and path (`GET /api/health`), and with 404 for anything else. An
 * answer is the JSON to send, or a function that makes the reply. Returns the
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
      })
      if (!(key in routes))
        return Response.json(
          { detail: 'There is no such thing.' },
          { status: 404 },
        )
      const answer = routes[key]
      return typeof answer === 'function'
        ? (answer as () => Response)()
        : Response.json(answer)
    }),
  )
  return asked
}

export const MOCK_HEALTH = { status: 'ok', version: '1.2.3', mode: 'mock' }
