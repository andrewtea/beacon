import type { Confidence, IncidentSummary, Source } from '../api/incidents.ts'
import { isMedical, standing } from '../lib/labels.ts'
import { href } from '../lib/router.ts'
import { clock } from '../lib/time.ts'
import { ConfidenceLabel, Sources, StandInTag } from './Provenance.tsx'

/** What the card's facts rest on: the radio rows of the incident, and the
 * least confidence among them. */
export type Basis = { confidence: Confidence; sources: Source[] }

/**
 * The incident card: what, who is responding, roughly where, and how it stands.
 * Every word about the incident is the server's. A medical response is its
 * crews and its block, and nothing else is asked of it here.
 */
export function IncidentCard({
  incident,
  basis = null,
  heading = 'h2',
  link = false,
}: {
  incident: IncidentSummary
  basis?: Basis | null
  heading?: 'h1' | 'h2' | 'h3'
  // Whether the title opens the incident's own page.
  link?: boolean
}) {
  const Heading = heading
  const medical = isMedical(incident)
  return (
    <article className={incident.open ? 'card incident' : 'card incident over'}>
      <Heading className="incident-title">
        {link ? (
          <a href={href.incident(incident.id)}>{incident.title}</a>
        ) : (
          incident.title
        )}
      </Heading>
      <p className="chips">
        <span className={incident.open ? 'tag open' : 'tag closed'}>
          {standing(incident)}
        </span>
        {incident.stand_in && <StandInTag />}
      </p>
      {incident.status_text !== null && (
        <p className="status-text">{incident.status_text}</p>
      )}
      <dl className="facts">
        {incident.place !== null && (
          <>
            <dt>Where</dt>
            <dd>{incident.place}</dd>
          </>
        )}
        {incident.units.length > 0 && (
          <>
            <dt>Units named on the radio</dt>
            <dd className="units">
              {incident.units.map((unit) => (
                <span key={unit} className="unit">
                  {unit}
                </span>
              ))}
            </dd>
          </>
        )}
        {!medical && incident.alarm !== null && (
          <>
            <dt>Alarm level on the radio</dt>
            <dd>{incident.alarm}</dd>
          </>
        )}
        {!medical && incident.taps > 0 && (
          <>
            <dt>Taps from nearby</dt>
            <dd>
              {incident.taps_more ? `At least ${incident.taps}` : incident.taps}{' '}
              <span className="aside">taps, not people</span>
            </dd>
          </>
        )}
        <dt>First heard</dt>
        <dd>
          <time dateTime={incident.opened_at}>{clock(incident.opened_at)}</time>
        </dd>
        <dt>Latest update</dt>
        <dd>
          <time dateTime={incident.updated_at}>
            {clock(incident.updated_at)}
          </time>
        </dd>
      </dl>
      {basis !== null && (
        <div className="basis">
          <ConfidenceLabel level={basis.confidence} />
          <Sources sources={basis.sources} />
        </div>
      )}
    </article>
  )
}
