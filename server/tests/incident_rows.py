"""Rows for the incident engine's tests: each helper stores one row and hands it back."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from beacon.evidence import Evidence, EvidenceStore, NewEvidence, Point

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
HAYES = Point(37.7766, -122.4236)


def at(minute: float) -> datetime:
    return NOON + timedelta(minutes=minute)


def mock_store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence" / "mock.sqlite3", "mock")


class Rows:
    """Stores rows in a mock store, each under a reference of its own."""

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store
        self._count = 0

    def _add(self, **fields: Any) -> Evidence:
        self._count += 1
        fields.setdefault("ref", f"row-{self._count}")
        fields.setdefault("origin", "mock")
        (row,) = self.store.add([NewEvidence(**fields)])
        return row

    def radio(
        self,
        minute: float,
        category: str = "fire",
        status: str = "unknown",
        *,
        units: tuple[str, ...] = (),
        street: str | None = None,
        block: int | None = None,
        cross_street: str | None = None,
        details: tuple[str, ...] = (),
        alarm: int | None = None,
        clarity: str = "clear",
    ) -> Evidence:
        place = None if street is None else f"{block} block of {street}" if block else street
        return self._add(
            kind="radio",
            source="openmhz",
            occurred_at=at(minute),
            confidence="medium" if clarity == "clear" else "low",
            model="fake",
            place=place,
            data={
                "event": "medical response" if category == "medical" else category,
                "category": category,
                "clarity": clarity,
                "status": status,
                "details": list(details),
                "units": list(units),
                "alarm": alarm,
                "street": street,
                "block": block,
                "cross_street": cross_street,
            },
        )

    def alert(
        self,
        minute: float,
        ref: str,
        *,
        until: float | None = 120,
        message_type: str = "Alert",
        replaces: tuple[str, ...] = (),
        point: Point | None = HAYES,
    ) -> Evidence:
        return self._add(
            kind="alert",
            source="nws",
            ref=ref,
            occurred_at=at(minute),
            until=None if until is None else at(until),
            confidence="official",
            point=point,
            data={
                "event": "Shelter In Place Warning",
                "message_type": message_type,
                "sender": "Department of Emergency Management",
                "headline": f"Headline of {ref}",
                "instruction": f"Instruction of {ref}",
                "replaces": list(replaces),
                "geometry": None,
            },
        )

    def traffic(
        self,
        minute: float,
        event_id: str = "511.org/1",
        *,
        created: float | None = None,
        road: str = "Hayes St",
        point: Point | None = HAYES,
        closed: bool = True,
    ) -> Evidence:
        return self._add(
            kind="traffic",
            source="511",
            ref=f"{event_id}@{at(minute).isoformat()}",
            occurred_at=at(minute),
            confidence="official",
            point=point,
            data={
                "event_id": event_id,
                "headline": f"Activity on {road}",
                "created": at(minute if created is None else created).isoformat(),
                "roads": [{"name": road, "closed": closed}],
                "closure": closed,
            },
        )

    def change(self, minute: float, about: Evidence, change: str = "not_listed") -> Evidence:
        return self._add(
            kind=about.kind,
            source=about.source,
            ref=f"{about.ref}#{change}@{at(minute).isoformat()}",
            occurred_at=at(minute),
            confidence="medium",
            point=about.point,
            data={"change": change, "about": about.ref},
        )

    def notice(
        self,
        minute: float,
        notice_type: str,
        *,
        hazard: str = "fire",
        streets: tuple[str, ...] = ("Hayes St",),
        **texts: Any,
    ) -> Evidence:
        return self._add(
            kind="notice",
            source="notices",
            occurred_at=at(minute),
            confidence="high",
            model="fake",
            url=f"https://notices.example/{self._count + 1}.pdf",
            data={
                "notice_type": notice_type,
                "hazard": hazard,
                "issuer": None,
                "title": None,
                "issued_at": None,
                "effective_until": None,
                "areas": [],
                "streets": list(streets),
                "instruction": None,
                "summary": None,
                "incomplete": [],
            }
            | texts,
        )

    def tap(self, minute: float, point: Point = HAYES) -> Evidence:
        return self._add(
            kind="tap",
            source="app",
            occurred_at=at(minute),
            confidence="low",
            origin="live",
            point=point,
            data={"accuracy_m": 10, "photo": None, "clip": None, "media_kept_until": None},
        )

    def aircraft(
        self,
        minute: float,
        point: Point,
        *,
        icao24: str = "abc123",
        altitude_m: float = 300.0,
        category: str | None = "rotorcraft",
    ) -> Evidence:
        return self._add(
            kind="aircraft",
            source="opensky",
            ref=f"{icao24}@{at(minute).timestamp()}",
            occurred_at=at(minute),
            confidence="high",
            point=point,
            data={
                "icao24": icao24,
                "callsign": "TEST1",
                "altitude_m": altitude_m,
                "category": category,
            },
        )
