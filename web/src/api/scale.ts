import { fetchJson } from './http.ts'

// `GET /api/scale`: the results of the scale run this server names, each number
// with the sentence that says what it is. Field for field as
// `server/beacon/api/scale.py` documents them.

export type ScaleNumber = {
  key: string
  // What is counted, in a few words.
  label: string
  // The value as it is shown, with its unit: "468 calls", "85.2%", or "none"
  // for a number the run does not have, which `text` then explains.
  value_text: string
  // What was counted and what the count does not show. Shown beside the value,
  // every time: no number of a run is shown bare.
  text: string
}

export type ScaleSection = {
  // What its numbers are about. Empty for numbers of no heading the server knows.
  title: string
  numbers: ScaleNumber[]
}

export type ScaleRun = {
  name: string
  // The day of DataSF's record the run is scored against, as YYYY-MM-DD.
  day: string
  made_at: string
  // Whether a stand-in had a part in the numbers: generated audio, a stand-in
  // for Ember, or both.
  stand_in: boolean
  complete: boolean
  // What the run is in a few words: "Stand-in, generated calls".
  kind_text: string
  // What the run is and what its numbers do and do not measure. Shown before
  // any number.
  basis_text: string
  // The sentence that says a stand-in's numbers are no measurement of Beacon on
  // real radio. Shown with every group of numbers. Null only when `stand_in`
  // is false.
  reminder_text: string | null
  // What `complete` means here.
  state_text: string
  // What else holds for every number.
  caveats: string[]
  // Whose record the run is scored against, with its page.
  scored_against: { text: string; url: string }
  sections: ScaleSection[]
}

export type Scale = {
  // What a scale run is, and that it is apart from what this server shows.
  about_text: string
  // The run this server shows, or null when it shows none.
  run: ScaleRun | null
  // Why there is no run, when there is none.
  none_text: string | null
}

export function fetchScale(signal?: AbortSignal): Promise<Scale> {
  return fetchJson<Scale>('/api/scale', { signal })
}
