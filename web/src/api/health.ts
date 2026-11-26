import { fetchJson } from './http.ts'

export type Health = {
  status: 'ok'
  version: string
  // `mock` when the server runs on the invented scenario. Null when it reads no
  // evidence.
  mode: 'live' | 'mock' | null
}

export function fetchHealth(signal?: AbortSignal): Promise<Health> {
  return fetchJson<Health>('/api/health', { signal })
}
