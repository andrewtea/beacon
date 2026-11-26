import type { IncidentSummary } from '../api/incidents.ts'
import { isMedical, recordState } from '../lib/labels.ts'
import { href } from '../lib/router.ts'
import { clock } from '../lib/time.ts'
import { ConfidenceLabel, Sources, StandInTag } from './Provenance.tsx'

/**
 * The incident card: what, who is responding, roughly where, and what the radio
 * last said of it. Every word about the incident is the server's, and every fact
 * on the card is one its radio calls gave, shown with those calls as its source
 * and the least confidence among them (`incident.radio`). What rests on another
 * source (how many taps came, when the newest line is from) is not on the card:
 * the timeline has it, each line with its own source.
 *
 * A medical response is its title, its crews, and its block, with their source.
 * Nothing else of one is shown, whatever the route sent: no status, no alarm,
 * no taps, no time, and not whether Beacon's record of it is open.
 */
export function IncidentCard({
  incident,
  heading = 'h2',
  link = false,
}: {
  incident: IncidentSummary
  heading?: 'h1' | 'h2' | 'h3'
  // Whether the title opens the incident's own page. A medical response has
  // none to open: its card is all that is shown of it.
  link?: boolean
}) {
  const Heading = heading
  const medical = isMedical(incident)
  // A closed record is drawn quieter, which would say of a medical response
  // what its card does not.
  const closed = !medical && !incident.open
  return (
    <article className={closed ? 'card incident over' : 'card incident'}>
      <Heading className="incident-title">
        {link && !medical ? (
          <a href={href.incident(incident.id)}>{incident.title}</a>
        ) : (
          incident.title
        )}
      </Heading>
      {(!medical || incident.stand_in) && (
        <p className="chips">
          {!medical && (
            <span className={incident.open ? 'tag open' : 'tag closed'}>
              {recordState(incident)}
            </span>
          )}
          {incident.stand_in && <StandInTag />}
        </p>
      )}
      {!medical && incident.status_text !== null && (
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
        {!medical && (
          <>
            <dt>First radio call</dt>
            <dd>
              <time dateTime={incident.opened_at}>
                {clock(incident.opened_at)}
              </time>
            </dd>
          </>
        )}
      </dl>
      {incident.radio !== null && (
        <div className="basis">
          <ConfidenceLabel level={incident.radio.confidence} />
          <Sources sources={incident.radio.sources} />
        </div>
      )}
    </article>
  )
}
