import { type Feature, fetchIncidentMap } from '../api/incidents.ts'
import { Icon } from '../components/Icon.tsx'
import { IncidentCard } from '../components/IncidentCard.tsx'
import { MapCanvas } from '../components/MapCanvas.tsx'
import { ConfidenceLabel, Sources } from '../components/Provenance.tsx'
import { OPEN_MS, OVER_MS } from '../lib/cadence.ts'
import { featureName } from '../lib/labels.ts'
import { usePolled } from '../lib/poll.ts'
import { href } from '../lib/router.ts'
import { TILES, type Tiles } from '../lib/tiles.ts'

// The map of one incident, opened from its card. It is secondary: it shows that
// incident and nothing else, and there is no map of the city. Every word about
// a thing on it is the server's, shown with its confidence and its sources in
// the list under the picture.

function Legend({ features }: { features: Feature[] }) {
  return (
    <ol className="map-legend">
      {features.map((feature, index) => (
        <li key={index} className="line">
          <p className="map-legend-head">
            <span className={`map-key map-key-${feature.kind}`}>
              {index + 1}
            </span>
            {featureName(feature.kind)}
          </p>
          <p className="line-text">{feature.text}</p>
          {feature.note !== null && <p className="note">{feature.note}</p>}
          <ConfidenceLabel level={feature.confidence} />
          <Sources sources={feature.sources} />
        </li>
      ))}
    </ol>
  )
}

export function MapView({
  id,
  tiles = TILES,
}: {
  id: string
  tiles?: Tiles | null
}) {
  const polled = usePolled(
    `map:${id}`,
    (signal) => fetchIncidentMap(id, signal),
    (answer) => (answer.incident.open ? OPEN_MS : OVER_MS),
  )
  // The server has no map at this address: there is no such incident, or it is
  // a medical response. Nothing of an earlier reading is shown then.
  const gone = polled.error?.status === 404
  const answer = gone ? null : polled.data
  const place = answer?.features.find((feature) => feature.kind === 'place')
  return (
    <div className="view map-view">
      <a
        className="action back"
        href={href.incident(id)}
        onClick={(event) => {
          // Back to the card the map was opened from, when there was one.
          if (window.history.length > 1) {
            event.preventDefault()
            window.history.back()
          }
        }}
      >
        <Icon name="back" size={16} />
        Back
      </a>
      <h1>Map of this incident</h1>
      {polled.error !== null && (
        <p className="note problem" role="status">
          {polled.error.message}
          {!gone && answer !== null && ' What is shown may be out of date.'}
        </p>
      )}
      {answer === null && polled.error === null && (
        <p className="meta" role="status">
          Asking what Beacon can place…
        </p>
      )}
      {answer !== null && (
        <>
          {answer.partial && (
            <p className="note">
              There was more evidence than Beacon reads at once, so something
              can be missing from this map.
            </p>
          )}
          {answer.text !== null && <p className="note">{answer.text}</p>}
          {place?.point != null && (
            <>
              <MapCanvas
                centre={place.point}
                features={answer.features}
                tiles={tiles}
                invented={answer.mode === 'mock'}
              />
              {tiles !== null && (
                <p className="meta">
                  {answer.mode === 'mock' &&
                    'The streets are real. What is drawn on them is invented. '}
                  The street map is loaded from the servers named on it, which
                  are asked for the streets around this incident. Beacon sends
                  them nothing of your taps or your places.
                </p>
              )}
              <section className="panel" aria-labelledby="legend-heading">
                <h2 id="legend-heading">What is on the map</h2>
                <p className="meta">
                  Only what Beacon has a point for is drawn. A tap is never
                  drawn, and a notice has no place: the incident’s own page has
                  what authorities published.
                </p>
                <Legend features={answer.features} />
              </section>
            </>
          )}
          <section className="panel" aria-labelledby="map-incident-heading">
            <h2 id="map-incident-heading">The incident</h2>
            <IncidentCard incident={answer.incident} heading="h3" link />
          </section>
        </>
      )}
    </div>
  )
}
