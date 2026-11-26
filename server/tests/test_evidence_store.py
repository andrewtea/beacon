"""The evidence store: what it keeps, what it refuses, and how rows are found."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from beacon.evidence import EvidenceStore, EvidenceStoreError, NewEvidence, Point

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
FIRE = Point(37.7766, -122.4236)


def minutes(count: float) -> datetime:
    return NOON + timedelta(minutes=count)


def row(ref: str, /, **changes: object) -> NewEvidence:
    fields: dict = {
        "kind": "alert",
        "source": "nws",
        "ref": ref,
        "occurred_at": NOON,
        "confidence": "official",
        "origin": "live",
    }
    return NewEvidence(**(fields | changes))


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "live")


def refs(rows: list) -> list[str]:
    return [found.ref for found in rows]


def test_a_row_comes_back_as_it_was_stored(store: EvidenceStore) -> None:
    pacific = timezone(timedelta(hours=-8))
    new = NewEvidence(
        kind="radio",
        source="openmhz",
        ref="call-1",
        url="https://example.org/call-1",
        occurred_at=datetime(2026, 11, 27, 12, 0, 0, 250_000, tzinfo=pacific),
        until=minutes(30),
        point=FIRE,
        place="400 block of Hayes St",
        confidence="medium",
        origin="live",
        model="replay",
        data={"units": ["E36", "T03"], "event": "structure fire"},
    )

    (stored,) = store.add([new], now=minutes(1))

    assert stored == store.get(stored.id) == store.find("openmhz", "call-1")
    assert stored.recorded_at == minutes(1)
    # The same moment, told in UTC.
    assert stored.occurred_at == NOON + timedelta(milliseconds=250)
    assert (stored.kind, stored.source, stored.ref, stored.url) == (
        "radio",
        "openmhz",
        "call-1",
        "https://example.org/call-1",
    )
    assert (stored.until, stored.point, stored.place) == (
        minutes(30),
        FIRE,
        "400 block of Hayes St",
    )
    assert (stored.confidence, stored.origin, stored.model) == ("medium", "live", "replay")
    assert stored.data == {"units": ["E36", "T03"], "event": "structure fire"}
    assert not stored.stand_in


def test_an_item_a_source_sends_twice_is_kept_once_and_never_changed(
    store: EvidenceStore,
) -> None:
    (first,) = store.add([row("a", data={"headline": "first"})])

    added = store.add([row("a", data={"headline": "second"}), row("b")])

    assert refs(added) == ["b"]
    assert store.get(first.id).data == {"headline": "first"}
    # The same reference from another source is another row.
    assert refs(store.add([row("a", source="511", kind="traffic")])) == ["a"]
    assert store.count_by_source() == {"nws": 2, "511": 1}


def test_the_live_store_refuses_what_a_stand_in_produced(tmp_path: Path) -> None:
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    invented = row("invented", origin="mock")
    by_a_fake = row("by-a-fake", kind="radio", source="openmhz", model="fake")
    assert invented.stand_in
    assert by_a_fake.stand_in

    for stand_in in (invented, by_a_fake):
        with pytest.raises(EvidenceStoreError, match="nothing a stand-in produced"):
            # A real row beside it is not stored either: the poll is one transaction.
            live.add([row("real"), stand_in], state=("nws", {"seen": 1}))
    assert live.search() == []
    assert live.state("nws") == {}

    mock = EvidenceStore(tmp_path / "mock.sqlite3", "mock")
    assert refs(mock.add([invented, by_a_fake, row("real")])) == [
        "invented",
        "by-a-fake",
        "real",
    ]


def test_a_store_is_the_mock_one_or_the_live_one_for_life(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    EvidenceStore(path, "mock").add([row("invented", origin="mock")])

    with pytest.raises(EvidenceStoreError, match="is the mock evidence store"):
        EvidenceStore(path, "live")
    assert refs(EvidenceStore(path, "mock").search()) == ["invented"]


def test_a_pollers_state_is_saved_with_its_rows_or_not_at_all(store: EvidenceStore) -> None:
    assert store.state("nws") == {}
    store.add([row("a")], state=("nws", {"listed": ["a"]}))
    assert store.state("nws") == {"listed": ["a"]}

    # The second row cannot be stored, so neither the first nor the new state is.
    with pytest.raises(EvidenceStoreError, match="A row's data is not JSON"):
        store.add([row("b"), row("c", data={"when": NOON})], state=("nws", {"listed": ["a", "b"]}))

    assert refs(store.search()) == ["a"]
    assert store.state("nws") == {"listed": ["a"]}
    assert store.state("511") == {}


def test_data_that_is_not_json_is_refused_without_being_repeated(store: EvidenceStore) -> None:
    with pytest.raises(EvidenceStoreError) as refused:
        store.add([row("a", data={"speed": float("nan"), "patient": "Jane Doe"})])

    assert str(refused.value) == "A row's data is not JSON."
    # The encoder's error, which names the value, is not kept under it.
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


def test_search_finds_rows_whose_time_or_span_touches_the_window(store: EvidenceStore) -> None:
    store.add(
        [
            row("before", occurred_at=minutes(-1)),
            row("at-start", occurred_at=minutes(0)),
            row("at-end", occurred_at=minutes(10)),
            row("after", occurred_at=minutes(11)),
            # Issued an hour before the window, and in force until its first minute.
            row("still-in-force", occurred_at=minutes(-60), until=minutes(0)),
            row("ended-before", occurred_at=minutes(-60), until=minutes(-1)),
            # Begins inside the window and runs past it.
            row("runs-past", occurred_at=minutes(5), until=minutes(600)),
        ]
    )

    found = store.search(start=minutes(0), end=minutes(10))

    assert refs(found) == ["still-in-force", "at-start", "runs-past", "at-end"]
    assert refs(store.search(start=minutes(10))) == ["runs-past", "at-end", "after"]
    assert refs(store.search(end=minutes(-1))) == ["still-in-force", "ended-before", "before"]


def test_search_finds_rows_within_a_distance_of_a_point(store: EvidenceStore) -> None:
    # A degree of latitude is a 360th of the way round a sphere of radius
    # 6,371,008.8 m, which is 111,195.08 m, so 0.004 degrees is 444.78 m.
    north = Point(FIRE.lat + 0.004, FIRE.lon)
    # At this latitude a degree of longitude is cos(37.7766 deg) of that: 87,886 m.
    west = Point(FIRE.lat, FIRE.lon - 0.004)
    store.add(
        [
            row("north-445m", point=north),
            row("west-352m", point=west),
            row("city-wide"),
            row("oakland", point=Point(37.8044, -122.2712)),
        ]
    )

    assert FIRE.distance_m(north) == pytest.approx(444.78, abs=0.01)
    assert FIRE.distance_m(west) == pytest.approx(351.54, abs=0.05)
    assert refs(store.search(near=FIRE, radius_m=445)) == ["north-445m", "west-352m"]
    assert refs(store.search(near=FIRE, radius_m=444)) == ["west-352m"]
    assert refs(store.search(near=FIRE, radius_m=351)) == []
    # A row with no point is everywhere or nowhere, as the caller says.
    assert refs(store.search(near=FIRE, radius_m=351, include_unlocated=True)) == ["city-wide"]
    assert len(store.search()) == 4


def test_search_filters_by_kind_and_source_and_orders_and_limits(store: EvidenceStore) -> None:
    store.add(
        [
            row("alert-1", occurred_at=minutes(1)),
            row("plane-2", kind="aircraft", source="opensky", occurred_at=minutes(2)),
            row("road-3", kind="traffic", source="511", occurred_at=minutes(3)),
            row("alert-4", occurred_at=minutes(4)),
            # Two rows of one moment keep the order they were stored in.
            row("alert-4b", occurred_at=minutes(4)),
        ]
    )

    assert refs(store.search(kinds=["alert", "traffic"])) == [
        "alert-1",
        "road-3",
        "alert-4",
        "alert-4b",
    ]
    assert refs(store.search(sources=["opensky"])) == ["plane-2"]
    assert refs(store.search(kinds=[])) == []
    assert refs(store.search(limit=2)) == ["alert-1", "plane-2"]
    assert refs(store.search(newest_first=True, limit=3)) == ["alert-4b", "alert-4", "road-3"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"near": FIRE},
        {"radius_m": 100.0},
        {"near": FIRE, "radius_m": -1.0},
        {"near": FIRE, "radius_m": float("nan")},
        {"limit": 0},
        {"limit": True},
    ],
)
def test_search_refuses_arguments_it_cannot_use(store: EvidenceStore, arguments: dict) -> None:
    with pytest.raises(ValueError, match=r"radius_m|limit"):
        store.search(**arguments)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "rumor"}, "kind must be one of"),
        ({"confidence": "certain"}, "confidence must be one of"),
        ({"origin": "replayed"}, "origin must be one of"),
        ({"model": "mock"}, "model must be one of"),
        ({"source": ""}, "source must be a non-empty string"),
        ({"ref": None}, "ref must be a non-empty string"),
        ({"occurred_at": datetime(2026, 11, 27, 12, 0)}, "occurred_at must be a datetime with"),
        ({"until": datetime(2026, 11, 27, 12, 0)}, "until must be a datetime with"),
        ({"data": ["a"]}, "data must be a mapping"),
    ],
)
def test_a_row_that_does_not_say_what_it_is_cannot_be_made(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        row("a", **changes)


@pytest.mark.parametrize(
    ("lat", "lon"), [(90.01, 0.0), (0.0, -180.01), (float("nan"), 0.0), ("37.77", 0.0), (True, 0)]
)
def test_a_point_off_the_globe_is_refused_without_repeating_it(lat: object, lon: object) -> None:
    with pytest.raises(ValueError, match="must be a number from") as refused:
        Point(lat, lon)  # type: ignore[arg-type]

    assert "90.01" not in str(refused.value)
    assert "180.01" not in str(refused.value)
    assert "37.77" not in str(refused.value)


def test_the_database_itself_refuses_a_row_outside_the_schema(store: EvidenceStore) -> None:
    store.add([row("a")])

    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError):
        # A latitude with no longitude is not a place.
        db.execute("UPDATE evidence SET lat = 37.7")
