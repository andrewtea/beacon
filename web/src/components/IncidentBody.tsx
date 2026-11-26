import type {
  Conflict,
  Crisis,
  IncidentDetail,
  Official,
  Story,
} from '../api/incidents.ts'
import {
  citedNow,
  officialState,
  officialType,
  signalName,
} from '../lib/labels.ts'
import { crisisLeads } from '../lib/cadence.ts'
import { clock } from '../lib/time.ts'
import {
  ConfidenceLabel,
  CreditLine,
  Line,
  Sources,
  WhoNote,
} from './Provenance.tsx'

// Everything under an incident's card: crisis mode, what authorities published,
// where sources disagree, the points its story is told by, and its timeline.
// The sentences are the server's. What the app adds are labels that say whose
// words a text is: an authority's own, or Beacon's reading of a document.

/** Crisis mode for an incident: the investigator's decision, what it rested on
 * then, and what the incident has of those signals now. */
export function CrisisPanel({ crisis }: { crisis: Crisis }) {
  return (
    <section
      className={crisis.active ? 'panel crisis active' : 'panel crisis'}
      aria-labelledby="crisis-heading"
    >
      <h2 id="crisis-heading">
        {crisis.active ? 'Crisis mode is on' : 'Crisis mode was on'}
      </h2>
      {!crisis.active && (
        <p className="note">
          The incident is not going on now, so crisis mode is no longer on.
        </p>
      )}
      <p>{crisis.text}</p>
      <p className="meta">
        Decided at{' '}
        <time dateTime={crisis.decided_at}>{clock(crisis.decided_at)}</time>.
      </p>
      <WhoNote who={crisis.decided_by_text} standIn={crisis.stand_in_text} />
      <details className="disclosure">
        <summary>What the decision rested on</summary>
        <h3>The signals it cited, as they were then</h3>
        <ul className="cited">
          {crisis.cited.map((cited) => (
            <li key={cited.kind}>
              <span className="cited-name">{signalName(cited.kind)}</span>
              <ConfidenceLabel level={cited.confidence} />
              <span className="cited-now">{citedNow(cited.now)}</span>
              <CreditLine credit={cited.credit} />
            </li>
          ))}
        </ul>
        <h3>Those signals now</h3>
        <p className="meta">{crisis.signals_text}</p>
        <ul className="lines">
          {crisis.signals.map((signal) => (
            <li key={signal.kind} className="line">
              <div className="line-head">
                <ConfidenceLabel level={signal.confidence} />
              </div>
              <p className="line-text">{signal.text}</p>
              <Sources sources={signal.sources} />
            </li>
          ))}
        </ul>
      </details>
    </section>
  )
}

const FIELD: Record<string, string> = {
  issuer: 'issuer',
  headline: 'title',
  instruction: 'instruction',
  summary: 'summary',
  areas: 'areas',
  streets: 'streets',
  description: 'description',
}

const fields = (names: string[]): string =>
  names.map((name) => FIELD[name] ?? name).join(', ')

/** One alert or notice an authority issued. An alert's words are the
 * authority's own. A notice's are Beacon's reading of a document, labeled so,
 * with the document linked beside them every time. */
function OfficialItem({ official }: { official: Official }) {
  const reading = official.reading
  return (
    <li className={`official official-${official.kind}`}>
      <div className="official-head">
        <h3>{officialType(official)}</h3>
        <span className={`tag state-${official.state}`}>
          {officialState(official)}
        </span>
      </div>
      <p className="meta">
        {reading ? 'Published' : 'Issued'} at{' '}
        <time dateTime={official.issued_at}>{clock(official.issued_at)}</time>
        {official.until !== null && (
          <>
            {' '}
            · {reading ? 'the document' : 'the alert'} gives an end time of{' '}
            <time dateTime={official.until}>{clock(official.until)}</time>
          </>
        )}
      </p>
      {reading ? (
        <div className="reading">
          <p className="reading-label">
            Beacon’s reading of the document, not the authority’s words. A
            reading can be wrong or leave something out. Read the document for
            what it says.
          </p>
          <dl className="facts">
            {official.issuer !== null && (
              <>
                <dt>Issuer, as read</dt>
                <dd>{official.issuer}</dd>
              </>
            )}
            {official.headline !== null && (
              <>
                <dt>Title, as read</dt>
                <dd>{official.headline}</dd>
              </>
            )}
            {official.areas.length > 0 && (
              <>
                <dt>Areas, as read</dt>
                <dd>{official.areas.join('; ')}</dd>
              </>
            )}
            {official.instruction !== null && (
              <>
                <dt>Instruction, as read</dt>
                <dd>{official.instruction}</dd>
              </>
            )}
            {official.summary !== null && (
              <>
                <dt>Summary, as read</dt>
                <dd>{official.summary}</dd>
              </>
            )}
          </dl>
        </div>
      ) : (
        <div className="authority">
          {official.issuer !== null && (
            <p className="issuer">{official.issuer}</p>
          )}
          {official.headline !== null && (
            <p className="headline">{official.headline}</p>
          )}
          {official.instruction !== null && (
            <figure className="instruction">
              <figcaption>The authority’s instruction, in its words</figcaption>
              <blockquote>{official.instruction}</blockquote>
            </figure>
          )}
        </div>
      )}
      {official.incomplete.length > 0 && (
        <p className="note">
          The original has more than Beacon keeps ({fields(official.incomplete)}
          ). What is shown here is not the whole of it.
        </p>
      )}
      {official.withheld.length > 0 && (
        <p className="note">
          Beacon left part of its reading out ({fields(official.withheld)}). The
          document has more.
        </p>
      )}
      <div className="official-source">
        <ConfidenceLabel level={official.source.confidence} />
        <Sources
          sources={[official.source]}
          linkText={reading ? 'Read the document' : 'Read the alert'}
        />
      </div>
    </li>
  )
}

function Conflicts({ conflicts }: { conflicts: Conflict[] }) {
  if (conflicts.length === 0) return null
  return (
    <section className="panel conflicts" aria-labelledby="conflicts-heading">
      <h2 id="conflicts-heading">Where sources disagree</h2>
      <ul className="lines">
        {conflicts.map((conflict, index) => (
          <li key={index} className="line">
            <div className="line-head">
              <ConfidenceLabel level={conflict.confidence} />
            </div>
            <p className="line-text">{conflict.text}</p>
            <Sources sources={[...conflict.official, ...conflict.radio]} />
          </li>
        ))}
      </ul>
    </section>
  )
}

function StoryPanel({ story }: { story: Story }) {
  if (story.claims.length === 0) return null
  return (
    <section className="panel story" aria-labelledby="story-heading">
      <h2 id="story-heading">Key points</h2>
      <p className="meta">
        {story.model === 'live'
          ? 'Muse Ember chose these points from the incident’s radio, closure, aircraft, and tap lines.'
          : 'A stand-in for Muse Ember chose these points from the incident’s radio, closure, aircraft, and tap lines, by rule. No model chose them.'}{' '}
        Code checked each against its evidence and wrote every sentence.
        {!story.current &&
          ' The timeline has lines newer than the ones these were chosen from.'}
        {story.left_out > 0 &&
          ` ${story.left_out} chosen ${story.left_out === 1 ? 'point was' : 'points were'} left out because the evidence did not show ${story.left_out === 1 ? 'it' : 'them'}.`}
      </p>
      <ul className="lines">
        {story.claims.map((claim, index) => (
          <Line
            key={index}
            at={claim.at}
            text={claim.text}
            confidence={claim.confidence}
            sources={claim.sources}
          />
        ))}
      </ul>
    </section>
  )
}

/** The alerts and notices authorities issued for the incident's place. */
export function Officials({ officials }: { officials: Official[] }) {
  if (officials.length === 0) return null
  return (
    <section className="panel officials" aria-labelledby="official-heading">
      <h2 id="official-heading">From authorities</h2>
      <ul className="official-list">
        {officials.map((official) => (
          <OfficialItem
            key={official.source.evidence_id ?? official.issued_at}
            official={official}
          />
        ))}
      </ul>
    </section>
  )
}

/** The first things shown of an incident in crisis mode. */
export function IncidentLead({ incident }: { incident: IncidentDetail }) {
  return (
    <>
      {incident.crisis !== null && <CrisisPanel crisis={incident.crisis} />}
      <Officials officials={incident.official} />
    </>
  )
}

export function IncidentBody({ incident }: { incident: IncidentDetail }) {
  // The newest line first: on a phone, what just happened is what is looked for.
  const timeline = incident.timeline.toReversed()
  return (
    <>
      {!crisisLeads(incident) && <IncidentLead incident={incident} />}
      <Conflicts conflicts={incident.conflicts} />
      {incident.story !== null && <StoryPanel story={incident.story} />}
      <section className="panel timeline" aria-labelledby="timeline-heading">
        <h2 id="timeline-heading">
          {crisisLeads(incident) ? 'Crisis timeline' : 'Timeline'}
        </h2>
        <p className="meta">
          One line for each thing a source said, newest first.
        </p>
        <ol className="lines" reversed>
          {timeline.map((entry, index) => (
            <Line
              key={`${entry.at}-${index}`}
              at={entry.at}
              text={entry.text}
              confidence={entry.confidence}
              sources={entry.sources}
            />
          ))}
        </ol>
      </section>
    </>
  )
}
