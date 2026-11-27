import { useLayoutEffect } from 'react'
import { fetchTapAnswer, type TapAnswer } from '../api/taps.ts'
import { Icon } from '../components/Icon.tsx'
import { IncidentCard } from '../components/IncidentCard.tsx'
import { IncidentLive } from '../components/IncidentLive.tsx'
import {
  ConfidenceLabel,
  CreditLine,
  WhoNote,
} from '../components/Provenance.tsx'
import { tapCadence } from '../lib/cadence.ts'
import { basisName, groundName } from '../lib/labels.ts'
import { usePolled } from '../lib/poll.ts'
import { href } from '../lib/router.ts'
import { forgetTap } from '../lib/tapStore.ts'
import { clock, usd } from '../lib/time.ts'

/** Who looked into the tap, what a stand-in had a part in, and what it cost. */
function Investigation({ answer }: { answer: TapAnswer }) {
  const calls = answer.ember_calls
  return (
    <section className="panel investigation" aria-labelledby="inv-heading">
      <h2 id="inv-heading">The investigation</h2>
      <WhoNote who={answer.investigator_text} standIn={answer.stand_in_text} />
      {answer.investigated_by !== null && (
        <p className="meta">
          {calls} {calls === 1 ? 'request' : 'requests'}{' '}
          {answer.investigated_by === 'model'
            ? 'answered by Muse Ember'
            : 'answered by the stand-in, none by Muse Ember'}{' '}
          · {usd(answer.cost_usd)}
        </p>
      )}
      {answer.cost_text !== null && <p className="note">{answer.cost_text}</p>}
      <a className="action" href={href.trace(answer.tap.id)}>
        <Icon name="trace" />
        See each step of the investigation
        <Icon name="forward" size={16} />
      </a>
    </section>
  )
}

function Match({ answer }: { answer: TapAnswer }) {
  const match = answer.match
  if (match === null) return null
  return (
    <section className="panel match" aria-labelledby="match-heading">
      <h2 id="match-heading">Why this incident</h2>
      <p>{match.text}</p>
      <ConfidenceLabel level={match.confidence} />
      <ul className="grounds">
        {match.grounds.map((ground) => (
          <li key={ground.kind}>
            <span className="ground-name">{groundName(ground.kind)}</span>
            <span className="ground-text">{ground.text}</span>
            <span className="ground-basis">
              Checked against {basisName(ground.basis)}.
            </span>
            <CreditLine credit={ground.credit} />
          </li>
        ))}
      </ul>
      {match.grounds_dropped > 0 && (
        <p className="note">
          The investigator gave {match.grounds_dropped} more{' '}
          {match.grounds_dropped === 1 ? 'ground' : 'grounds'} that Beacon’s
          check did not bear out.{' '}
          {match.grounds_dropped === 1 ? 'It is' : 'They are'} not shown.
        </p>
      )}
    </section>
  )
}

/** Incidents listed because of where they are: found near the tap by the
 * investigator's tool, or begun near it since. The server's sentence says which,
 * and when a place one is near by is 511's, 511's credit goes beside it. */
function Others({
  heading,
  answer,
  which,
}: {
  heading: string
  answer: TapAnswer
  which: 'nearby' | 'since'
}) {
  const incidents = answer[which]
  const text = answer[`${which}_text`]
  if (incidents.length === 0) return null
  return (
    <section className="panel others" aria-labelledby={`${which}-heading`}>
      <h2 id={`${which}-heading`}>{heading}</h2>
      {text !== null && <p className="meta">{text}</p>}
      <CreditLine credit={answer[`${which}_credit`]} />
      <div className="card-list">
        {incidents.map((incident) => (
          <IncidentCard
            key={incident.id}
            incident={incident}
            heading="h3"
            link
          />
        ))}
      </div>
    </section>
  )
}

/** What is said of a tap the server answered that it does not have, with
 * nothing of the tap shown and nothing asked. `repeatsText` is the server's
 * sentence about telling its story again, when it does: why a tap can be gone
 * sooner than on another server. */
export function TapGone({ repeatsText }: { repeatsText: string | null }) {
  return (
    <div className="view tap-view">
      <h1>This tap is gone</h1>
      <p role="status">
        Beacon does not have this tap. An answer is kept for 23 hours at most.
      </p>
      {repeatsText !== null && <p className="meta">{repeatsText}</p>}
      <a className="action" href={href.home}>
        <Icon name="back" size={16} />
        Back to the button
      </a>
    </div>
  )
}

export function TapView({
  id,
  repeatsText = null,
  onGone,
}: {
  id: string
  // As in `TapGone`.
  repeatsText?: string | null
  // Told when the server answers that it does not have the tap, before the
  // tap is forgotten: whoever mounted the view may take it away then.
  onGone?: () => void
}) {
  const polled = usePolled(
    `tap:${id}`,
    (signal) => fetchTapAnswer(id, signal),
    tapCadence,
  )
  const gone = polled.error?.status === 404
  // A tap the server no longer has is not kept on the device either.
  // Before the page is drawn: whoever mounted the view shows that the tap is
  // gone in this view's place, and it is said once.
  useLayoutEffect(() => {
    if (!gone) return
    onGone?.()
    forgetTap(id)
  }, [gone, id, onGone])

  const answer = polled.data
  if (answer === null) {
    if (gone) return <TapGone repeatsText={repeatsText} />
    return (
      <div className="view tap-view">
        {polled.error === null ? (
          <p className="meta" role="status">
            Reading the answer…
          </p>
        ) : (
          <>
            <h1>No answer yet</h1>
            <p role="status">{polled.error.message}</p>
          </>
        )}
        <a className="action" href={href.home}>
          <Icon name="back" size={16} />
          Back to the button
        </a>
      </div>
    )
  }

  const received = (
    <p className="meta tap-meta">
      Your tap at{' '}
      <time dateTime={answer.tap.received_at}>
        {clock(answer.tap.received_at)}
      </time>
      {answer.tap.clip && ' · with a clip'}
      {answer.tap.photo && ' · with a photo'}
    </p>
  )
  const stale = polled.error !== null && (
    <p className="note problem" role="status">
      {gone
        ? 'Beacon no longer has this tap. This is the last answer it gave.'
        : `${polled.error.message} This answer may be out of date.`}
    </p>
  )

  if (answer.status === 'pending') {
    return (
      <div className="view tap-view pending" aria-live="polite">
        <div className="looking" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <h1>Looking into it</h1>
        <p>{answer.text}</p>
        {received}
        {stale}
      </div>
    )
  }

  if (answer.status === 'incident') {
    const incident = answer.incident
    return (
      <div
        className={
          incident?.crisis?.active ? 'view tap-view in-crisis' : 'view tap-view'
        }
      >
        {stale}
        {incident !== null ? (
          <IncidentLive id={incident.id} summary={incident}>
            {received}
            <Match answer={answer} />
            <Investigation answer={answer} />
          </IncidentLive>
        ) : (
          <>
            <h1>The incident is gone</h1>
            {answer.incident_gone_text !== null && (
              <p>{answer.incident_gone_text}</p>
            )}
            {received}
            <Match answer={answer} />
            <Investigation answer={answer} />
          </>
        )}
        <Others heading="Also nearby" answer={answer} which="nearby" />
      </div>
    )
  }

  return (
    <div className="view tap-view unconfirmed">
      {stale}
      <h1 className="answer-text">{answer.text}</h1>
      {received}
      {answer.watching && (
        <p className="note watching" role="status">
          For half an hour after an unconfirmed tap, Beacon lists here any
          incident whose first radio call comes within a kilometre of it. Nobody
          investigates the tap a second time.
        </p>
      )}
      <Others
        heading="Began near here since your tap"
        answer={answer}
        which="since"
      />
      <Others heading="Also nearby" answer={answer} which="nearby" />
      <Investigation answer={answer} />
      <a className="action" href={href.home}>
        <Icon name="back" size={16} />
        Back to the button
      </a>
    </div>
  )
}
