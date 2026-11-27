import { useEffect, useState } from 'react'
import './App.css'
import { fetchHealth } from './api/health.ts'
import { Icon } from './components/Icon.tsx'
import { usePolled } from './lib/poll.ts'
import { href, type Route, useRoute } from './lib/router.ts'
import { sweepTaps } from './lib/tapStore.ts'
import { Home } from './views/Home.tsx'
import { IncidentView } from './views/IncidentView.tsx'
import { PlacesView } from './views/PlacesView.tsx'
import { TapView } from './views/TapView.tsx'
import { TraceView } from './views/TraceView.tsx'

const HEALTH_MS = 30_000

function View({
  route,
  mode,
  onOtherMode,
}: {
  route: Route
  mode: 'live' | 'mock' | null
  onOtherMode: () => void
}) {
  switch (route.view) {
    case 'home':
      return <Home mode={mode} onOtherMode={onOtherMode} />
    case 'tap':
      return <TapView key={route.id} id={route.id} />
    case 'trace':
      return <TraceView key={route.id} id={route.id} />
    case 'incident':
      return <IncidentView key={route.id} id={route.id} />
    case 'places':
      return <PlacesView mode={mode} onOtherMode={onOtherMode} />
    case 'unknown':
      return (
        <div className="view">
          <h1>There is nothing at this address</h1>
          <a className="action" href={href.home}>
            <Icon name="back" size={16} />
            Back to the button
          </a>
        </div>
      )
  }
}

export default function App() {
  const route = useRoute()
  // Counted up when the server refused a tap for being in another mode than is
  // shown here. The health is then asked for anew, under a new key, and until
  // it answers the mode is not known and no tap is taken.
  const [asked, setAsked] = useState(0)
  const health = usePolled(`health:${asked}`, fetchHealth, () => HEALTH_MS)
  const unreachable = health.error !== null
  // The mode is what the server said at the latest asking. When that asking
  // failed, the mode is not known: the last answer is not carried over, because
  // the server that comes back can be another one, on other sources.
  const mode = unreachable ? null : (health.data?.mode ?? null)
  // A tap the server no longer answers for is deleted from this device.
  useEffect(() => sweepTaps(), [])

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href={href.home}>
          <img src="/favicon.svg" alt="" width="28" height="28" />
          <span className="name">Beacon</span>
        </a>
        {mode === 'mock' && (
          <span className="tag stand-in">Invented scenario</span>
        )}
      </header>
      {mode === 'mock' &&
        (route.view === 'home' || route.view === 'places') && (
          <p className="mock-banner">
            Beacon is running on an invented scenario, not real sources. The
            incidents, alerts, and notices it shows are made up and are not a
            record of anything that happened.
          </p>
        )}
      {unreachable && (
        <p className="note problem" role="status">
          {health.error?.message}
        </p>
      )}
      <main>
        <View
          route={route}
          mode={mode}
          onOtherMode={() => setAsked((count) => count + 1)}
        />
      </main>
      <footer>
        <p>
          Beacon relays what authorities publish and tells nobody what to do.
          Every line names its source.
        </p>
        {health.data !== null && !unreachable && (
          <p className="server">Server v{health.data.version}</p>
        )}
      </footer>
    </div>
  )
}
