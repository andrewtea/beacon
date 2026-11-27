import { fetchJson } from './http.ts'

export type Input = 'own' | 'invented'

export type Health = {
  status: 'ok'
  version: string
  // `mock` when the server runs on the invented scenario. Null when it reads no
  // evidence.
  mode: 'live' | 'mock' | null
  // On a server on the invented scenario: who does the model's work on it, a
  // `stand_in` that works by rule or Muse Ember (`ember`). Null on real sources.
  // Every incident, story, and answer says of itself who made it; the app
  // words nothing from this.
  teller: 'stand_in' | 'ember' | null
  // Where Muse Ember works on the invented scenario: the server's sentence that
  // says so, and that the story is still made up. Shown with what says the
  // scenario is invented. Null on every other server.
  teller_text: string | null
  // On a server that tells the invented scenario again when it is over: the
  // server's sentence that says so, and what becomes of the taps sent before.
  // Shown with what says the scenario is invented. Null on every other server.
  repeats_text: string | null
  // What the server takes from a visitor: their `own` place, sound, photo, saved
  // places, and notification address, or only the `invented` scenario's two
  // places, which is what a server takes that is reached through a host nobody
  // has seen keep the private routes out of its records. The app then asks for
  // no place, sound, or photo, and sends none.
  input: Input
  // On a server that takes only the invented places: the server's sentence that
  // says so and why, shown on the pages a person sends something from. Null on
  // every other server.
  front_text: string | null
  // The rest of that, shown when it is asked for. Null whenever that one is.
  front_more_text: string | null
}

export function fetchHealth(signal?: AbortSignal): Promise<Health> {
  return fetchJson<Health>('/api/health', { signal })
}
