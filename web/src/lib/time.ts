// Times are shown in the reader's own time zone, the way their phone shows them.

const CLOCK = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: '2-digit',
})
const CLOCK_SECONDS = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
})
const DAY = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
})

/** `2:14 PM`, with the day in front when it is not today. */
export function clock(iso: string, now: Date = new Date()): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const sameDay = at.toDateString() === now.toDateString()
  return sameDay ? CLOCK.format(at) : `${DAY.format(at)}, ${CLOCK.format(at)}`
}

/** `2:14:07 PM`, for the trace, where seconds tell calls apart. */
export function clockSeconds(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '' : CLOCK_SECONDS.format(at)
}

/** `$0.000412`: a call to Ember costs a fraction of a cent. */
export function usd(amount: number): string {
  return `$${amount.toFixed(6)}`
}

/** `12 min` or `1 h 5 min` between two moments. */
export function span(fromIso: string, toIso: string): string {
  const minutes = Math.max(
    0,
    Math.round((Date.parse(toIso) - Date.parse(fromIso)) / 60_000),
  )
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}
