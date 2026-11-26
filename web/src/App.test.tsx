import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from './App.tsx'

test('shows the server version once the health check succeeds', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) =>
      String(input) === '/api/health'
        ? Response.json({ status: 'ok', version: '1.2.3' })
        : new Response('Not Found', { status: 404 }),
    ),
  )

  render(<App />)

  expect(
    await screen.findByText('Server connected (v1.2.3)'),
  ).toBeInTheDocument()
})

test('reports an unreachable server when the health check fails, with the reason', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('Bad Gateway', { status: 502 })),
  )

  render(<App />)

  expect(await screen.findByText('Server unreachable')).toBeInTheDocument()
  expect(
    screen.getByText('The server answered with HTTP 502.'),
  ).toBeInTheDocument()
})

test('says the server could not be reached when the request never arrives', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }),
  )

  render(<App />)

  expect(
    await screen.findByText('The server could not be reached.'),
  ).toBeInTheDocument()
})
