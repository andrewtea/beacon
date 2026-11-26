import type { ReactNode } from 'react'
import { fetchIncident, type IncidentSummary } from '../api/incidents.ts'
import { crisisLeads, incidentCadence } from '../lib/cadence.ts'
import { isMedical } from '../lib/labels.ts'
import { usePolled } from '../lib/poll.ts'
import { IncidentBody, IncidentLead } from './IncidentBody.tsx'
import { IncidentCard } from './IncidentCard.tsx'

/**
 * An incident, read again for as long as it is shown: its card, then whatever
 * the caller puts between (a tap's match), then its crisis mode, what
 * authorities published, and its timeline. `summary` is shown until the first
 * reading arrives.
 *
 * When the server says there is no such incident, nothing of it is shown: not
 * the last reading, whose "crisis mode is on" and "in force" nobody is checking
 * any more, and not `summary`. What the caller puts between stays, so a tap
 * keeps the match that was made for it.
 */
export function IncidentLive({
  id,
  summary = null,
  heading = 'h1',
  children,
}: {
  id: string
  summary?: IncidentSummary | null
  heading?: 'h1' | 'h2'
  children?: ReactNode
}) {
  const polled = usePolled(
    `incident:${id}`,
    (signal) => fetchIncident(id, signal),
    incidentCadence,
  )
  const gone = polled.error?.status === 404
  const read = gone ? null : polled.data
  // A medical response is its card: its title, its crews, and its block. Its
  // timeline would add the times of its calls, which are not shown.
  const medical = read !== null && isMedical(read.incident)
  const answer = medical ? null : read
  const shown = gone ? null : (read?.incident ?? summary)
  return (
    <>
      {shown !== null && <IncidentCard incident={shown} heading={heading} />}
      {gone && (
        <>
          {heading === 'h1' && <h1>Beacon has no incident at this address</h1>}
          <p className="note problem" role="status">
            An incident’s address can change when its calls are grouped anew,
            and an old incident is no longer read. Nothing is shown of what was
            at this address before.
          </p>
        </>
      )}
      {polled.error !== null && !gone && (
        <p className="note problem" role="status">
          {polled.error.message} What is shown may be out of date.
        </p>
      )}
      {answer?.partial && (
        <p className="note">
          There was more evidence than Beacon reads at once. An incident can
          lack its oldest lines, count fewer taps than there were, or show an
          old alert as not known.
        </p>
      )}
      {answer !== null && crisisLeads(answer.incident) && (
        <IncidentLead incident={answer.incident} />
      )}
      {children}
      {answer !== null && <IncidentBody incident={answer.incident} />}
      {medical && (
        <p className="note">
          Beacon shows a medical response as its crews and its block, and
          nothing else.
        </p>
      )}
      {polled.data === null && polled.error === null && (
        <p className="meta" role="status">
          Reading the incident…
        </p>
      )}
    </>
  )
}
