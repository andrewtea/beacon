import { useCallback, useEffect, useState } from 'react'
import './App.css'
import { fetchHealth, type Input } from './api/health.ts'
import { Icon } from './components/Icon.tsx'
import { usePolled } from './lib/poll.ts'
import { usePushSync } from './lib/pushSync.ts'
import { href, type Route, useRoute } from './lib/router.ts'
import { sweepTaps } from './lib/tapStore.ts'
import { Home } from './views/Home.tsx'
import { IncidentView } from './views/IncidentView.tsx'
import { MapView } from './views/MapView.tsx'
import { PlacesView } from './views/PlacesView.tsx'
import { ResultsView } from './views/ResultsView.tsx'
import { TapView } from './views/TapView.tsx'
import { TraceView } from './views/TraceView.tsx'

const HEALTH_MS = 30_000

function View({
  route,
  mode,
  input,
  repeatsText,
  onOtherMode,
}: {
  route: Route
  mode: 'live' | 'mock' | null
  input: Input
  repeatsText: string | null
  onOtherMode: () => void
}) {
  switch (route.view) {
    case 'home':
      return <Home mode={mode} input={input} onOtherMode={onOtherMode} />
    case 'tap':
      return <TapView key={route.id} id={route.id} repeatsText={repeatsText} />
    case 'trace':
      return (
        <TraceView key={route.id} id={route.id} repeatsText={repeatsText} />
      )
    case 'incident':
      return <IncidentView key={route.id} id={route.id} />
    case 'map':
      return <MapView key={route.id} id={route.id} />
    case 'places':
      return <PlacesView mode={mode} input={input} onOtherMode={onOtherMode} />
    case 'results':
      return <ResultsView />
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
  const repeatsText = unreachable ? null : (health.data?.repeats_text ?? null)
  // What the server takes from a visitor. Nothing is sent while the mode is not
  // known, so what is taken matters only once the server has said both.
  const input: Input =
    !unreachable && health.data?.input === 'invented' ? 'invented' : 'own'
  const frontText = unreachable ? null : (health.data?.front_text ?? null)
  const frontMore = unreachable ? null : (health.data?.front_more_text ?? null)
  // A tap the server no longer answers for is deleted from this device.
  useEffect(() => sweepTaps(), [])
  const askAgain = useCallback(() => setAsked((count) => count + 1), [])
  // The server is kept told of what this device wants of notifications.
  usePushSync(mode, askAgain)

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
            {/* On a server that tells the story again, the server's sentence
                about that: a tap sent now is gone when it does. */}
            {repeatsText !== null && <> {repeatsText}</>}
          </p>
        )}
      {/* On a server behind a host nobody has checked: the server's sentence
          about what it takes and why, on the pages a person sends something
          from. */}
      {frontText !== null &&
        (route.view === 'home' || route.view === 'places') && (
          <div className="note front-note">
            <p>{frontText}</p>
            {frontMore !== null && (
              <details>
                <summary>More about this</summary>
                <p>{frontMore}</p>
              </details>
            )}
          </div>
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
          input={input}
          repeatsText={repeatsText}
          onOtherMode={askAgain}
        />
      </main>
      <footer>
        <p>
          Beacon relays what authorities publish and tells nobody what to do.
          Every line names its source.
        </p>
        <p className="server">
          {health.data !== null && !unreachable && (
            <>
              <span>Server v{health.data.version}</span>
              {' · '}
            </>
          )}
          {/* A secondary page, linked from the foot of every view and from
              nowhere on the way to an answer. */}
          <a href={href.results}>Scale run results</a>
        </p>
      </footer>
    </div>
  )
}
