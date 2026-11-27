import { useId, useState } from 'react'
import type { Credit, Feature, Point } from '../api/incidents.ts'
import {
  at,
  coversView,
  DISC,
  DOT,
  type Mark,
  marksOf,
  SIZE,
} from '../lib/marks.ts'
import {
  DEFAULT_ZOOM,
  frameOf,
  type MapFrame,
  type Tiles,
  ZOOMS,
} from '../lib/tiles.ts'
import { CreditLine } from './Provenance.tsx'

// The picture of one incident's map: the street map's tiles, and over them what
// Beacon places, each thing under the number it has in the list below the map.
// The picture says nothing by itself: every word about a thing, its confidence,
// and its sources are in that list. Where a thing's dot and its number go is
// `marksOf` (`lib/marks.ts`): the dot is the thing's own point and is not moved.
//
// The one sentence written here is about the picture and not the incident: that
// an area holds all of the view, when none of its outline is in view to carry
// its number (`coversView`).

/** The credits the sources of what is drawn ask for, each once. */
function creditsOf(features: Feature[]): Credit[] {
  const found: Credit[] = []
  for (const feature of features)
    for (const { credit } of feature.sources)
      if (
        credit !== null &&
        !found.some(
          (other) => other.text === credit.text && other.url === credit.url,
        )
      )
        found.push(credit)
  return found
}

const outlineOf = (frame: MapFrame, ring: Point[]): string =>
  ring
    .map((point, index) => {
      const { x, y } = at(frame, point)
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`
    })
    .join(' ') + ' Z'

/** An alert's area. A place is in it when it is inside the outline of one of
 * its polygons and in none of that polygon's holes, and that is what is
 * filled: each polygon is its outline with its own holes taken out of it, by
 * a mask. The rings are never put in one path and filled by parity, which
 * would have a second polygon cut a hole in the first where they overlap, and
 * two holes that overlap fill each other in. The polygons are filled as one
 * layer, so a place that two of them hold is no darker than its neighbours. */
function Area({ frame, feature }: { frame: MapFrame; feature: Feature }) {
  // An id that is good in `url(#...)` whatever React makes of it.
  const id = `area-${useId().replace(/[^A-Za-z0-9_-]/g, '')}`
  return (
    <g className="map-area">
      <defs>
        {feature.polygons.map(([outline, ...holes], index) => (
          <mask
            key={index}
            id={`${id}-${index}`}
            maskUnits="userSpaceOnUse"
            x="0"
            y="0"
            width={SIZE}
            height={SIZE}
          >
            <path
              className="map-area-in"
              d={outlineOf(frame, outline)}
              fill="#fff"
              fillRule="evenodd"
            />
            {holes.map((hole, inner) => (
              <path
                key={inner}
                className="map-area-out"
                d={outlineOf(frame, hole)}
                fill="#000"
                fillRule="evenodd"
              />
            ))}
          </mask>
        ))}
      </defs>
      <g className="map-area-fill">
        {feature.polygons.map((_, index) => (
          <rect
            key={index}
            width={SIZE}
            height={SIZE}
            mask={`url(#${id}-${index})`}
          />
        ))}
      </g>
      {feature.polygons.flatMap((rings, index) =>
        rings.map((ring, inner) => (
          <path
            key={`${index}-${inner}`}
            className="map-area-outline"
            d={outlineOf(frame, ring)}
          />
        )),
      )}
    </g>
  )
}

function Shape({ frame, feature }: { frame: MapFrame; feature: Feature }) {
  if (feature.kind === 'alert_area')
    return <Area frame={frame} feature={feature} />
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
  // itself, whatever it is drawn on.
  invented: boolean
}) {
  const [zoom, setZoom] = useState<number>(DEFAULT_ZOOM)
  const frame = frameOf(centre, zoom, tiles)
  const level = ZOOMS.indexOf(zoom as (typeof ZOOMS)[number])
  const marks = marksOf(frame, features)
  // A number that does not stand on the place it is about is joined to it.
  const led = (mark: Mark | null): mark is Mark =>
    mark !== null &&
    (mark.number.x !== mark.anchor.x || mark.number.y !== mark.anchor.y)
  // The areas that hold all of the view, by their numbers in the list.
  const covering = features.flatMap((feature, index) =>
    coversView(frame, feature) ? [index + 1] : [],
  )
  const credits = creditsOf(features)
  return (
    <div className="map-wrap">
      <div className={tiles === null ? 'map-canvas plain' : 'map-canvas'}>
        <div className="map-ground">
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
            {marks.map(
              (mark, index) =>
                led(mark) && (
                  <g key={index}>
                    <line
                      className="map-leader-casing"
                      x1={mark.anchor.x.toFixed(2)}
                      y1={mark.anchor.y.toFixed(2)}
                      x2={mark.number.x.toFixed(2)}
                      y2={mark.number.y.toFixed(2)}
                    />
                    <line
                      className="map-leader"
                      x1={mark.anchor.x.toFixed(2)}
                      y1={mark.anchor.y.toFixed(2)}
                      x2={mark.number.x.toFixed(2)}
                      y2={mark.number.y.toFixed(2)}
                    />
                  </g>
                ),
            )}
            {marks.map(
              (mark, index) =>
                mark !== null &&
                mark.dot && (
                  <circle
                    key={index}
                    className="map-dot"
                    cx={mark.anchor.x.toFixed(2)}
                    cy={mark.anchor.y.toFixed(2)}
                    r={DOT}
                  />
                ),
            )}
            {features.map((feature, index) => {
              const mark = marks[index]
              if (mark === null) return null
              return (
                <g
                  key={index}
                  className={`map-mark map-mark-${feature.kind}`}
                  transform={`translate(${mark.number.x.toFixed(2)} ${mark.number.y.toFixed(2)})`}
                >
                  <circle r={DISC} />
                  <text textAnchor="middle" dy="0.35em">
                    {index + 1}
                  </text>
                </g>
              )
            })}
          </svg>
          {invented && (
            <span className="tag stand-in map-invented">Invented scenario</span>
          )}
        </div>
        {/* The words the tiles' terms and each source's terms ask for, in the
            picture's own frame, along its lower edge, where they cover nothing
            that is drawn: a source's are here whenever something of that
            source's is drawn, with a street map under it or not. */}
        {(tiles !== null || credits.length > 0) && (
          <p className="map-credits">
            {tiles !== null && (
              <a
                className="credit"
                href={tiles.credit.url}
                target="_blank"
                rel="noreferrer"
              >
                {tiles.credit.text}
              </a>
            )}
            {credits.map((credit) => (
              <CreditLine
                key={`${credit.text} ${credit.url}`}
                credit={credit}
              />
            ))}
          </p>
        )}
      </div>
      {covering.map((number) => (
        <p key={number} className="note map-covering">
          The area numbered {number} in the list below holds all of this
          picture: no part of its outline is in view, so it has no number on the
          picture.
        </p>
      ))}
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
