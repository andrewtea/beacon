import type { ReactNode } from 'react'
import {
  fetchIncident,
  type IncidentAnswer,
  type IncidentSummary,
} from '../api/incidents.ts'
import { crisisLeads, incidentCadence } from '../lib/cadence.ts'
import { weakest } from '../lib/labels.ts'
import { usePolled } from '../lib/poll.ts'
import { IncidentBody, IncidentLead } from './IncidentBody.tsx'
import { type Basis, IncidentCard } from './IncidentCard.tsx'

/** What the card's facts rest on: the incident's radio lines. */
function basisOf(answer: IncidentAnswer): Basis | null {
  const radio = answer.incident.timeline.filter(
    (entry) => entry.kind === 'radio' || entry.kind === 'medical',
  )
  const confidence = weakest(radio.map((entry) => entry.confidence))
  if (confidence === null) return null
  return { confidence, sources: radio.flatMap((entry) => entry.sources) }
}

/**
 * An incident, read again for as long as it is shown: its card, then whatever
 * the caller puts between (a tap's match), then its crisis mode, what
 * authorities published, and its timeline. `summary` is shown until the first
 * reading arrives, and when the incident can no longer be read.
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
  const answer = polled.data
  const shown = answer?.incident ?? summary
  return (
    <>
      {shown !== null && (
        <IncidentCard
          incident={shown}
          basis={answer === null ? null : basisOf(answer)}
          heading={heading}
        />
      )}
      {polled.error !== null && (
        <p className="note problem" role="status">
          {polled.error.status === 404
            ? 'Beacon no longer has this incident: its calls were regrouped, or it is older than Beacon reads.'
            : `${polled.error.message} What is shown may be out of date.`}
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
      {answer === null && polled.error === null && (
        <p className="meta" role="status">
          Reading the incident…
        </p>
      )}
    </>
  )
}
