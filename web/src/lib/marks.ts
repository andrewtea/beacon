import type { Feature, Point } from '../api/incidents.ts'
import type { MapFrame } from './tiles.ts'

// Where the things of one incident's map go on the picture.
//
// A number is about one place on the picture, its anchor, and an anchor is
// never moved. For a thing that has a point the anchor is that point, exactly
// where the frame puts it, and it is drawn as a dot. For an area it is a point
// of the area's own outline that is in view, and never a point that is on
// neither its outline nor its fill. For an aircraft none of whose positions is
// in view it is a point of its drawn line that is, with no dot. The number is what gives way: to the edge
// of the picture, to another number, and to a dot. A number that does not
// stand on its anchor is joined to it by a line.

// The overlay is drawn in these units across and down.
export const SIZE = 300
// A number's disc, how far apart two discs' middles are kept, and a dot.
export const DISC = 8.5
export const APART = 19
export const DOT = 3.2
// How far from its anchor a number first stands, and how much further each
// time round. More than `APART`, so that numbers of things at one point can
// stand in a row where the corner of the picture leaves one way out.
const REACH = 20
// The nearest a number's middle comes to the picture's edge: its disc is whole
// inside.
const MARGIN = DISC + 1

export type Spot = { x: number; y: number }

export const at = (frame: MapFrame, place: Point): Spot => {
  const { x, y } = frame.project(place)
  return { x: x * SIZE, y: y * SIZE }
}

const inView = (spot: Spot): boolean =>
  spot.x >= 0 && spot.x <= SIZE && spot.y >= 0 && spot.y <= SIZE

const apart = (a: Spot, b: Spot): number => Math.hypot(a.x - b.x, a.y - b.y)

/** The part of the line from `a` to `b` that is in the picture, `inset` in
 * from its edges, or null when none of it is. */
function clip(a: Spot, b: Spot, inset: number): [Spot, Spot] | null {
  const low = inset
  const high = SIZE - inset
  const dx = b.x - a.x
  const dy = b.y - a.y
  let from = 0
  let to = 1
  for (const [step, room] of [
    [-dx, a.x - low],
    [dx, high - a.x],
    [-dy, a.y - low],
    [dy, high - a.y],
  ]) {
    if (step === 0) {
      if (room < 0) return null
      continue
    }
    const where = room / step
    if (step < 0) {
      if (where > to) return null
      from = Math.max(from, where)
    } else {
      if (where < from) return null
      to = Math.min(to, where)
    }
  }
  return [
    { x: a.x + from * dx, y: a.y + from * dy },
    { x: a.x + to * dx, y: a.y + to * dy },
  ]
}

/** The parts of a line through `corners` that are in the picture, `inset` in
 * from its edges, in the line's own order. */
function partsIn(corners: Spot[], inset: number): [Spot, Spot][] {
  const parts: [Spot, Spot][] = []
  for (const [index, corner] of corners.slice(1).entries()) {
    const part = clip(corners[index], corner, inset)
    if (part !== null) parts.push(part)
  }
  return parts
}

/** The parts of an area's outline (every ring of every polygon, holes
 * included) that are in the picture, `inset` in from its edges. */
function outlineIn(
  frame: MapFrame,
  feature: Feature,
  inset: number,
): [Spot, Spot][] {
  return feature.polygons.flatMap((rings) =>
    rings.flatMap((ring) =>
      partsIn(
        ring.map((point) => at(frame, point)),
        inset,
      ),
    ),
  )
}

const middleOf = ([a, b]: [Spot, Spot]): Spot => ({
  x: (a.x + b.x) / 2,
  y: (a.y + b.y) / 2,
})

function inRing(ring: Spot[], spot: Spot): boolean {
  let inside = false
  for (const [index, b] of ring.slice(1).entries()) {
    const a = ring[index]
    if (
      a.y > spot.y !== b.y > spot.y &&
      spot.x < a.x + ((spot.y - a.y) / (b.y - a.y)) * (b.x - a.x)
    )
      inside = !inside
  }
  return inside
}

/** Whether an area holds the whole picture: none of its outline is in view,
 * and the middle of the picture is inside the outline of one of its polygons
 * and in none of that polygon's holes. With no outline in view, what holds for
 * the middle holds for all of the picture. Such an area has no number on the
 * picture, because no point of the picture is more its own than another. */
export function coversView(frame: MapFrame, feature: Feature): boolean {
  if (feature.polygons.length === 0) return false
  if (outlineIn(frame, feature, 0).length > 0) return false
  const middle = { x: SIZE / 2, y: SIZE / 2 }
  return feature.polygons.some((rings) => {
    const [outline, ...holes] = rings.map((ring) =>
      ring.map((point) => at(frame, point)),
    )
    return (
      inRing(outline, middle) && !holes.some((hole) => inRing(hole, middle))
    )
  })
}

type Anchor = {
  spot: Spot
  // Whether the spot is the thing's own point, which is drawn as a dot: a
  // place, a 511 event, a position an aircraft was seen at. A point of an
  // area's outline has no dot, and neither has a point of the line between two
  // positions of an aircraft, where nobody saw it.
  dot: boolean
}

/** The place on the picture a thing's number is about.
 *
 * - A thing with a point: the point.
 * - An aircraft: the newest position it was seen at that is in view. With none
 *   in view, the middle of the newest stretch of its drawn line that is: the
 *   line between two positions can cross a picture that neither is in, and a
 *   line that is drawn has its number. That middle is no sighting and gets no
 *   dot.
 * - An area: the middle of the longest stretch of its outline that is in view.
 *
 * A stretch is taken far enough in from the picture's edge for a number to
 * stand on it when there is such a stretch. Null when nothing of the thing is
 * in view to put a number to: a point out of view, an aircraft whose line does
 * not cross the picture, and an area with none of its outline in view. */
function anchorOf(frame: MapFrame, feature: Feature): Anchor | null {
  if (feature.point !== null) {
    const spot = at(frame, feature.point)
    return inView(spot) ? { spot, dot: true } : null
  }
  if (feature.path.length > 0) {
    const seen = feature.path.map((position) => at(frame, position))
    const spot = seen.findLast(inView)
    if (spot !== undefined) return { spot, dot: true }
    for (const inset of [MARGIN, 0]) {
      const newest = partsIn(seen, inset).at(-1)
      if (newest !== undefined) return { spot: middleOf(newest), dot: false }
    }
    return null
  }
  for (const inset of [MARGIN, 0]) {
    const parts = outlineIn(frame, feature, inset)
    if (parts.length === 0) continue
    const longest = parts.reduce((best, part) =>
      apart(...part) > apart(...best) ? part : best,
    )
    return { spot: middleOf(longest), dot: false }
  }
  return null
}

// Where a number is tried, from its anchor: north-east first, then round, then
// the ways between, then further out.
const WAYS = [-45, -135, 45, 135, 0, 180, -90, 90]
const AROUND: Spot[] = [1, 2, 3, 4, 5, 6].flatMap((ring) =>
  [...WAYS, ...WAYS.map((degrees) => degrees + 22.5)].map((degrees) => ({
    x: Math.cos((degrees * Math.PI) / 180) * REACH * ring,
    y: Math.sin((degrees * Math.PI) / 180) * REACH * ring,
  })),
)

// Every place a number can stand in the picture, a number's width apart: what
// is tried when there is no room around the anchor.
const STEPS = Math.floor((SIZE - 2 * MARGIN) / APART)
const EVERYWHERE: Spot[] = Array.from({ length: STEPS + 1 }, (_, row) =>
  Array.from({ length: STEPS + 1 }, (_, column) => ({
    x: MARGIN + column * APART,
    y: MARGIN + row * APART,
  })),
).flat()

const whole = (spot: Spot): boolean =>
  spot.x >= MARGIN &&
  spot.x <= SIZE - MARGIN &&
  spot.y >= MARGIN &&
  spot.y <= SIZE - MARGIN

export type Mark = {
  // The place the number is about, which is never moved: the thing's own
  // point, or a point of an area's outline.
  anchor: Spot
  // Whether the anchor is the thing's own point, and so drawn as a dot.
  dot: boolean
  // Where the number is drawn: whole inside the picture. When it is not the
  // anchor, a line joins the two.
  number: Spot
}

/** Where each thing's number is drawn and what it is joined to, or null for a
 * thing with nothing in view to put a number to (and for an area that holds
 * the whole picture: `coversView`).
 *
 * A number is put at the first place that is whole inside the picture, clear
 * of the numbers already placed, and on no dot: on an area's anchor itself,
 * then around the anchor, then anywhere in the picture, nearest first. Things
 * at one point, as an incident and a 511 event on its block are, share a dot
 * and have a number each. Only when the picture has no place left that is
 * clear of every other number does one number stand on another. */
export function marksOf(frame: MapFrame, features: Feature[]): (Mark | null)[] {
  const anchors = features.map((feature) => anchorOf(frame, feature))
  const dots = anchors.flatMap((anchor) => (anchor?.dot ? [anchor.spot] : []))
  const placed: Spot[] = []
  return anchors.map((anchor) => {
    if (anchor === null) return null
    const { spot } = anchor
    const tries = [
      // A number may stand on an outline or a line. A point's stands beside it.
      ...(anchor.dot ? [] : [spot]),
      ...AROUND.map((offset) => ({
        x: spot.x + offset.x,
        y: spot.y + offset.y,
      })),
      ...EVERYWHERE.toSorted((a, b) => apart(a, spot) - apart(b, spot)),
    ].filter(whole)
    const clear = (tried: Spot) =>
      placed.every((other) => apart(other, tried) >= APART)
    const number =
      tries.find(
        (tried) =>
          clear(tried) && dots.every((dot) => apart(dot, tried) > DISC + DOT),
      ) ??
      // With no place that is clear of every dot, one that is clear of the
      // numbers. With none of those the picture is full, and the number
      // stands on another: there is nowhere else for it.
      tries.find(clear) ??
      tries[0]
    placed.push(number)
    return { anchor: spot, dot: anchor.dot, number }
  })
}
