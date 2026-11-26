import { fetchJson } from './http.ts'
import type { Confidence, Credit, IncidentSummary, Mode } from './incidents.ts'

// The shapes of the tap routes, field for field as `server/beacon/api/taps.py`
// documents them.

export type TapBody = {
  location: { lat: number; lon: number; accuracy_m?: number }
  // A JPEG and a WAV file, each in base64.
  photo?: string
  clip?: string
}

export type TapReceipt = {
  id: string
  received_at: string
  mode: Mode
  photo: boolean
  clip: boolean
  media_kept_until: string | null
  investigating: boolean
}

export type Ground = {
  kind: string
  text: string
  basis: string
  credit: Credit | null
}

export type Match = {
  confidence: 'medium' | 'low'
  text: string
  grounds: Ground[]
  grounds_dropped: number
}

export type TapAnswer = {
  mode: Mode
  as_of: string
  partial: boolean
  tap: { id: string; received_at: string; photo: boolean; clip: boolean }
  status: 'pending' | 'incident' | 'unconfirmed'
  reason: string | null
  text: string
  investigated_at: string | null
  investigated_by: 'model' | 'stand_in' | null
  investigator_text: string | null
  model: string | null
  stand_in: boolean
  stand_in_text: string | null
  incident_id: string | null
  incident: IncidentSummary | null
  incident_gone: boolean
  incident_gone_text: string | null
  match: Match | null
  nearby: IncidentSummary[]
  nearby_text: string | null
  watching: boolean
  since: IncidentSummary[]
  since_text: string | null
  cost_usd: number
  ember_calls: number
  cost_incomplete: boolean
  cost_text: string | null
}

export type EmberCall = {
  source: string
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cost_usd: number
  latency_ms: number
}

export type ToolCall = {
  tool: string
  thinking: string | null
  thinking_text: string | null
  lead: string | null
  why: string | null
  args: Record<string, unknown>
  ok: boolean
  text: string
  returned: Record<string, unknown>
  returned_text: string
  credits: Credit[]
  ember: EmberCall | null
}

export type Round = { n: number; ember: EmberCall; calls: ToolCall[] }

export type CitedThen = {
  kind: string
  confidence: Confidence
  evidence_ids: number[]
  credit: Credit | null
}

export type Outcome = {
  status: 'incident' | 'unconfirmed'
  reason: string | null
  text: string
  incident_id: string | null
  grounds: string[]
  grounds_dropped: number
  confidence: 'medium' | 'low' | null
  crisis: boolean
  crisis_signals: string[]
  crisis_cited: CitedThen[]
  crisis_refused: boolean
}

export type TapRun = {
  mode: Mode
  tap_id: string
  state: 'none' | 'pending' | 'lost' | 'finished'
  started_at: string | null
  finished_at: string | null
  evidence_as_of: string | null
  investigated_by: 'model' | 'stand_in' | null
  investigator_text: string | null
  model: string | null
  stand_in: boolean
  stand_in_text: string | null
  rounds: Round[]
  final: EmberCall | null
  outcome: Outcome | null
  cut_short: string | null
  cut_short_text: string | null
  ember_calls: number
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cost_usd: number
  latency_ms: number
  unaccounted_calls: number
  cost_incomplete: boolean
  cost_text: string | null
}

export function sendTap(
  body: TapBody,
  signal?: AbortSignal,
): Promise<TapReceipt> {
  return fetchJson<TapReceipt>('/api/taps', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

export function fetchTapAnswer(
  id: string,
  signal?: AbortSignal,
): Promise<TapAnswer> {
  return fetchJson<TapAnswer>(`/api/taps/${encodeURIComponent(id)}`, { signal })
}

export function fetchTapRun(id: string, signal?: AbortSignal): Promise<TapRun> {
  return fetchJson<TapRun>(`/api/taps/${encodeURIComponent(id)}/run`, {
    signal,
  })
}
