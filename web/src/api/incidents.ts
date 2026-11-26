import { fetchJson } from './http.ts'

// The shapes of `GET /api/incidents` and `GET /api/incidents/{id}`, field for
// field as `server/beacon/api/incidents.py` documents them.

export type Confidence = 'official' | 'high' | 'medium' | 'low'
export type Mode = 'live' | 'mock'

/** Words a source's terms require beside anything shown from it, and their link. */
export type Credit = { text: string; url: string }

export type Source = {
  kind: string
  source: string
  label: string
  confidence: Confidence
  at: string
  evidence_id: number | null
  url: string | null
  stand_in: boolean
  model: string | null
  credit: Credit | null
}

export type Signal = {
  kind: string
  text: string
  confidence: Confidence
  sources: Source[]
}

export type Conflict = {
  kind: string
  text: string
  confidence: Confidence
  official: Source[]
  radio: Source[]
}

export type Cited = {
  kind: string
  confidence: Confidence
  evidence_ids: number[]
  now: 'same' | 'changed' | 'gone'
  credit: Credit | null
}

export type Crisis = {
  active: boolean
  decided_at: string
  text: string
  decided_by: 'model' | 'stand_in'
  decided_by_text: string
  cited: Cited[]
  signals: Signal[]
  signals_text: string
  model: string | null
  stand_in: boolean
  stand_in_text: string | null
}

export type Point = { lat: number; lon: number }

/** The radio calls an incident's title, status, place, units, alarm, and first
 * call rest on: the least confidence among them, and a source for each. */
export type Radio = { confidence: Confidence; sources: Source[] }

export type IncidentSummary = {
  id: string
  category: string
  title: string
  status: string
  // The status in the server's sentence. Null for a medical response.
  status_text: string | null
  // Whether Beacon's record of it is open. Not whether anything is happening
  // at the place: a record also closes when the radio has only gone quiet.
  open: boolean
  // When its first radio call came, and when the newest line of its timeline is
  // from, whichever source that line is from.
  opened_at: string
  updated_at: string
  radio: Radio | null
  place: string | null
  point: Point | null
  point_from: string | null
  point_source: Source | null
  streets: string[]
  units: string[]
  alarm: number | null
  taps: number
  taps_more: boolean
  aircraft_more: boolean
  signals: Signal[]
  conflicts: Conflict[]
  stand_in: boolean
  crisis: Crisis | null
}

export type Entry = {
  at: string
  kind: string
  text: string
  confidence: Confidence
  sources: Source[]
  details: Record<string, unknown>
}

export type Official = {
  kind: 'alert' | 'notice'
  type: string
  state: string
  ended_by: number | null
  issued_at: string
  until: string | null
  issuer: string | null
  headline: string | null
  instruction: string | null
  summary: string | null
  areas: string[]
  reading: boolean
  source: Source
  incomplete: string[]
  withheld: string[]
}

export type Track = {
  icao24: string
  callsign: string | null
  category: string | null
  first_seen: string
  last_seen: string
  positions: number
  nearest_m: number
  lowest_m: number | null
  highest_m: number | null
  circling: boolean
  low: boolean
}

export type Claim = {
  kind: string
  text: string
  at: string
  confidence: Confidence
  sources: Source[]
}

export type Story = {
  written_at: string
  current: boolean
  claims: Claim[]
  left_out: number
  lines: number
  lines_left_out: number
  model: string
  stand_in: boolean
  input_tokens: number
  cached_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cost_usd: number
  latency_ms: number
}

export type IncidentDetail = IncidentSummary & {
  timeline: Entry[]
  official: Official[]
  aircraft: Track[]
  story: Story | null
}

export type IncidentAnswer = {
  mode: Mode
  as_of: string
  partial: boolean
  incident: IncidentDetail
}

export function fetchIncident(
  id: string,
  signal?: AbortSignal,
): Promise<IncidentAnswer> {
  return fetchJson<IncidentAnswer>(`/api/incidents/${encodeURIComponent(id)}`, {
    signal,
  })
}
