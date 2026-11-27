import { useState } from 'react'
import type { Feature, Point } from '../api/incidents.ts'
import {
  DEFAULT_ZOOM,
  frameOf,
  type MapFrame,
  type Tiles,
  ZOOMS,
} from '../lib/tiles.ts'

// The picture of one incident's map: the street map's tiles, and over them what
// Beacon places, each thing under the number it has in the list below the map.
// The picture says nothing by itself: every word about a thing, its confidence,
// and its sources are in that list.

// The overlay is drawn in these units across and down.
const SIZE = 300

const at = (frame: MapFrame, place: Point): { x: number; y: number } => {
  const { x, y } = frame.project(place)
  return { x: x * SIZE, y: y * SIZE }
}

const inView = (spot: { x: number; y: number }): boolean =>
  spot.x >= 0 && spot.x <= SIZE && spot.y >= 0 && spot.y <= SIZE

// How far apart two numbers are kept, so that neither covers the other.
const APART = 19

/** Where a thing's number goes: on its point, on the newest position of an
 * aircraft that is in view, or at the north-west corner of the part of an area that is in view
 * (its middle is where the incident is). Null when there is nowhere to put it. */
function anchorOf(
  frame: MapFrame,
  feature: Feature,
): { x: number; y: number } | null {
  if (feature.point !== null) return at(frame, feature.point)
  if (feature.path.length > 0) {
    // The newest position that is in view, or failing one, the newest.
    const seen = feature.path.map((position) => at(frame, position))
    return seen.findLast(inView) ?? seen[seen.length - 1]
  }
  const corners = feature.rings.flat().map((point) => at(frame, point))
  if (corners.length === 0) return null
  const inside = (value: number) => Math.max(12, Math.min(SIZE - 12, value))
  return {
    x: inside(Math.min(...corners.map((corner) => corner.x))),
    y: inside(Math.min(...corners.map((corner) => corner.y))),
  }
}

/** Where each thing's number is drawn: its anchor, moved east while it would
 * cover a number that is already placed. Things at one point, as an incident
 * and a closure on its block are, then stand side by side. */
function marksOf(
  frame: MapFrame,
  features: Feature[],
): ({ x: number; y: number } | null)[] {
  const placed: { x: number; y: number }[] = []
  return features.map((feature) => {
    const anchor = anchorOf(frame, feature)
    if (anchor === null || !inView(anchor)) return null
    const spot = { ...anchor }
    while (
      placed.some(
        (other) => Math.hypot(other.x - spot.x, other.y - spot.y) < APART,
      )
    )
      spot.x += APART
    placed.push(spot)
    return spot
  })
}

function Shape({ frame, feature }: { frame: MapFrame; feature: Feature }) {
  if (feature.kind === 'alert_area') {
    const outline = feature.rings
      .map(
        (ring) =>
          ring
            .map((point, index) => {
              const { x, y } = at(frame, point)
              return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`
            })
            .join(' ') + ' Z',
      )
      .join(' ')
    return <path className="map-area" d={outline} fillRule="evenodd" />
  }
  if (feature.kind === 'aircraft') {
    const line = feature.path
      .map((seen) => {
        const { x, y } = at(frame, seen)
        return `${x.toFixed(2)},${y.toFixed(2)}`
      })
      .join(' ')
    return (
      <>
        <polyline className="map-path-casing" points={line} />
        <polyline className="map-path" points={line} />
      </>
    )
  }
  return null
}

export function MapCanvas({
  centre,
  features,
  tiles,
  invented,
}: {
  // The incident's own point: the middle of the view.
  centre: Point
  features: Feature[]
  // The street map's tiles, or null to draw on a plain ground.
  tiles: Tiles | null
  // Whether what is drawn is the invented scenario's: the picture then says so
  // itself, because the streets under it are real.
  invented: boolean
}) {
  const [zoom, setZoom] = useState<number>(DEFAULT_ZOOM)
  const frame = frameOf(centre, zoom, tiles)
  const level = ZOOMS.indexOf(zoom as (typeof ZOOMS)[number])
  const marks = marksOf(frame, features)
  return (
    <div className="map-wrap">
      <div className={tiles === null ? 'map-canvas plain' : 'map-canvas'}>
        {frame.tiles.map((tile) => (
          <img
            key={tile.key}
            className="map-tile"
            src={tile.src}
            alt=""
            draggable={false}
            decoding="async"
            // The tile server asks for a Referer, and gets the site's address
            // and no more: never the page, and so never a view's fragment.
            referrerPolicy="strict-origin-when-cross-origin"
            style={{
              left: `${tile.left * 100}%`,
              top: `${tile.top * 100}%`,
            }}
            // A tile that does not come leaves the plain ground showing.
            onError={(event) => {
              event.currentTarget.style.visibility = 'hidden'
            }}
          />
        ))}
        <svg
          className="map-overlay"
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          role="img"
          aria-label="What Beacon places of this incident. Each numbered thing is described in the list below the map."
        >
          {features.map((feature, index) => (
            <Shape key={index} frame={frame} feature={feature} />
          ))}
          {features.map((feature, index) => {
            const anchor = marks[index]
            if (anchor === null) return null
            return (
              <g
                key={index}
                className={`map-mark map-mark-${feature.kind}`}
                transform={`translate(${anchor.x.toFixed(2)} ${anchor.y.toFixed(2)})`}
              >
                <circle r="8.5" />
                <text textAnchor="middle" dy="0.35em">
                  {index + 1}
                </text>
              </g>
            )
          })}
        </svg>
        {invented && (
          <span className="tag stand-in map-invented">
            Invented scenario on a real street map
          </span>
        )}
        {tiles !== null && (
          <a
            className="map-credit"
            href={tiles.credit.url}
            target="_blank"
            rel="noreferrer"
          >
            {tiles.credit.text}
          </a>
        )}
      </div>
      <div
        className="map-zoom"
        role="group"
        aria-label="How much the map shows"
      >
        <button
          type="button"
          className="action"
          disabled={level <= 0}
          onClick={() => setZoom(ZOOMS[level - 1])}
        >
          Wider area
        </button>
        <button
          type="button"
          className="action"
          disabled={level >= ZOOMS.length - 1}
          onClick={() => setZoom(ZOOMS[level + 1])}
        >
          Closer in
        </button>
      </div>
    </div>
  )
}
