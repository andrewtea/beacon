import {
  type FormEvent,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react'
import { OTHER_MODE } from '../api/http.ts'
import type { Mode } from '../api/incidents.ts'
import { checkPlaces, type PlaceAnswer } from '../api/places.ts'
import { Icon } from '../components/Icon.tsx'
import { IncidentCard } from '../components/IncidentCard.tsx'
import { CreditLine } from '../components/Provenance.tsx'
import { PLACES_MS } from '../lib/cadence.ts'
import { locate, LocationError, MOCK_SPOTS } from '../lib/geo.ts'
import {
  MOST_PLACES,
  NAME_MOST,
  placesSnapshot,
  removePlace,
  savePlace,
  type SavedPlace,
  subscribePlaces,
} from '../lib/placeStore.ts'
import { usePolled } from '../lib/poll.ts'
import { href } from '../lib/router.ts'

// Saved places: a place a person named, kept on their device, and what Beacon
// has within a kilometre of it. Every sentence about an incident, a distance,
// or there being nothing near is the server's. The view lists nothing but the
// open incidents near a place the person saved: it is no list of what is going
// on in the city.

/** What Beacon has near one saved place, in the server's words. */
function Near({ answer }: { answer: PlaceAnswer }) {
  if (answer.near.length === 0)
    return answer.text === null ? null : <p>{answer.text}</p>
  return (
    <>
      {/* 511's, when a distance below is measured from a place of 511's. */}
      <CreditLine credit={answer.credit} />
      <ul className="near-list">
        {answer.near.map((item) => (
          <li key={item.incident.id}>
            <p className="near-distance">{item.text}</p>
            <IncidentCard incident={item.incident} heading="h3" link />
          </li>
        ))}
      </ul>
    </>
  )
}

/** The saved places, each with what the server says is near it, asked for again
 * while the view is open. */
function Checked({
  places,
  mode,
  onOtherMode,
}: {
  places: SavedPlace[]
  mode: Mode
  onOtherMode?: () => void
}) {
  // Where the places are, and nothing else of them, names the asking: an
  // answer is matched to its place by position, so an answer for other places
  // is never shown under these.
  const where = places.map((place) => `${place.lat},${place.lon}`).join(';')
  const polled = usePolled(
    `places:${mode}:${where}`,
    (signal) => checkPlaces(places, mode, signal),
    () => PLACES_MS,
  )
  const otherMode = polled.error?.status === OTHER_MODE
  // The server is in another mode than is shown here: what is shown is wrong,
  // and is asked for again.
  useEffect(() => {
    if (otherMode) onOtherMode?.()
  }, [otherMode, onOtherMode])

  const answer = polled.data
  // An answer that does not have one entry for each place is not shown under
  // any of them.
  const fits = answer !== null && answer.places.length === places.length
  // The place this browser would not take off its list, if there is one.
  const [stuck, setStuck] = useState<string | null>(null)
  return (
    <>
      {polled.error !== null && (
        <p className="note problem" role="status">
          {polled.error.message}
          {answer !== null && ' What is shown may be out of date.'}
        </p>
      )}
      {fits && <p className="note caveat">{answer.caveat_text}</p>}
      {fits && answer.partial && (
        <p className="note">
          There was more evidence than Beacon reads at once, so what is listed
          here can be incomplete.
        </p>
      )}
      {places.map((place, index) => (
        <section
          key={place.id}
          className="panel place"
          aria-labelledby={`place-${place.id}`}
        >
          <div className="place-head">
            <h2 id={`place-${place.id}`}>
              <Icon name="pin" />
              {place.name}
            </h2>
            <button
              type="button"
              className="quiet"
              onClick={() => setStuck(removePlace(place.id) ? null : place.id)}
              aria-label={`Remove ${place.name} from this device`}
            >
              <Icon name="close" size={16} />
            </button>
          </div>
          {stuck === place.id && (
            <p className="note problem" role="alert">
              This browser would not change what it keeps. The place is still
              saved on this device.
            </p>
          )}
          {fits ? (
            <Near answer={answer.places[index]} />
          ) : (
            polled.error === null && (
              <p className="meta" role="status">
                Asking what is near…
              </p>
            )
          )}
        </section>
      ))}
    </>
  )
}

type Saving =
  { kind: 'idle' } | { kind: 'locating' } | { kind: 'failed'; message: string }

const NOT_KEPT = 'This browser would not keep the place. Nothing was saved.'
const FULL = `Beacon keeps up to ${MOST_PLACES} places. Remove one to save another.`

/** Saving a place: where the phone is now, under a name. On the invented
 * scenario, also a place in it. */
function AddPlace({ mode, full }: { mode: Mode | null; full: boolean }) {
  const [name, setName] = useState('')
  const [saving, setSaving] = useState<Saving>({ kind: 'idle' })
  const busy = saving.kind === 'locating'
  // Whether the view is still open: a position that arrives after a person
  // left the view saves nothing.
  const open = useRef(true)
  useEffect(() => {
    open.current = true
    return () => {
      open.current = false
    }
  }, [])

  const keep = (where: { lat: number; lon: number }, called: string) => {
    const came = savePlace(called, where)
    if (came === 'saved') {
      setName('')
      setSaving({ kind: 'idle' })
    } else
      // `full` is another tab's doing: this one offers no saving at five.
      setSaving({ kind: 'failed', message: came === 'full' ? FULL : NOT_KEPT })
  }
  const saveHere = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || full) return
    setSaving({ kind: 'locating' })
    try {
      const here = await locate()
      if (open.current) keep(here, name)
    } catch (error) {
      if (!open.current) return
      setSaving({
        kind: 'failed',
        message:
          error instanceof LocationError
            ? error.message
            : 'Beacon could not save this place. Try again.',
      })
    }
  }

  return (
    <section className="panel add-place" aria-labelledby="add-heading">
      <h2 id="add-heading">Save a place</h2>
      {full ? (
        <p className="meta">{FULL}</p>
      ) : (
        <>
          <form onSubmit={(event) => void saveHere(event)}>
            <label htmlFor="place-name">Its name, for you only</label>
            <input
              id="place-name"
              type="text"
              value={name}
              maxLength={NAME_MOST}
              placeholder="Home"
              autoComplete="off"
              onChange={(event) => setName(event.target.value)}
            />
            <button type="submit" className="action" disabled={busy}>
              <Icon name="pin" />
              {busy ? 'Finding where you are…' : 'Save where I am now'}
            </button>
          </form>
          {mode === 'mock' && (
            <>
              <p className="meta">
                This server runs on a made-up fire. If you are not in San
                Francisco, save a place in the invented scenario.
              </p>
              <div className="spot-list">
                {MOCK_SPOTS.map((spot) => (
                  <button
                    key={spot.name}
                    type="button"
                    className="spot"
                    disabled={busy}
                    onClick={() =>
                      keep(spot.place, name.trim() === '' ? spot.name : name)
                    }
                  >
                    <Icon name="pin" />
                    Save “{spot.name}”
                  </button>
                ))}
              </div>
            </>
          )}
        </>
      )}
      {saving.kind === 'failed' && (
        <p className="note problem" role="alert">
          {saving.message}
        </p>
      )}
    </section>
  )
}

export function PlacesView({
  mode,
  onOtherMode,
}: {
  // The mode the server said it is in at the latest asking, or null when that
  // is not known: no place is sent to a server the person was not told of.
  mode: Mode | null
  onOtherMode?: () => void
}) {
  const places = useSyncExternalStore(subscribePlaces, placesSnapshot)
  return (
    <div className="view places-view">
      <a className="action back" href={href.home}>
        <Icon name="back" size={16} />
        Back to the button
      </a>
      <h1>Your places</h1>
      <p className="meta">
        A place and its name are kept on this device only. To see what is near
        one, Beacon is sent where it is, rounded to about a block, without its
        name, and keeps nothing of it.
      </p>
      {places.length > 0 &&
        (mode === null ? (
          <p className="note" role="status">
            Beacon asks what is near your places once it has heard from its
            server.
          </p>
        ) : (
          <Checked places={places} mode={mode} onOtherMode={onOtherMode} />
        ))}
      {places.length > 0 && mode === null && (
        <ul className="place-names">
          {places.map((place) => (
            <li key={place.id}>
              <Icon name="pin" />
              {place.name}
            </li>
          ))}
        </ul>
      )}
      <AddPlace mode={mode} full={places.length >= MOST_PLACES} />
    </div>
  )
}
