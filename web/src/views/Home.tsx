import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ApiError, OTHER_MODE } from '../api/http.ts'
import type { Input } from '../api/health.ts'
import type { Mode } from '../api/incidents.ts'
import { sendTap, type TapBody } from '../api/taps.ts'
import { Icon } from '../components/Icon.tsx'
import { locate, LocationError, MOCK_SPOTS, type Place } from '../lib/geo.ts'
import { drawPhoto, PhotoError } from '../lib/photo.ts'
import { CLIP_SECONDS, ClipError, recordClip } from '../lib/recorder.ts'
import { href, navigate } from '../lib/router.ts'
import {
  forgetTap,
  keepTap,
  subscribeTaps,
  tapsSnapshot,
} from '../lib/tapStore.ts'
import { clock } from '../lib/time.ts'
import { toBase64 } from '../lib/wav.ts'

type Phase =
  | { kind: 'idle' }
  | { kind: 'locating' }
  | { kind: 'listening' }
  | { kind: 'sending' }
  | { kind: 'failed'; message: string }

const BUSY: Record<Phase['kind'], string | null> = {
  idle: null,
  locating: 'Finding where you are…',
  listening: `Listening for ${CLIP_SECONDS} seconds…`,
  sending: 'Sending your tap…',
  failed: null,
}

/** Why a tap was not sent, in a sentence. The server's refusals are fixed
 * sentences that repeat nothing the tap held, and are shown as they are. */
function reason(error: unknown): string {
  if (
    error instanceof LocationError ||
    error instanceof ClipError ||
    error instanceof ApiError
  )
    return error.message
  return 'Beacon could not send this tap. Try again.'
}

/** The taps this device sent that the server still answers for. */
function YourTaps() {
  // Off the list from the moment a tap runs out: see `tapsSnapshot`.
  const taps = useSyncExternalStore(subscribeTaps, tapsSnapshot)
  if (taps.length === 0) return null
  return (
    <section className="panel your-taps" aria-labelledby="taps-heading">
      <h2 id="taps-heading">Your taps</h2>
      <p className="meta">
        Kept on this device only. Beacon keeps an answer for 23 hours at most
        and lists taps for nobody. A tap is deleted from this device when the
        server says it no longer has it, or 23 hours after it was sent, or the
        next time Beacon is opened after that.
      </p>
      <ul className="tap-list">
        {taps.map((tap) => (
          <li key={tap.id}>
            <a href={href.tap(tap.id)}>
              <Icon name="tap" />
              <time dateTime={tap.at}>{clock(tap.at)}</time>
              {tap.clip && <span className="tag">clip</span>}
              {tap.photo && <span className="tag">photo</span>}
            </a>
            <button
              type="button"
              className="quiet"
              onClick={() => forgetTap(tap.id)}
              aria-label={`Forget the tap of ${clock(tap.at)} on this device`}
            >
              <Icon name="close" size={16} />
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function Home({
  mode,
  input,
  mediaText = null,
  onOtherMode,
}: {
  // Where a clip or a photo added to a tap can be sent to Meta for a model to
  // say what is in it: the server's sentence about that, shown beside the
  // controls that add one, before one is added.
  mediaText?: string | null
  // What the server said it takes from a visitor at the latest asking, or null
  // when that is not known, and then no tap is taken. When it is only the
  // invented scenario's two places, the button sends a tap from the first of
  // them, and where the phone is, a sound, and a photo are neither asked for
  // nor sent. The phone is asked where it is only for `own`.
  input: Input | null
  // The mode the server said it is in at the latest asking, or null when that
  // is not known.
  mode: Mode | null
  // Called when the server refused a tap because it is in another mode than
  // the app was showing: whoever knows the mode asks for it again.
  onOtherMode?: () => void
}) {
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })
  const [withClip, setWithClip] = useState(false)
  const [photo, setPhoto] = useState<Blob | null>(null)
  const [photoProblem, setPhotoProblem] = useState<string | null>(null)
  // A picture that was chosen is being drawn again. Until that is done there is
  // no photo to send, so no tap is taken and no other picture chosen: a tap
  // sent now would go without it, and a second choice could finish first.
  const [drawing, setDrawing] = useState(false)
  const sending = useRef<AbortController | null>(null)
  const picker = useRef<HTMLInputElement | null>(null)
  // A tap that is on its way is called off when the view is left, and when
  // what the server says of its mode changes, to another mode or to not known:
  // a tap goes to the server its sender was told of when they pressed, or it
  // does not go. What they added to it stays chosen, for a tap they make once
  // the mode is known again and shown. A layout effect, so that it is done as
  // the change is drawn and not a frame after.
  useLayoutEffect(
    () => () => {
      // Only a tap that is under way: why an earlier one failed stays shown.
      if (sending.current === null) return
      sending.current.abort()
      sending.current = null
      setPhase({ kind: 'idle' })
    },
    [mode, input],
  )

  const busy = drawing ? 'Reading the picture…' : BUSY[phase.kind]
  // Whether the server runs on real sources or on the invented scenario is known
  // before a tap is taken, so that nobody sends where they are, a clip, or a
  // photo to the scenario without having been told that is what it is.
  // And so is what the server takes: the phone is not asked where it is for a
  // server that has not said it takes that.
  const ready = mode !== null && input !== null
  const invented = input === 'invented'

  const tap = async (given?: Place) => {
    if (busy !== null || mode === null || input === null) return
    // On a server that takes only the invented places, the button's tap is from
    // the first of them: the phone is not asked where it is.
    const from = given ?? (invented ? MOCK_SPOTS[0].place : undefined)
    const clipWanted = withClip && !invented
    // The mode that was on the screen when the button was pressed. The tap
    // names it, and a server in the other mode refuses the tap unread.
    const shown = mode
    const controller = new AbortController()
    sending.current = controller
    // Asked after every wait: a tap that was called off while it waited for
    // the phone's place, the microphone, or a photo's bytes sends nothing.
    const unlessCalledOff = () => {
      if (controller.signal.aborted)
        throw new DOMException('Aborted', 'AbortError')
    }
    try {
      // The sound is recorded while the phone finds where it is: a siren does
      // not wait.
      setPhase({
        kind: clipWanted ? 'listening' : from ? 'sending' : 'locating',
      })
      const [place, clip] = await Promise.all([
        from ?? locate(),
        clipWanted ? recordClip(CLIP_SECONDS, controller.signal) : null,
      ])
      unlessCalledOff()
      setPhase({ kind: 'sending' })
      const body: TapBody = { location: place }
      if (clip !== null) body.clip = toBase64(clip)
      if (photo !== null && !invented) {
        body.photo = toBase64(new Uint8Array(await photo.arrayBuffer()))
        unlessCalledOff()
      }
      const receipt = await sendTap(body, shown, controller.signal)
      keepTap({
        id: receipt.id,
        at: receipt.received_at,
        photo: receipt.photo,
        clip: receipt.clip,
        // As the server said of it, and only when it did.
        invented: receipt.invented_only === true,
      })
      navigate(href.tap(receipt.id))
    } catch (error) {
      const calledOff = controller.signal.aborted
      // Whatever else was under way for this tap is stopped: the microphone
      // is let go of when the place could not be had.
      controller.abort()
      if (!calledOff) setPhase({ kind: 'failed', message: reason(error) })
      // The server is in another mode than was shown: what is shown is wrong,
      // and is asked for again before another tap is taken.
      if (error instanceof ApiError && error.status === OTHER_MODE)
        onOtherMode?.()
    } finally {
      if (sending.current === controller) sending.current = null
    }
  }

  const choosePhoto = async (file: File | undefined) => {
    if (file === undefined || drawing) return
    setPhotoProblem(null)
    setDrawing(true)
    try {
      setPhoto(await drawPhoto(file))
    } catch (error) {
      setPhoto(null)
      setPhotoProblem(
        error instanceof PhotoError
          ? error.message
          : 'Beacon could not read that picture.',
      )
    } finally {
      setDrawing(false)
    }
  }

  return (
    <div className="view home">
      <div className="ask">
        <h1 className="sr-only">Beacon</h1>
        <button
          type="button"
          className={
            !ready
              ? 'beacon-button waiting'
              : busy === null
                ? 'beacon-button'
                : 'beacon-button busy'
          }
          onClick={() => void tap()}
          disabled={busy !== null || !ready}
        >
          <span className="rings" aria-hidden="true">
            <span />
            <span />
          </span>
          <span className="beacon-label">What’s that?</span>
        </button>
        <p className="ask-status" role="status" aria-live="polite">
          {busy ??
            (!ready
              ? 'Beacon takes a tap once it has heard from its server.'
              : invented
                ? `On this server the button sends a tap from an invented place: ${MOCK_SPOTS[0].name}. Where you are is not asked for and not sent.`
                : 'Heard a siren or saw a helicopter? Tap, and Beacon looks into what is going on near you.')}
        </p>
        {phase.kind === 'failed' && (
          <p className="note problem" role="alert">
            {phase.message}
          </p>
        )}
      </div>

      {/* Nothing is added to a tap, and no place of a person's own goes with
          one, on a server that takes only the invented places. */}
      {!invented && (
        <>
          <fieldset className="extras" disabled={busy !== null || !ready}>
            <legend>Add to your tap</legend>
            <label className="extra">
              <input
                type="checkbox"
                checked={withClip}
                onChange={(event) => setWithClip(event.target.checked)}
              />
              <Icon name="mic" />
              <span>{CLIP_SECONDS} seconds of sound</span>
            </label>
            <input
              ref={picker}
              className="sr-only"
              type="file"
              accept="image/*"
              capture="environment"
              tabIndex={-1}
              aria-hidden="true"
              onChange={(event) => {
                void choosePhoto(event.target.files?.[0])
                event.target.value = ''
              }}
            />
            {photo === null ? (
              <button
                type="button"
                className="extra"
                onClick={() => picker.current?.click()}
              >
                <Icon name="camera" />
                <span>A photo</span>
              </button>
            ) : (
              <button
                type="button"
                className="extra on"
                onClick={() => setPhoto(null)}
              >
                <Icon name="camera" />
                <span>Photo attached. Remove it</span>
              </button>
            )}
          </fieldset>
          {photoProblem !== null && (
            <p className="note problem" role="alert">
              {photoProblem}
            </p>
          )}
          {mediaText !== null && <p className="note media-note">{mediaText}</p>}
          <p className="meta privacy">
            A tap sends where you are, rounded to about a block, and whatever
            you add. A clip or a photo goes only with the tap, as evidence for
            it, and after an hour Beacon cannot read it and its sweep deletes
            it. Beacon keeps nothing that says who sent a tap.
          </p>
        </>
      )}

      {mode === 'mock' && (
        <section className="panel mock-spots" aria-labelledby="spots-heading">
          <h2 id="spots-heading">Try the invented scenario</h2>
          <p className="meta">
            {invented
              ? 'This server runs on a made-up fire, not real sources. Send a tap as if from one of these places.'
              : 'This server runs on a made-up fire, not real sources. If you are not in San Francisco, send a tap as if from one of these places.'}
          </p>
          <div className="spot-list">
            {MOCK_SPOTS.map((spot) => (
              <button
                key={spot.name}
                type="button"
                className="spot"
                disabled={busy !== null || !ready}
                onClick={() => void tap(spot.place)}
              >
                <Icon name="pin" />
                {spot.name}
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="panel places-link" aria-labelledby="places-heading">
        <h2 id="places-heading">Your places</h2>
        <p className="meta">
          Save a place, such as home, and see what Beacon has near it.
        </p>
        <a className="action" href={href.places}>
          <Icon name="pin" />
          Open your places
          <Icon name="forward" size={16} />
        </a>
      </section>

      <YourTaps />
    </div>
  )
}
