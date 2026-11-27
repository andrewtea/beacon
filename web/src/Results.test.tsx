import { render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test } from 'vitest'
import App from './App.tsx'
import type { Scale } from './api/scale.ts'
import { parseRoute } from './lib/router.ts'
import { scaleAnswer } from './test/fixtures.ts'
import { MOCK_HEALTH, serve } from './test/server.ts'

beforeEach(() => {
  window.localStorage.clear()
  window.location.hash = ''
})

const run = scaleAnswer.run!
const NUMBERS = run.sections.flatMap((section) => section.numbers)

test('shows what a run is before any number, and every number with its sentence', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH, 'GET /api/scale': scaleAnswer })
  window.location.hash = '#/results'

  render(<App />)

  // What a scale run is, and that it is apart from what this server shows.
  expect(await screen.findByText(scaleAnswer.about_text)).toBeInTheDocument()
  const what = screen.getByRole('region', { name: 'What this run is' })
  expect(within(what).getByText(run.basis_text)).toBeInTheDocument()
  expect(within(what).getByText(run.state_text)).toBeInTheDocument()
  for (const caveat of run.caveats)
    expect(within(what).getByText(caveat)).toBeInTheDocument()
  // Whose record it is scored against, with its page.
  expect(
    within(what).getByRole('link', { name: run.scored_against.text }),
  ).toHaveAttribute('href', run.scored_against.url)
  // What the run is comes before the first number in the page.
  const first = screen.getByText(NUMBERS[0].label)
  expect(
    within(what).getByText(run.basis_text).compareDocumentPosition(first) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy()

  // No number is shown bare: each has its label, its value, and its sentence,
  // as the server gave them.
  for (const section of run.sections) {
    const region = screen.getByRole('region', { name: section.title })
    for (const number of section.numbers) {
      // A heading can have the words of a number under it: the label is the term.
      const label = within(region).getByText(number.label, { selector: 'dt' })
      const row = label.closest('.scale-number') as HTMLElement
      expect(within(row).getByText(number.value_text)).toBeInTheDocument()
      expect(within(row).getByText(number.text)).toBeInTheDocument()
    }
  }
  expect(document.querySelectorAll('.scale-number')).toHaveLength(
    NUMBERS.length,
  )
})

test('says with every group of numbers that a stand-in’s are no measurement of Beacon', async () => {
  serve({ 'GET /api/health': MOCK_HEALTH, 'GET /api/scale': scaleAnswer })
  window.location.hash = '#/results'

  render(<App />)

  await screen.findByText(scaleAnswer.about_text)
  expect(run.sections.length).toBeGreaterThan(3)
  for (const section of run.sections) {
    const region = screen.getByRole('region', { name: section.title })
    expect(within(region).getByText(run.reminder_text!)).toBeInTheDocument()
    expect(within(region).getByText(run.kind_text)).toHaveClass('stand-in')
    // A number the run does not have is "none", and never a zero.
    for (const value of region.querySelectorAll('.scale-value'))
      expect(value).toHaveClass('stand-in')
  }
  const cost = screen
    .getByRole('region', { name: 'Cost of hearing' })
    .querySelector('.scale-number')!
  expect(within(cost as HTMLElement).getByText('none')).toBeInTheDocument()
  expect(
    within(cost as HTMLElement).getByText(/^Not measured/),
  ).toBeInTheDocument()
})

test('marks nothing as a stand-in’s for a run of recorded radio heard by Muse Ember', async () => {
  const recorded: Scale = {
    ...scaleAnswer,
    run: {
      ...run,
      stand_in: false,
      kind_text: 'Muse Ember, recorded radio',
      basis_text: 'Recorded radio, for Muse Ember to hear.',
      reminder_text: null,
    },
  }
  serve({ 'GET /api/health': MOCK_HEALTH, 'GET /api/scale': recorded })
  window.location.hash = '#/results'

  render(<App />)

  await screen.findByText('Recorded radio, for Muse Ember to hear.')
  expect(screen.queryByText(run.reminder_text!)).not.toBeInTheDocument()
  expect(document.querySelector('.results-view .stand-in')).toBeNull()
  expect(screen.getAllByText('Muse Ember, recorded radio')).toHaveLength(
    run.sections.length + 1,
  )
})

test('a server that shows no run says so, and shows no number', async () => {
  const none: Scale = {
    about_text: scaleAnswer.about_text,
    run: null,
    none_text: 'This server shows no scale run’s results.',
  }
  serve({ 'GET /api/health': MOCK_HEALTH, 'GET /api/scale': none })
  window.location.hash = '#/results'

  render(<App />)

  expect(await screen.findByText(none.none_text!)).toBeInTheDocument()
  expect(screen.getByText(scaleAnswer.about_text)).toBeInTheDocument()
  expect(document.querySelector('.scale-number')).toBeNull()
  expect(screen.queryByText('What this run is')).not.toBeInTheDocument()
})

test('shows the server’s sentence when the results cannot be read, and nothing of a run', async () => {
  serve({
    'GET /api/health': MOCK_HEALTH,
    'GET /api/scale': () =>
      Response.json(
        { detail: 'The scale run’s results on this server cannot be read.' },
        { status: 503 },
      ),
  })
  window.location.hash = '#/results'

  render(<App />)

  expect(
    await screen.findByText(
      'The scale run’s results on this server cannot be read.',
    ),
  ).toBeInTheDocument()
  expect(document.querySelector('.scale-number')).toBeNull()
})

test('the results are a secondary page: linked from the foot, and not asked for or shown with the button', async () => {
  const asked = serve({
    'GET /api/health': MOCK_HEALTH,
    'GET /api/scale': scaleAnswer,
  })

  render(<App />)

  expect(
    await screen.findByRole('button', { name: 'What’s that?' }),
  ).toBeInTheDocument()
  const link = screen.getByRole('link', { name: 'Scale run results' })
  expect(link).toHaveAttribute('href', '#/results')
  expect(link.closest('footer')).not.toBeNull()
  // The link is at the foot and nowhere in the page's own content.
  expect(
    within(screen.getByRole('main')).queryByRole('link', {
      name: 'Scale run results',
    }),
  ).not.toBeInTheDocument()
  expect(asked.map((request) => request.key)).not.toContain('GET /api/scale')
  expect(document.querySelector('.scale-number')).toBeNull()
})

test('the results have one address, with nothing after it', () => {
  expect(parseRoute('#/results')).toEqual({ view: 'results' })
  expect(parseRoute('#/results/first')).toEqual({ view: 'unknown' })
})
