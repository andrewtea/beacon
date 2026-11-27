import type { IncidentAnswer, IncidentDetail } from '../api/incidents.ts'
import type { TapAnswer, TapRun } from '../api/taps.ts'

// How often the app asks again. A tap that is being looked into is asked about
// every second; an incident is read again while Beacon's record of it is open,
// and less often after, when a line or a notice can still follow.
export const PENDING_MS = 1_000
export const OPEN_MS = 10_000
export const OVER_MS = 60_000
export const WATCHING_MS = 15_000
// What is near a saved place is asked for again while its view is open.
export const PLACES_MS = 30_000
// A scale run's results change only when its command is given again.
export const SCALE_MS = 60_000

export const incidentCadence = (answer: IncidentAnswer): number =>
  answer.incident.open ? OPEN_MS : OVER_MS

/** How long to wait before asking for a tap's answer again, or null to stop. */
export function tapCadence(answer: TapAnswer): number | null {
  if (answer.status === 'pending') return PENDING_MS
  if (answer.status === 'incident')
    return answer.incident?.open ? OPEN_MS : OVER_MS
  // An unconfirmed tap changes only while Beacon lists what begins near it.
  return answer.watching ? WATCHING_MS : null
}

/** A run that is still being made is asked about again; a kept one is not. */
export const runCadence = (run: TapRun): number | null =>
  run.state === 'pending' ? PENDING_MS : null

/** Whether an incident's crisis mode and what authorities published lead its
 * page, ahead of anything about a tap: they do while crisis mode is on. */
export function crisisLeads(incident: IncidentDetail): boolean {
  return incident.crisis?.active === true
}
