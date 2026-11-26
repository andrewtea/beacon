import { useEffect } from 'react'
import './App.css'
import { fetchHealth } from './api/health.ts'
import { Icon } from './components/Icon.tsx'
import { usePolled } from './lib/poll.ts'
import { href, type Route, useRoute } from './lib/router.ts'
import { sweepTaps } from './lib/tapStore.ts'
import { Home } from './views/Home.tsx'
import { IncidentView } from './views/IncidentView.tsx'
import { TapView } from './views/TapView.tsx'
import { TraceView } from './views/TraceView.tsx'

const HEALTH_MS = 30_000

function View({ route, mode }: { route: Route; mode: 'live' | 'mock' | null }) {
  switch (route.view) {
    case 'home':
      return <Home mode={mode} />
    case 'tap':
      return <TapView key={route.id} id={route.id} />
    case 'trace':
      return <TraceView key={route.id} id={route.id} />
    case 'incident':
      return <IncidentView key={route.id} id={route.id} />
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
  const health = usePolled('health', fetchHealth, () => HEALTH_MS)
  const mode = health.data?.mode ?? null
  const unreachable = health.error !== null
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
      {mode === 'mock' && route.view === 'home' && (
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
        <View route={route} mode={mode} />
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
