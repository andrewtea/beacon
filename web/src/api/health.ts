import { fetchJson } from './http.ts'

export type Health = {
  status: 'ok'
  version: string
  // `mock` when the server runs on the invented scenario. Null when it reads no
  // evidence.
  mode: 'live' | 'mock' | null
  // On a server that tells the invented scenario again when it is over: the
  // server's sentence that says so, and what becomes of the taps sent before.
  // Shown with what says the scenario is invented. Null on every other server.
  repeats_text: string | null
}

export function fetchHealth(signal?: AbortSignal): Promise<Health> {
  return fetchJson<Health>('/api/health', { signal })
}
