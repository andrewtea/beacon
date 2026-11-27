import { useState, useSyncExternalStore } from 'react'
import type { Mode } from '../api/incidents.ts'
import { fetchPushInfo } from '../api/push.ts'
import { usePolled } from '../lib/poll.ts'
import {
  keptNotice,
  onPushChange,
  pushSnapshot,
  pushSupported,
  turnOff,
  turnOn,
} from '../lib/push.ts'
import { Icon } from './Icon.tsx'

// Notifications for the saved places. What Beacon keeps to send one, when it
// sends one, and what a notification says are the server's words (`about_text`,
// and the notice the service worker shows). The sentences written here are about
// this device and this browser: whether notifications are on, and why they
// cannot be.

const INFO_MS = 10 * 60_000

const UNSUPPORTED =
  'This browser cannot receive notifications from Beacon here. On an iPhone or iPad, add Beacon to the Home Screen and open it from there.'
const BLOCKED =
  'Notifications from this site are blocked in this browser. Allow them in the browser’s settings for this site, then turn them on here.'
const NO_WORKER =
  'This copy of Beacon has no service worker, which is what receives a notification. The built app, served over HTTPS, has one.'
const NOT_KEPT =
  'This browser would not keep the change. Notifications are as they were.'
const SAID = { blocked: BLOCKED, 'no-worker': NO_WORKER, failed: NOT_KEPT }
const NO_WORDS =
  'This device has no words for a notification yet. They are kept once Beacon’s server has been told.'
const NOT_SHOWN =
  'This browser would not show a notification just now. Whether one from Beacon would be shown is the browser’s to say: look at its settings for this site.'

function permission(): NotificationPermission | null {
  return typeof Notification === 'undefined' ? null : Notification.permission
}

/** Shows, on this device, the notification a push from this server would show.
 * It is marked as a preview: no push was sent, and nothing was decided.
 * Answers with the sentence to show when it could not, else null. */
async function preview(): Promise<string | null> {
  const notice = await keptNotice()
  const registration = await navigator.serviceWorker.getRegistration()
  if (notice === null || registration === undefined) return NO_WORDS
  try {
    await registration.showNotification(notice.title, {
      body: `A preview, not a notification. What one would say: ${notice.text}`,
      tag: 'beacon-preview',
      icon: '/pwa-192x192.png',
    })
  } catch {
    // The browser refuses when its leave was taken back since the page was
    // drawn, and where it shows none at all.
    return NOT_SHOWN
  }
  return null
}

export function Notifications({
  mode,
  places,
}: {
  // The mode the server said it is in, or null when that is not known.
  mode: Mode | null
  // How many places are saved on this device.
  places: number
}) {
  const status = useSyncExternalStore(onPushChange, pushSnapshot)
  // What a press met, with whether notifications were on when it did: it is
  // about that press, and is not shown once they are turned on or off.
  const [met, setMet] = useState<{ wanted: boolean; text: string } | null>(null)
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
  const problem = met !== null && met.wanted === status.wanted ? met.text : null

  const press = async (work: () => Promise<string | null>) => {
    setBusy(true)
    setMet(null)
    // Whatever the browser does, the controls come back.
    const text = await work().catch(() => NOT_KEPT)
    setBusy(false)
    setMet(text === null ? null : { wanted: pushSnapshot().wanted, text })
  }
  const on = () =>
    press(async () => {
      const came = await turnOn()
      return came === 'on' ? null : SAID[came]
    })
  const off = () => press(async () => ((await turnOff()) ? null : NOT_KEPT))
  const show = () => press(preview)

  const blocked = status.wanted && permission() !== 'granted'
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
        <>
          <p className="meta">{info.about_text}</p>
          {status.wanted ? (
            <>
              <p className="notify-state" role="status">
                Notifications are on for this device.{' '}
                {blocked
                  ? BLOCKED
                  : places === 0
                    ? 'With no place saved, Beacon’s server keeps nothing of this device and sends it nothing.'
                    : status.told === 'told'
                      ? `Beacon’s server has this device’s address and where ${status.places === 1 ? 'your saved place is' : `your ${status.places} saved places are`}.`
                      : status.told === 'failed'
                        ? null
                        : 'Beacon’s server is being told.'}
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
                {!blocked && status.told === 'told' && places > 0 && (
                  <button
                    type="button"
                    className="action"
                    disabled={busy}
                    onClick={() => void show()}
                  >
                    Show a preview of one
                  </button>
                )}
              </div>
            </>
          ) : (
            <div className="notify-actions">
              <button
                type="button"
                className="action"
                disabled={busy}
                onClick={() => void on()}
              >
                <Icon name="bell" />
                Turn notifications on
              </button>
            </div>
          )}
        </>
      )}
      {status.told === 'failed' && !status.wanted && (
        <p className="note problem" role="status">
          Notifications are off on this device, and nothing can arrive.{' '}
          {status.problem} Beacon tries again while it is open, and the server
          deletes what it kept of this device when it hears.
        </p>
      )}
      {problem !== null && (
        <p className="note problem" role="alert">
          {problem}
        </p>
      )}
    </section>
  )
}
