import { Icon } from '../components/Icon.tsx'
import { IncidentLive } from '../components/IncidentLive.tsx'
import { href } from '../lib/router.ts'

/** One incident, opened from a card: its card, crisis mode, what authorities
 * published, and its timeline, read again while it is shown. */
export function IncidentView({ id }: { id: string }) {
  return (
    <div className="view incident-view">
      <a
        className="action back"
        href={href.home}
        onClick={(event) => {
          // Back to the tap the card was opened from, when there was one.
          if (window.history.length > 1) {
            event.preventDefault()
            window.history.back()
          }
        }}
      >
        <Icon name="back" size={16} />
        Back
      </a>
      <IncidentLive id={id} />
    </div>
  )
}
