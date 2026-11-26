import type { Confidence, IncidentSummary, Official } from '../api/incidents.ts'

// Short labels for the server's closed lists. They name a thing and claim
// nothing about it: every sentence about an incident, an order, or an
// investigation is the server's (`*_text`), shown as it is given.

const humanize = (tag: string): string => {
  const words = tag.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// How an alert or a notice stands. `published` is all Beacon says of a notice:
// never that an order is in force. `not_listed` is what Beacon saw, and keeps
// the words that say it is no all-clear.
const OFFICIAL_STATE: Record<string, string> = {
  in_force: 'In force',
  published: 'Published',
  replaced: 'Replaced by a later message',
  cancelled: 'Cancelled by the authority',
  not_listed: 'No longer listed. Not an official all-clear.',
  ended: 'Past its end time',
  unknown: 'Not known whether it is in force',
}

export function officialState(official: Official): string {
  return OFFICIAL_STATE[official.state] ?? humanize(official.state)
}

const NOTICE_TYPE: Record<string, string> = {
  evacuation_order: 'Evacuation order',
  evacuation_warning: 'Evacuation warning',
  shelter_in_place: 'Shelter-in-place order',
  road_closure: 'Road closure notice',
  all_clear: 'All-clear notice',
  advisory: 'Advisory',
  press_release: 'Press release',
  other: 'Document',
}

/** What an alert or a notice is: an alert's own name for itself, or the type
 * Beacon read a notice as. */
export function officialType(official: Official): string {
  if (official.kind === 'alert') return official.type
  return NOTICE_TYPE[official.type] ?? humanize(official.type)
}

// The names of the signals a decision on crisis mode can rest on. A notice
// signal covers orders and warnings alike, so it is not called an order.
const SIGNAL: Record<string, string> = {
  alarm: 'Alarm level on the radio',
  evacuation_on_radio: 'Evacuation mentioned on the radio',
  many_units: 'Many units named on the radio',
  official_notice: 'Official order or warning published',
  official_alert: 'Official alert',
  closure: 'Road closure listed by 511',
  aircraft_circling: 'Aircraft circling',
  taps: 'Taps from nearby',
}

export function signalName(kind: string): string {
  return SIGNAL[kind] ?? humanize(kind)
}

const CITED_NOW: Record<string, string> = {
  same: 'The incident still has this signal, on the same evidence.',
  changed: 'The incident has this signal now on other evidence.',
  gone: 'The incident no longer has this signal.',
}

export function citedNow(now: string): string {
  return CITED_NOW[now] ?? ''
}

// The grounds a match can rest on, by name. Their sentences are the server's.
const GROUND: Record<string, string> = {
  near: 'Near the tap',
  going_on: 'Going on at the time',
  heard_in_clip: 'Heard in the clip',
  seen_in_photo: 'Seen in the photo',
  aircraft_overhead: 'Aircraft overhead',
  other_taps: 'Other taps nearby',
}

export function groundName(kind: string): string {
  return GROUND[kind] ?? humanize(kind)
}

// What each ground was checked against, as the trace names it.
const BASIS: Record<string, string> = {
  nearby_incidents: 'the nearby incidents tool',
  listen_clip: 'the clip tool',
  look_photo: 'the photo tool',
  nearby_taps: 'the nearby taps tool',
  incident_aircraft: "the incident's own aircraft",
}

export function basisName(basis: string): string {
  return BASIS[basis] ?? humanize(basis).toLowerCase()
}

const TOOL: Record<string, string> = {
  nearby_incidents: 'Nearby incidents',
  get_radio: 'Radio calls',
  get_aircraft: 'Aircraft',
  get_official: 'Official alerts, notices, closures',
  nearby_taps: 'Nearby taps',
  listen_clip: 'Listen to the clip',
  look_photo: 'Look at the photo',
}

export function toolName(tool: string): string {
  return TOOL[tool] ?? tool
}

/** Whether an incident is a medical response, of which Beacon shows its crews
 * and its block and nothing else. */
export function isMedical(incident: IncidentSummary): boolean {
  return incident.category === 'medical response'
}

/** How an incident stands, in a few words: going on, or what was last heard. */
export function standing(incident: IncidentSummary): string {
  if (incident.open) return 'Going on'
  return 'Not going on now'
}

const RANK: Confidence[] = ['official', 'high', 'medium', 'low']

/** The least confidence among several: a claim that rests on several rows is
 * trusted as little as the weakest, as the server has it. */
export function weakest(levels: Confidence[]): Confidence | null {
  if (levels.length === 0) return null
  return RANK[Math.max(...levels.map((level) => RANK.indexOf(level)))] ?? 'low'
}
