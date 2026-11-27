import { useEffect } from 'react'
import type { Credit } from '../api/incidents.ts'
import {
  type EmberCall,
  fetchTapRun,
  type Outcome,
  type TapRun,
  type ToolCall,
} from '../api/taps.ts'
import { Icon } from '../components/Icon.tsx'
import {
  ConfidenceLabel,
  CreditLine,
  WhoNote,
} from '../components/Provenance.tsx'
import { runCadence } from '../lib/cadence.ts'
import { groundName, signalName, toolName } from '../lib/labels.ts'
import { usePolled } from '../lib/poll.ts'
import { href } from '../lib/router.ts'
import { forgetTap } from '../lib/tapStore.ts'
import { clockSeconds, usd } from '../lib/time.ts'

// The agent trace: each round of the investigator's run, the tools it called
// and why, what each came back with, and what each request cost. A run is kept
// as ids, counts, and tags, and every sentence here is the server's, written
// from them. Nothing a tap sent and nothing a source said is in it.

/** Who answered a request: Muse Ember, or the stand-in. Never one called the
 * other. */
const answeredBy = (source: string): string =>
  source === 'live'
    ? 'Muse Ember'
    : source === 'replay'
      ? 'A recorded answer of Muse Ember’s'
      : 'The stand-in'

/** The accounting of one request: who answered, its tokens, time, and cost. */
function Accounting({ ember }: { ember: EmberCall }) {
  return (
    <dl className="accounting">
      <div>
        <dt>Answered by</dt>
        <dd>{answeredBy(ember.source)}</dd>
      </div>
      <div>
        <dt>Tokens in</dt>
        <dd>
          {ember.input_tokens}
          {ember.cached_tokens > 0 && ` (${ember.cached_tokens} cached)`}
        </dd>
      </div>
      <div>
        <dt>Tokens out</dt>
        <dd>
          {ember.output_tokens}
          {ember.reasoning_tokens > 0 &&
            ` (${ember.reasoning_tokens} reasoning)`}
        </dd>
      </div>
      <div>
        <dt>Time</dt>
        <dd>{ember.latency_ms} ms</dd>
      </div>
      <div>
        <dt>Cost</dt>
        <dd>{usd(ember.cost_usd)}</dd>
      </div>
    </dl>
  )
}

/** What a tool kept of its answer: ids, counts, and tags, as they are. */
function Kept({ returned }: { returned: Record<string, unknown> }) {
  if (Object.keys(returned).length === 0) return null
  return (
    <details className="kept">
      <summary>What was kept of the answer</summary>
      <pre>{JSON.stringify(returned, null, 2)}</pre>
    </details>
  )
}

/** Several sources' credits, each on a line of its own: one can be a sentence
 * long, and two set end to end read as one. */
function Credits({ credits }: { credits: Credit[] }) {
  if (credits.length === 0) return null
  return (
    <p className="credits">
      {credits.map((credit) => (
        <CreditLine key={credit.text} credit={credit} />
      ))}
    </p>
  )
}

function Call({ call }: { call: ToolCall }) {
  return (
    <li className={call.ok ? 'call' : 'call refused'}>
      <h4>
        <span className="tool">{toolName(call.tool)}</span>
        <code>{call.tool}</code>
      </h4>
      {call.thinking_text !== null && (
        <p className="thinking">{call.thinking_text}</p>
      )}
      <p className="asked">{call.text}</p>
      <p className="returned">{call.returned_text}</p>
      <Credits credits={call.credits} />
      <Kept returned={call.returned} />
      {call.ember !== null && (
        <>
          <p className="meta">
            This tool made a request of its own to look or listen:
          </p>
          <Accounting ember={call.ember} />
        </>
      )}
    </li>
  )
}

function OutcomePanel({ outcome }: { outcome: Outcome }) {
  return (
    <>
      <p>{outcome.text}</p>
      {outcome.confidence !== null && (
        <ConfidenceLabel level={outcome.confidence} />
      )}
      {outcome.grounds.length > 0 && (
        <p className="meta">
          Grounds that held when code checked them:{' '}
          {outcome.grounds.map(groundName).join(', ')}.
          {outcome.grounds_dropped > 0 &&
            ` ${outcome.grounds_dropped} more did not hold and ${outcome.grounds_dropped === 1 ? 'was' : 'were'} dropped.`}
        </p>
      )}
      <Credits credits={outcome.credits} />
      {outcome.crisis && (
        <div className="crisis-decision">
          <p>
            The investigator decided on crisis mode, and code found every signal
            it cited among the incident’s own. That is the investigator’s
            decision, not an authority’s. Only the first such decision for an
            incident turns crisis mode on.
          </p>
          <ul className="cited">
            {outcome.crisis_cited.map((cited) => (
              <li key={cited.kind}>
                <span className="cited-name">{signalName(cited.kind)}</span>
                <ConfidenceLabel level={cited.confidence} />
                <CreditLine credit={cited.credit} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {outcome.crisis_refused && (
        <p className="note">
          The investigator decided on crisis mode, and code refused the
          decision: it did not stand.
        </p>
      )}
    </>
  )
}

function Finished({ run }: { run: TapRun }) {
  return (
    <>
      <WhoNote who={run.investigator_text} standIn={run.stand_in_text} />
      <dl className="facts run-facts">
        {run.evidence_as_of !== null && (
          <>
            <dt>Evidence as it stood at</dt>
            <dd>
              <time dateTime={run.evidence_as_of}>
                {clockSeconds(run.evidence_as_of)}
              </time>
              , when the tap came
            </dd>
          </>
        )}
        {run.started_at !== null && run.finished_at !== null && (
          <>
            <dt>Run</dt>
            <dd>
              <time dateTime={run.started_at}>
                {clockSeconds(run.started_at)}
              </time>{' '}
              to{' '}
              <time dateTime={run.finished_at}>
                {clockSeconds(run.finished_at)}
              </time>
            </dd>
          </>
        )}
      </dl>
      <ol className="rounds">
        {run.rounds.map((round) => (
          <li key={round.n} className="round">
            <h3>Round {round.n}</h3>
            <p className="meta">
              {round.calls.length === 1
                ? '1 tool call of this round was kept.'
                : `${round.calls.length} tool calls of this round were kept.`}{' '}
              A round can have asked for more than were kept.
            </p>
            <Accounting ember={round.ember} />
            <ol className="calls">
              {round.calls.map((call, index) => (
                <Call key={index} call={call} />
              ))}
            </ol>
          </li>
        ))}
        <li className="round answer">
          <h3>Answer</h3>
          {run.final !== null && <Accounting ember={run.final} />}
          {run.cut_short_text !== null && (
            <p className="note problem">{run.cut_short_text}</p>
          )}
          {run.outcome !== null && <OutcomePanel outcome={run.outcome} />}
        </li>
      </ol>
      <section className="panel totals" aria-labelledby="totals-heading">
        <h2 id="totals-heading">Totals</h2>
        <dl className="accounting">
          <div>
            <dt>Requests answered</dt>
            <dd>{run.ember_calls}</dd>
          </div>
          <div>
            <dt>Tokens in</dt>
            <dd>
              {run.input_tokens}
              {run.cached_tokens > 0 && ` (${run.cached_tokens} cached)`}
            </dd>
          </div>
          <div>
            <dt>Tokens out</dt>
            <dd>
              {run.output_tokens}
              {run.reasoning_tokens > 0 &&
                ` (${run.reasoning_tokens} reasoning)`}
            </dd>
          </div>
          <div>
            <dt>Time</dt>
            <dd>{run.latency_ms} ms</dd>
          </div>
          <div>
            <dt>Cost</dt>
            <dd>{usd(run.cost_usd)}</dd>
          </div>
        </dl>
        {run.investigated_by === 'stand_in' && (
          <p className="meta">
            The stand-in answered every request. These are not Muse Ember’s
            numbers, and nothing was spent.
          </p>
        )}
        {run.cost_text !== null && <p className="note">{run.cost_text}</p>}
      </section>
    </>
  )
}

export function TraceView({
  id,
  repeatsText = null,
  onGone,
}: {
  id: string
  // As in `TapView`: why a run can be gone sooner than on another server, and
  // who is told that it is.
  repeatsText?: string | null
  onGone?: () => void
}) {
  const polled = usePolled(
    `run:${id}`,
    (signal) => fetchTapRun(id, signal),
    runCadence,
  )
  const gone = polled.error?.status === 404
  useEffect(() => {
    if (!gone) return
    onGone?.()
    forgetTap(id)
  }, [gone, id, onGone])
  const run = polled.data
  return (
    <div className="view trace-view">
      <a className="action back" href={href.tap(id)}>
        <Icon name="back" size={16} />
        Back to the answer
      </a>
      <h1>How Beacon investigated this tap</h1>
      {polled.error !== null && (
        <p className="note problem" role="status">
          {gone
            ? 'Beacon does not have this tap. A run is kept for 23 hours at most.'
            : polled.error.message}
        </p>
      )}
      {gone && repeatsText !== null && <p className="meta">{repeatsText}</p>}
      {run === null && polled.error === null && (
        <p className="meta" role="status">
          Reading the run…
        </p>
      )}
      {run?.state === 'pending' && (
        <p role="status">The investigation is still under way.</p>
      )}
      {run?.state === 'none' && (
        <p role="status">
          Beacon has no investigation of this tap, and none is coming.
        </p>
      )}
      {run?.state === 'lost' && (
        <>
          <p role="status">
            An investigation of this tap was begun and nothing was kept of it:
            the server was stopped under it.
          </p>
          {run.cost_text !== null && <p className="note">{run.cost_text}</p>}
        </>
      )}
      {run?.state === 'finished' && <Finished run={run} />}
    </div>
  )
}
