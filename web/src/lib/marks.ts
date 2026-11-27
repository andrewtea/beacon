import type { Feature, Point } from '../api/incidents.ts'
import type { MapFrame } from './tiles.ts'

// Where the things of one incident's map go on the picture.
//
// A thing that has a point is a dot at that point, exactly where the frame puts
// it, and the dot is never moved. Its number stands beside the dot, joined to it
// by a line, and it is the number that gives way: to the edge of the picture, to
// another number, and to another thing's dot.

// The overlay is drawn in these units across and down.
export const SIZE = 300
// A number's disc, how far apart two discs' middles are kept, and a dot.
export const DISC = 8.5
const APART = 19
export const DOT = 3.2
// How far from its dot a number first stands, and how much further each time
// round. More than `APART`, so that numbers of things at one point can stand in
// a row where the corner of the picture leaves one way out.
const REACH = 20

export type Spot = { x: number; y: number }

export const at = (frame: MapFrame, place: Point): Spot => {
  const { x, y } = frame.project(place)
  return { x: x * SIZE, y: y * SIZE }
}

const inView = (spot: Spot): boolean =>
  spot.x >= 0 && spot.x <= SIZE && spot.y >= 0 && spot.y <= SIZE

const apart = (a: Spot, b: Spot): number => Math.hypot(a.x - b.x, a.y - b.y)

type Anchor = {
  spot: Spot
  // Whether the spot is where the thing is. An area's is not: it is a corner of
  // its outline, where its number is put, and it claims no point.
  exact: boolean
}

/** Where a thing is on the picture: its point, or the newest position of an
 * aircraft that is in view. For an area, the north-west corner of the part of
 * it that is in view (its middle is where the incident is), which is only
 * where its number goes. Null when there is nowhere. */
function anchorOf(frame: MapFrame, feature: Feature): Anchor | null {
  if (feature.point !== null)
    return { spot: at(frame, feature.point), exact: true }
  if (feature.path.length > 0) {
    // The newest position that is in view, or failing one, the newest.
    const seen = feature.path.map((position) => at(frame, position))
    return {
      spot: seen.findLast(inView) ?? seen[seen.length - 1],
      exact: true,
    }
  }
  const corners = feature.rings.flat().map((point) => at(frame, point))
  if (corners.length === 0) return null
  const inside = (value: number) => Math.max(12, Math.min(SIZE - 12, value))
  return {
    spot: {
      x: inside(Math.min(...corners.map((corner) => corner.x))),
      y: inside(Math.min(...corners.map((corner) => corner.y))),
    },
    exact: false,
  }
}

// Where a number is tried, from its dot: north-east first, then round, then
// the ways between, then further out.
const WAYS = [-45, -135, 45, 135, 0, 180, -90, 90]
const AROUND: Spot[] = [1, 2, 3, 4, 5, 6].flatMap((ring) =>
  [...WAYS, ...WAYS.map((degrees) => degrees + 22.5)].map((degrees) => ({
    x: Math.cos((degrees * Math.PI) / 180) * REACH * ring,
    y: Math.sin((degrees * Math.PI) / 180) * REACH * ring,
  })),
)

const whole = (spot: Spot): boolean =>
  spot.x >= DISC + 1 &&
  spot.x <= SIZE - DISC - 1 &&
  spot.y >= DISC + 1 &&
  spot.y <= SIZE - DISC - 1

export type Mark = {
  // Where the thing is, as the frame projects it. Null for an area.
  dot: Spot | null
  // Where its number is drawn: whole inside the picture.
  number: Spot
}

/** Where each thing's dot and number are drawn, or null for a thing that is
 * out of view. A dot is the thing's own point. A number is put at the first
 * place around it that is whole inside the picture, clear of the numbers
 * already placed, and on no thing's dot. Things at one point, as an incident
 * and a 511 event on its block are, share a dot and have a number each. */
export function marksOf(frame: MapFrame, features: Feature[]): (Mark | null)[] {
  const anchors = features.map((feature) => {
    const anchor = anchorOf(frame, feature)
    return anchor !== null && inView(anchor.spot) ? anchor : null
  })
  const dots = anchors.flatMap((anchor) => (anchor?.exact ? [anchor.spot] : []))
  const placed: Spot[] = []
  return anchors.map((anchor) => {
    if (anchor === null) return null
    const tries = [
      // An area's number may stand on its corner. A point's stands beside it.
      ...(anchor.exact ? [] : [anchor.spot]),
      ...AROUND.map((offset) => ({
        x: anchor.spot.x + offset.x,
        y: anchor.spot.y + offset.y,
      })),
    ].filter(whole)
    const clear = (spot: Spot) =>
      placed.every((other) => apart(other, spot) >= APART)
    const number =
      tries.find(
        (spot) =>
          clear(spot) && dots.every((dot) => apart(dot, spot) > DISC + DOT),
      ) ??
      // With no place that is clear of every dot, one that is clear of the
      // numbers; and with none of those, the first that is inside the picture.
      tries.find(clear) ??
      tries[0]
    placed.push(number)
    return { dot: anchor.exact ? anchor.spot : null, number }
  })
}
