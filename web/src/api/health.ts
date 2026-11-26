import { fetchJson } from './http.ts'

export type Health = {
  status: 'ok'
  version: string
}

export function fetchHealth(signal?: AbortSignal): Promise<Health> {
  return fetchJson<Health>('/api/health', { signal })
}
