import { useState, useSyncExternalStore } from 'react'
import type { Mode } from '../api/incidents.ts'
import { fetchPushInfo } from '../api/push.ts'
import { usePolled } from '../lib/poll.ts'
import {
  onPushChange,
  previewNotice,
  pushSnapshot,
  pushSupported,
  turnOff,
  turnOn,
} from '../lib/push.ts'
import { Icon } from './Icon.tsx'

// Notifications for the saved places. What Beacon keeps to send one, when it
// sends one, and what a notification says are the server's words (`about_text`,
// and the notice the service worker shows). The sentences written here are about
// this device and this browser, and about what the server has answered to it:
// whether notifications are on, why they cannot be, and whether the server has
// said that it deleted this device's address.
//
// **Notifications can be turned off whatever the server is doing**: the control
// is there whenever they are on, with the server unreachable, in another mode,
// sending none, or not answering a request that is under way. And nothing here
// says that a deletion happened before the server answered that it did.

const INFO_MS = 10 * 60_000

const UNSUPPORTED =
  'This browser cannot receive notifications from Beacon here. On an iPhone or iPad, add Beacon to the Home Screen and open it from there.'
const BLOCKED =
  'Notifications from this site are blocked in this browser. Allow them in the browser’s settings for this site, then turn them on here.'
const NO_WORKER =
  'This copy of Beacon could not start the service worker that receives a notification. It needs HTTPS, or localhost.'
const NOT_KEPT =
  'This browser would not keep the change. Notifications are as they were.'
const OVERTAKEN =
  'Notifications were turned off on this device while this was under way. They are off.'
const SAID = {
  blocked: BLOCKED,
  'no-worker': NO_WORKER,
  overtaken: OVERTAKEN,
  failed: NOT_KEPT,
}
const NO_WORDS =
  'This device has no words for a notification yet. They are kept once Beacon’s server has been told.'
const NOT_SHOWN =
  'This browser would not show a notification just now. Whether one from Beacon would be shown is the browser’s to say: look at its settings for this site.'
const HELD =
  'This browser has not let go of a subscription that is no longer wanted, or has not said whether it holds one. A notification can be sent to this device through it only while Beacon’s server has this device’s address, and one that was already sent can still arrive. Beacon tries again while it is open.'
// What a server of each mode is called.
const SERVER = { mock: 'the invented scenario', live: 'real sources' }

function permission(): NotificationPermission | null {
  return typeof Notification === 'undefined' ? null : Notification.permission
}

export function Notifications({
  mode,
}: {
  // The mode the server said it is in, or null when that is not known.
  mode: Mode | null
}) {
  const status = useSyncExternalStore(onPushChange, pushSnapshot)
  // What a press met, with whether notifications were on when it did: it is
  // about that press, and is not shown once they are turned on or off.
  const [met, setMet] = useState<{ on: Mode | null; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const supported = pushSupported()
  const info = usePolled(
    `push:${supported ? (mode ?? 'unknown') : 'unsupported'}`,
    // A browser that cannot be notified, and a server that has not said what
    // it is, are asked nothing.
    async (signal) =>
      supported && mode !== null ? await fetchPushInfo(signal) : null,
    () => INFO_MS,
  ).data
  const problem = met !== null && met.on === status.on ? met.text : null

  const press = async (work: () => Promise<string | null>) => {
    setBusy(true)
    setMet(null)
    // Whatever the browser does, the controls come back.
    const text = await work().catch(() => NOT_KEPT)
    setBusy(false)
    setMet(text === null ? null : { on: pushSnapshot().on, text })
  }
  const on = (shown: Mode) =>
    press(async () => {
      const came = await turnOn(shown)
      return came === 'on' ? null : SAID[came]
    })
  // A browser that would not let go is said by the status, for as long as it
  // is so.
  const off = () =>
    press(async () => ((await turnOff()) === 'failed' ? NOT_KEPT : null))
  const show = (shown: Mode) =>
    press(async () => {
      let came: 'shown' | 'no-words'
      try {
        came = await previewNotice(shown)
      } catch {
        // The browser refuses when its leave was taken back since the page was
        // drawn, and where it shows none at all.
        return NOT_SHOWN
      }
      return came === 'no-words' ? NO_WORDS : null
    })

  const blocked = status.on !== null && permission() !== 'granted'
  // The addresses a server has yet to answer that it deleted: this server's,
  // and the other mode's, which only that server can delete.
  const here = status.forget.filter((owner) => owner === mode).length
  const elsewhere = [
    ...new Set(status.forget.filter((owner) => owner !== mode)),
  ]
  return (
    <section className="panel notify" aria-labelledby="notify-heading">
      <h2 id="notify-heading">
        <Icon name="bell" />
        Notifications
      </h2>
      {!supported ? (
        <p className="meta">{UNSUPPORTED}</p>
      ) : mode === null || info === null ? (
        <p className="meta" role="status">
          Beacon says what it keeps for notifications once it has heard from its
          server.
        </p>
      ) : !info.enabled ? (
        <p className="meta">{info.text}</p>
      ) : (
        <p className="meta about">{info.about_text}</p>
      )}
      {status.on !== null ? (
        <>
          <p className="notify-state" role="status">
            Notifications are on for this device.{' '}
            {blocked
              ? BLOCKED
              : status.told === 'told'
                ? status.places === 0
                  ? 'With no place saved, Beacon’s server has no address for this device.'
                  : `Beacon’s server has this device’s address and where ${status.places === 1 ? 'your saved place is' : `your ${status.places} saved places are`}.`
                : status.told === 'not-yet'
                  ? 'Beacon’s server is yet to be told.'
                  : null}
          </p>
          {status.told === 'failed' && (
            <p className="note problem" role="status">
              {status.problem} Beacon tries again while it is open.
            </p>
          )}
          <div className="notify-actions">
            <button
              type="button"
              className="action"
              disabled={busy}
              onClick={() => void off()}
            >
              Turn notifications off
            </button>
            {!blocked &&
              mode !== null &&
              status.on === mode &&
              status.told === 'told' &&
              status.places > 0 && (
                <button
                  type="button"
                  className="action"
                  disabled={busy}
                  onClick={() => void show(mode)}
                >
                  Show a preview of one
                </button>
              )}
          </div>
        </>
      ) : (
        <>
          {supported && mode !== null && info !== null && info.enabled && (
            <div className="notify-actions">
              <button
                type="button"
                className="action"
                disabled={busy}
                onClick={() => void on(mode)}
              >
                <Icon name="bell" />
                Turn notifications on
              </button>
            </div>
          )}
          {here > 0 ? (
            <p className="note problem" role="status">
              Notifications are off on this device. Beacon’s server has not yet
              answered that it deleted this device’s address.{' '}
              {status.told === 'failed' ? `${status.problem} ` : ''}
              Beacon asks again while it is open.
            </p>
          ) : (
            status.deleted && (
              <p className="note" role="status">
                Notifications are off on this device. Beacon’s server has
                answered that it deleted this device’s address.
              </p>
            )
          )}
        </>
      )}
      {status.held && (
        <p className="note problem" role="status">
          {HELD}
        </p>
      )}
      {elsewhere.map((owner) => (
        <p className="note problem" role="status" key={owner}>
          Beacon’s server for {SERVER[owner]} has not yet answered that it
          deleted an address of this device. Only that server can, so Beacon
          asks when it is next shown it.
        </p>
      ))}
      {problem !== null && (
        <p className="note problem" role="alert">
          {problem}
        </p>
      )}
    </section>
  )
}
