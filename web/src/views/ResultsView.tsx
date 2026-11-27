import { fetchScale, type ScaleRun, type ScaleSection } from '../api/scale.ts'
import { Icon } from '../components/Icon.tsx'
import { SCALE_MS } from '../lib/cadence.ts'
import { usePolled } from '../lib/poll.ts'
import { href } from '../lib/router.ts'
import { clock } from '../lib/time.ts'

// The results of one scale run: a secondary page, opened from the foot of the
// app and from nowhere on the way to an answer. Every sentence on it about the
// run is the server's, and no number is shown without the sentence that says
// what it is. A run a stand-in had a part in says so at its head, with every
// group of numbers, and beside every value, so that a number read by itself,
// with the rest scrolled away, is not taken for Beacon's on real radio.

function Kind({ run }: { run: ScaleRun }) {
  return (
    <span className={run.stand_in ? 'tag stand-in' : 'tag'}>
      {run.kind_text}
    </span>
  )
}

function Section({
  section,
  run,
  index,
}: {
  section: ScaleSection
  run: ScaleRun
  index: number
}) {
  const heading = `scale-section-${index}`
  return (
    <section className="panel scale-section" aria-labelledby={heading}>
      <div className="scale-section-head">
        <h2 id={heading}>{section.title || 'Other numbers'}</h2>
        <Kind run={run} />
      </div>
      {run.reminder_text !== null && (
        <p className="note stand-in-note">{run.reminder_text}</p>
      )}
      <dl className="scale-numbers">
        {section.numbers.map((number) => (
          <div key={number.key} className="scale-number">
            <dt>{number.label}</dt>
            <dd
              className={run.stand_in ? 'scale-value stand-in' : 'scale-value'}
            >
              <span className="scale-amount">{number.value_text}</span>
              {/* A row scrolled to, or captured by itself, still says whose
                  number it is. */}
              {run.stand_in && (
                <span className="scale-kind">{run.kind_text}</span>
              )}
            </dd>
            <dd className="scale-text">{number.text}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function Run({ run }: { run: ScaleRun }) {
  return (
    <>
      <section className="panel" aria-labelledby="scale-run-heading">
        <div className="scale-section-head">
          <h2 id="scale-run-heading">What this run is</h2>
          <Kind run={run} />
        </div>
        <p className={run.stand_in ? 'note stand-in-note' : 'note'}>
          {run.basis_text}
        </p>
        <dl className="facts">
          <dt>Day of DataSF’s record</dt>
          <dd>
            <time dateTime={run.day}>{run.day}</time>
          </dd>
          <dt>Run</dt>
          <dd>{run.name}</dd>
          <dt>Results written</dt>
          <dd>
            <time dateTime={run.made_at}>{clock(run.made_at)}</time>
          </dd>
          <dt>Scored against</dt>
          <dd>
            <a href={run.scored_against.url} rel="noreferrer">
              {run.scored_against.text}
            </a>
          </dd>
        </dl>
        <p>{run.state_text}</p>
        {run.caveats.length > 0 && (
          <>
            <h3>What holds for every number</h3>
            <ul className="scale-caveats">
              {run.caveats.map((caveat) => (
                <li key={caveat}>{caveat}</li>
              ))}
            </ul>
          </>
        )}
      </section>
      {run.sections.map((section, index) => (
        <Section
          key={section.title}
          section={section}
          run={run}
          index={index}
        />
      ))}
    </>
  )
}

export function ResultsView() {
  const polled = usePolled('scale', fetchScale, () => SCALE_MS)
  const answer = polled.data
  return (
    <div className="view results-view">
      <a className="action back" href={href.home}>
        <Icon name="back" size={16} />
        Back to the button
      </a>
      <h1>Scale run results</h1>
      {polled.error !== null && (
        <p className="note problem" role="status">
          {polled.error.message}
          {answer !== null && ' What is shown may be out of date.'}
        </p>
      )}
      {answer === null && polled.error === null && (
        <p className="meta" role="status">
          Asking for the results…
        </p>
      )}
      {answer !== null && (
        <>
          <p className="lede">{answer.about_text}</p>
          {answer.none_text !== null && (
            <p className="note">{answer.none_text}</p>
          )}
          {answer.run !== null && <Run run={answer.run} />}
        </>
      )}
    </div>
  )
}
