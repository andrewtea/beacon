"""The evidence store: one SQLite row for each thing a source said.

A poller writes a row for a radio transmission, an aircraft position, an alert, a
traffic event, a notice, or a tap. The incident engine and the investigator's tools
read them back by kind, time, and place. A row says when and where, which source said
it and under which reference of the source's own, how far to trust it, and whether a
stand-in produced it.

There are two stores, and a file is one or the other for life. The *mock* store holds
what the invented scenario and stand-ins produce. The *live* store holds what real
sources and Ember produce, and refuses a row a stand-in had a part in, so nothing in
it has to be doubted on that account. `EvidenceStore.from_env` picks the file from
`BEACON_LIVE_SOURCES`, the setting that turns real sources on.

Rows are never changed. A source that revises what it said, as 511 does when it
updates an event, gets a new row under a new reference.
"""

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from beacon.ember.client import default_data_dir

# What a row is about. A kind can have more than one source over time: aircraft can
# come from OpenSky or airplanes.live.
Kind = Literal["radio", "aircraft", "alert", "traffic", "notice", "tap"]
# How far to trust what the row says, strongest first. `official` is an authority
# speaking for itself: a weather alert, a road closure, an evacuation order.
Confidence = Literal["official", "high", "medium", "low"]
# `mock` is the invented scenario. `live` is a real source, or a real person's tap.
Origin = Literal["live", "mock"]
# Which kind of Ember client wrote the row's data, when a model did.
ModelSource = Literal["live", "replay", "fake"]
Mode = Literal["live", "mock"]

LIVE_SOURCES_VARIABLE = "BEACON_LIVE_SOURCES"

_EARTH_RADIUS_M = 6_371_008.8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('radio', 'aircraft', 'alert', 'traffic', 'notice', 'tap')),
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    url TEXT,
    occurred_ms INTEGER NOT NULL,
    until_ms INTEGER,
    lat REAL,
    lon REAL,
    place TEXT,
    confidence TEXT NOT NULL CHECK (confidence IN ('official', 'high', 'medium', 'low')),
    origin TEXT NOT NULL CHECK (origin IN ('live', 'mock')),
    model TEXT CHECK (model IN ('live', 'replay', 'fake')),
    data TEXT NOT NULL,
    recorded_ms INTEGER NOT NULL,
    UNIQUE (source, ref),
    CHECK ((lat IS NULL) = (lon IS NULL))
);
CREATE INDEX IF NOT EXISTS evidence_by_kind_and_time ON evidence (kind, occurred_ms);
CREATE TABLE IF NOT EXISTS poller_state (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_MODE = "SELECT value FROM store_meta WHERE key = 'mode'"

_COLUMNS = (
    "id, kind, source, ref, url, occurred_ms, until_ms, lat, lon, place, confidence, "
    "origin, model, data, recorded_ms"
)


class EvidenceStoreError(Exception):
    """The store cannot take what it was given, or is not the store that was asked for."""


@dataclass(frozen=True)
class Point:
    """A WGS-84 position in decimal degrees."""

    lat: float
    lon: float

    def __post_init__(self) -> None:
        for name, value, limit in (("lat", self.lat, 90.0), ("lon", self.lon, 180.0)):
            number = isinstance(value, int | float) and not isinstance(value, bool)
            if not number or not math.isfinite(value) or abs(value) > limit:
                # The value is not repeated: a point can be where a person is standing.
                raise ValueError(f"{name} must be a number from -{limit:g} to {limit:g}.")

    def distance_m(self, other: "Point") -> float:
        """The great-circle distance to another point, in metres."""
        return _distance_m(self.lat, self.lon, other.lat, other.lon)


@dataclass(frozen=True, kw_only=True)
class NewEvidence:
    """One thing a source said, before it is stored."""

    kind: Kind
    # The feed it came from, such as `nws` or `opensky`.
    source: str
    # The source's own name for this item: an alert id, a call id, a transponder
    # address with a time. A source and a reference name one row, so a poller can
    # hand the store the same item twice and it is kept once.
    ref: str
    # When it happened, according to the source.
    occurred_at: datetime
    confidence: Confidence
    origin: Origin
    # Where a person can read the original, when it is public.
    url: str | None = None
    # When it stops applying, for a row that covers a span, such as an alert.
    until: datetime | None = None
    point: Point | None = None
    # The place in words, never finer than a block: `400 block of Hayes St`.
    place: str | None = None
    model: ModelSource | None = None
    # What the source said, in a shape that belongs to the row's kind and source.
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value, allowed in (
            ("kind", self.kind, Kind),
            ("confidence", self.confidence, Confidence),
            ("origin", self.origin, Origin),
        ):
            if value not in get_args(allowed):
                raise ValueError(f"{name} must be one of {get_args(allowed)}.")
        if self.model is not None and self.model not in get_args(ModelSource):
            raise ValueError(f"model must be one of {get_args(ModelSource)}, or None.")
        for name, value in (("source", self.source), ("ref", self.ref)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string.")
        for name, moment in (("occurred_at", self.occurred_at), ("until", self.until)):
            if moment is not None and (
                not isinstance(moment, datetime) or moment.utcoffset() is None
            ):
                raise ValueError(f"{name} must be a datetime with a time zone.")
        if not isinstance(self.data, Mapping):
            raise ValueError("data must be a mapping.")

    @property
    def stand_in(self) -> bool:
        """Whether a stand-in had a part in this row: the invented scenario, or a
        fake Ember. Such a row is labeled as one wherever it is shown."""
        return self.origin == "mock" or self.model == "fake"


@dataclass(frozen=True, kw_only=True)
class Evidence(NewEvidence):
    """A stored row."""

    id: int
    # When Beacon stored it, which is later than when it happened.
    recorded_at: datetime


class EvidenceStore:
    def __init__(self, path: Path, mode: Mode) -> None:
        """Open the store at `path`, beginning it when there is none.

        Raises EvidenceStoreError when the file was begun as the other mode's store."""
        if mode not in get_args(Mode):
            raise ValueError(f"mode must be one of {get_args(Mode)}.")
        self._path = path
        self._mode: Mode = mode
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()):
            pass

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "EvidenceStore":
        """The live store when `BEACON_LIVE_SOURCES` names a source, else the mock
        store, each in a file of its own under the data directory."""
        mode: Mode = "live" if env.get(LIVE_SOURCES_VARIABLE, "").strip() else "mock"
        return cls(default_data_dir(env) / "evidence" / f"{mode}.sqlite3", mode)

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def path(self) -> Path:
        return self._path

    def add(
        self,
        items: Iterable[NewEvidence],
        *,
        state: tuple[str, Mapping[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> list[Evidence]:
        """Store the rows that are new and return them. A row whose source and
        reference are already stored is left as it is and not returned.

        `state` is a poller's name and what it must remember between polls. It is
        saved in the same transaction, so a poller never remembers a poll whose rows
        were lost, or the other way round.

        Raises EvidenceStoreError when the live store is handed a stand-in's row, or
        a row's data is not JSON."""
        recorded_ms = _to_ms(datetime.now(UTC) if now is None else now)
        rows = [self._row(item, recorded_ms) for item in items]
        saved = None if state is None else (state[0], _json(state[1], "A poller's state"))
        added: list[Evidence] = []
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    cursor = db.execute(
                        "INSERT INTO evidence (kind, source, ref, url, occurred_ms, until_ms, "
                        "lat, lon, place, confidence, origin, model, data, recorded_ms) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (source, ref) DO NOTHING",
                        row,
                    )
                    if cursor.rowcount:
                        stored = db.execute(
                            f"SELECT {_COLUMNS} FROM evidence WHERE id = ?", (cursor.lastrowid,)
                        ).fetchone()
                        added.append(_evidence(stored))
                if saved is not None:
                    db.execute(
                        "INSERT INTO poller_state (name, state, updated_ms) VALUES (?, ?, ?) "
                        "ON CONFLICT (name) DO UPDATE SET state = excluded.state, "
                        "updated_ms = excluded.updated_ms",
                        (*saved, recorded_ms),
                    )
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        return added

    def state(self, name: str) -> dict[str, Any]:
        """What the poller of that name saved last, or an empty mapping."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT state FROM poller_state WHERE name = ?", (name,)).fetchone()
        return {} if row is None else json.loads(row[0])

    def find(self, source: str, ref: str) -> Evidence | None:
        """The row a source and a reference name, when it is stored. A poller asks
        before it pays to work an item out."""
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM evidence WHERE source = ? AND ref = ?", (source, ref)
            ).fetchone()
        return None if row is None else _evidence(row)

    def last_recorded_at(self) -> datetime | None:
        """The latest moment anything was stored: a row, or a poller's state. None
        for a store that holds nothing."""
        with closing(self._connect()) as db:
            (latest,) = db.execute(
                "SELECT MAX(ms) FROM (SELECT MAX(recorded_ms) AS ms FROM evidence "
                "UNION ALL SELECT MAX(updated_ms) FROM poller_state)"
            ).fetchone()
        return None if latest is None else _from_ms(latest)

    def count_by_source(self) -> dict[str, int]:
        """How many rows each source has."""
        with closing(self._connect()) as db:
            return dict(db.execute("SELECT source, COUNT(*) FROM evidence GROUP BY source"))

    def get(self, evidence_id: int) -> Evidence | None:
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
        return None if row is None else _evidence(row)

    def search(
        self,
        *,
        kinds: Sequence[Kind] | None = None,
        sources: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        near: Point | None = None,
        radius_m: float | None = None,
        include_unlocated: bool = False,
        newest_first: bool = False,
        limit: int = 500,
    ) -> list[Evidence]:
        """Rows by kind, source, time, and place, oldest first unless `newest_first`.

        `start` and `end` bound a window, and either can be left open. A row with an
        `until` is in the window when its span from `occurred_at` to `until` touches
        it, so an alert issued an hour ago that still applies is found. Any other row
        is in it when it happened inside it. Both ends are included.

        `near` with `radius_m` keeps the rows whose point is within that many metres.
        A row with no point, such as a city-wide alert or a transmission whose
        location was not found, is left out unless `include_unlocated` is true."""
        if (near is None) != (radius_m is None):
            raise ValueError("near and radius_m are given together.")
        if radius_m is not None and not (math.isfinite(radius_m) and radius_m >= 0):
            raise ValueError("radius_m must be zero or more.")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a whole number, one or more.")
        where: list[str] = []
        values: list[Any] = []
        for column, wanted in (("kind", kinds), ("source", sources)):
            if wanted is not None:
                where.append(f"{column} IN ({', '.join('?' * len(wanted))})")
                values.extend(wanted)
        if start is not None:
            where.append("COALESCE(until_ms, occurred_ms) >= ?")
            values.append(_to_ms(start))
        if end is not None:
            where.append("occurred_ms <= ?")
            values.append(_to_ms(end))
        if near is not None:
            located = "(lat IS NOT NULL AND distance_m(lat, lon, ?, ?) <= ?)"
            where.append(f"({located} OR lat IS NULL)" if include_unlocated else located)
            values.extend((near.lat, near.lon, radius_m))
        order = "DESC" if newest_first else "ASC"
        query = (
            f"SELECT {_COLUMNS} FROM evidence"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + f" ORDER BY occurred_ms {order}, id {order} LIMIT ?"
        )
        with closing(self._connect()) as db:
            rows = db.execute(query, (*values, limit)).fetchall()
        return [_evidence(row) for row in rows]

    def _row(self, item: NewEvidence, recorded_ms: int) -> tuple[Any, ...]:
        if self._mode == "live" and item.stand_in:
            raise EvidenceStoreError(
                "The live evidence store holds nothing a stand-in produced: this row "
                "comes from the mock scenario or a fake Ember, and belongs in the mock store."
            )
        return (
            item.kind,
            item.source,
            item.ref,
            item.url,
            _to_ms(item.occurred_at),
            None if item.until is None else _to_ms(item.until),
            None if item.point is None else item.point.lat,
            None if item.point is None else item.point.lon,
            item.place,
            item.confidence,
            item.origin,
            item.model,
            _json(item.data, "A row's data"),
            recorded_ms,
        )

    def _connect(self) -> sqlite3.Connection:
        # A connection for each operation, as the Ember ledger does, so one store is
        # safe to share between threads and between the server and the pollers.
        db = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            db.create_function("distance_m", 4, _distance_m, deterministic=True)
            # Read before anything is written, so a connection to a store that is
            # already begun takes no write lock to open.
            try:
                begun = db.execute(_MODE).fetchone()
            except sqlite3.OperationalError:
                begun = None
            if begun is None:
                db.executescript(_SCHEMA)
                # Of two connections that begin a file at once, one mode is kept.
                db.execute(
                    "INSERT OR IGNORE INTO store_meta (key, value) VALUES ('mode', ?)",
                    (self._mode,),
                )
                begun = db.execute(_MODE).fetchone()
            if begun[0] != self._mode:
                raise EvidenceStoreError(
                    f"{self._path} is the {begun[0]} evidence store, and was opened as the "
                    f"{self._mode} one. A store is one or the other for life."
                )
        except BaseException:
            db.close()
            raise
        return db


def _distance_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """The haversine distance between two positions, in metres."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    half_lat = (phi_b - phi_a) / 2
    half_lon = math.radians(lon_b - lon_a) / 2
    chord = math.sin(half_lat) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(half_lon) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(chord)))


def _to_ms(moment: datetime) -> int:
    if moment.utcoffset() is None:
        raise ValueError("A time must carry a time zone.")
    return round(moment.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _json(value: Mapping[str, Any], what: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        # The value is not repeated: it can be what a source or a person sent.
        raise EvidenceStoreError(f"{what} is not JSON.") from None


def _evidence(row: Sequence[Any]) -> Evidence:
    (
        evidence_id,
        kind,
        source,
        ref,
        url,
        occurred_ms,
        until_ms,
        lat,
        lon,
        place,
        confidence,
        origin,
        model,
        data,
        recorded_ms,
    ) = row
    return Evidence(
        id=evidence_id,
        kind=kind,
        source=source,
        ref=ref,
        url=url,
        occurred_at=_from_ms(occurred_ms),
        until=None if until_ms is None else _from_ms(until_ms),
        point=None if lat is None else Point(lat, lon),
        place=place,
        confidence=confidence,
        origin=origin,
        model=model,
        data=json.loads(data),
        recorded_at=_from_ms(recorded_ms),
    )
