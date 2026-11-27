import type { Confidence, Credit, Source } from '../api/incidents.ts'
import { clock } from '../lib/time.ts'
import { Icon } from './Icon.tsx'

// Every claim the app shows carries how far it is trusted and where it is from.
// These are the pieces that say so, the same way everywhere.

const CONFIDENCE: Record<Confidence, { label: string; bars: number }> = {
  official: { label: 'Official source', bars: 4 },
  high: { label: 'High confidence', bars: 3 },
  medium: { label: 'Medium confidence', bars: 2 },
  low: { label: 'Low confidence', bars: 1 },
}

/** A confidence label: the word, and bars that say the same without colour. */
export function ConfidenceLabel({ level }: { level: Confidence }) {
  const { label, bars } = CONFIDENCE[level] ?? CONFIDENCE.low
  return (
    <span className={`confidence confidence-${level}`}>
      <span className="bars" aria-hidden="true">
        {[1, 2, 3, 4].map((bar) => (
          <span key={bar} className={bar <= bars ? 'bar on' : 'bar'} />
        ))}
      </span>
      {label}
    </span>
  )
}

/**
 * Says that something is a stand-in's: from the invented scenario, or written by
 * the stand-in that answers in place of Muse Ember. `byModel` is the kind of
 * Ember client that wrote it, when one did.
 */
export function StandInTag({ byModel }: { byModel?: string | null }) {
  return (
    <span className="tag stand-in">
      {byModel === 'fake' ? 'Invented · stand-in for Ember' : 'Invented'}
    </span>
  )
}

/** A stand-in's part in what is shown, in the server's own sentence. */
export function StandInNote({ text }: { text: string | null }) {
  if (text === null || text === '') return null
  return <p className="note stand-in-note">{text}</p>
}

/**
 * Who investigated or decided, and a stand-in's part in what is shown, each in
 * the server's sentence and each said once: when a stand-in investigated, the
 * stand-in's sentence already begins with who did.
 */
export function WhoNote({
  who,
  standIn,
}: {
  who: string | null
  standIn: string | null
}) {
  const repeated = who !== null && standIn !== null && standIn.includes(who)
  return (
    <>
      {who !== null && who !== '' && !repeated && <p className="who">{who}</p>}
      <StandInNote text={standIn} />
    </>
  )
}

/** An address a link may open: a web page's, and nothing a page could run. */
function webAddress(url: string | null): string | null {
  return url !== null && /^https?:\/\//i.test(url) ? url : null
}

/** The words a source's terms require beside what is shown from it. The words
 * are shown whether or not their address is one a link may open. A credit that
 * comes in pieces is shown piece by piece, each link going where its own words
 * say: a license's name to the license, and not to the database beside it. */
export function CreditLine({ credit }: { credit: Credit | null }) {
  if (credit === null) return null
  if (credit.parts.length > 0)
    return (
      <span className="credit">
        {credit.parts.map((part, index) => {
          const address = webAddress(part.url)
          return address === null ? (
            <span key={index}>{part.text}</span>
          ) : (
            <a key={index} href={address} target="_blank" rel="noreferrer">
              {part.text}
            </a>
          )
        })}
      </span>
    )
  const address = webAddress(credit.url)
  if (address === null) return <span className="credit">{credit.text}</span>
  return (
    <a className="credit" href={address} target="_blank" rel="noreferrer">
      {credit.text}
    </a>
  )
}

function sameSource(a: Source, b: Source): boolean {
  return (
    a.label === b.label &&
    a.url === b.url &&
    a.stand_in === b.stand_in &&
    a.model === b.model &&
    a.credit?.text === b.credit?.text
  )
}

/**
 * The sources a claim rests on: each feed's label, its link when the original is
 * public, the credit its terms require, and whether a stand-in produced it. Rows
 * of one feed that read the same are listed once.
 */
export function Sources({
  sources,
  linkText = 'Open the source',
}: {
  sources: Source[]
  linkText?: string
}) {
  const listed = sources.filter(
    (source, index) =>
      sources.findIndex((other) => sameSource(other, source)) === index,
  )
  if (listed.length === 0) return null
  return (
    <ul className="sources">
      {listed.map((source, index) => (
        <li key={index}>
          <span className="source-label">{source.label}</span>
          {source.stand_in && <StandInTag byModel={source.model} />}
          {webAddress(source.url) !== null && (
            <a
              className="source-link"
              href={webAddress(source.url) ?? undefined}
              target="_blank"
              rel="noreferrer"
            >
              {linkText}
              <Icon name="external" size={14} />
            </a>
          )}
          <CreditLine credit={source.credit} />
        </li>
      ))}
    </ul>
  )
}

/** One thing a source said: when, the words, how far it is trusted, and where
 * it is from. */
export function Line({
  at,
  text,
  confidence,
  sources,
}: {
  at: string
  text: string
  confidence: Confidence
  sources: Source[]
}) {
  return (
    <li className="line">
      <div className="line-head">
        <time dateTime={at}>{clock(at)}</time>
        <ConfidenceLabel level={confidence} />
      </div>
      <p className="line-text">{text}</p>
      <Sources sources={sources} />
    </li>
  )
}
