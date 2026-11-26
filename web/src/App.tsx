import { useEffect, useState } from 'react'
import './App.css'
import { fetchHealth } from './api/health.ts'
import { ApiError } from './api/http.ts'

type ServerState =
  | { kind: 'checking' }
  | { kind: 'connected'; version: string }
  | { kind: 'unreachable'; reason: string }

function useServerState(): ServerState {
  const [state, setState] = useState<ServerState>({ kind: 'checking' })

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(controller.signal)
      .then((health) =>
        setState({ kind: 'connected', version: health.version }),
      )
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setState({
          kind: 'unreachable',
          reason:
            error instanceof ApiError
              ? error.message
              : 'The server could not be reached.',
        })
      })
    return () => controller.abort()
  }, [])

  return state
}

const serverLabel = (state: ServerState): string => {
  switch (state.kind) {
    case 'checking':
      return 'Checking server…'
    case 'connected':
      return `Server connected (v${state.version})`
    case 'unreachable':
      return 'Server unreachable'
  }
}

export default function App() {
  const server = useServerState()

  return (
    <div className="app">
      <header className="bar">
        <img src="/favicon.svg" alt="" width="28" height="28" />
        <span className="name">Beacon</span>
      </header>
      <main>
        <h1>What’s that?</h1>
        <p className="lede">
          You hear a siren or see a helicopter. Beacon will say what is going
          on, which crews are on it, and roughly where, with a source on every
          claim.
        </p>
        <p className="note">
          This is the app’s shell. The What’s that? button is not built yet, so
          there is nothing to tap.
        </p>
      </main>
      <footer>
        <p className={`server ${server.kind}`} role="status">
          <span className="dot" aria-hidden="true" />
          {serverLabel(server)}
        </p>
        {server.kind === 'unreachable' && (
          <p className="reason">{server.reason}</p>
        )}
      </footer>
    </div>
  )
}
