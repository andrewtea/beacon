"""The evidence store: what it keeps, what it refuses, and how rows are found."""

import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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


WEEK = timedelta(days=7)


def at_time(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def test_a_promise_to_leave_a_source_alone_is_made_once_and_stands_until_its_moment(
    store: EvidenceStore,
) -> None:
    until = NOON + timedelta(seconds=180)
    store.add([], state=("adsblol", {"other": 1}))

    assert store.reserve("adsblol", clock=at_time(NOON), seconds=180, longest=WEEK) is None
    # It is kept beside whatever else the poller saved, and stands for every caller.
    assert store.state("adsblol") == {"other": 1, "not_before": until.isoformat()}
    asked = NOON + timedelta(seconds=179)
    assert store.reserve("adsblol", clock=at_time(asked), seconds=3600, longest=WEEK) == until
    assert store.state("adsblol")["not_before"] == until.isoformat()
    # At its moment it is over, and the next is made. Another poller has its own.
    later = until + timedelta(hours=1)
    assert store.reserve("adsblol", clock=at_time(until), seconds=3600, longest=WEEK) is None
    assert store.reserve("511", clock=at_time(until), seconds=3600, longest=WEEK) is None
    assert store.state("adsblol")["not_before"] == later.isoformat()


def test_of_two_processes_that_find_the_time_come_one_makes_the_promise(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    until = NOON + timedelta(seconds=180)
    # Each with a store of its own on the one file, as a server and the one-shot
    # command have, and all asking at the same moment.
    stores = [EvidenceStore(path, "mock") for _ in range(8)]
    together = threading.Barrier(len(stores))

    def reserve(store: EvidenceStore) -> datetime | None:
        together.wait(5)
        return store.reserve("adsblol", clock=at_time(NOON), seconds=180, longest=WEEK)

    with ThreadPoolExecutor(len(stores)) as pool:
        answers = list(pool.map(reserve, stores))

    assert sorted(answers, key=lambda answer: answer is not None) == [None] + [until] * 7


def test_a_promise_is_counted_from_when_the_store_may_write_and_not_from_before_the_wait(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(path, "mock")
    wall = [NOON]
    reading = threading.Event()

    def clock() -> datetime:
        reading.set()
        return wall[0]

    # Another process is writing, and goes on for eleven seconds by the clock:
    # longer than the ten the promise is for.
    with closing(sqlite3.connect(path)) as other:
        other.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(
                lambda: store.reserve("adsblol", clock=clock, seconds=10, longest=WEEK)
            )
            # The clock is not read while the file is another's.
            assert not reading.wait(0.3)
            wall[0] = NOON + timedelta(seconds=11)
            other.execute("ROLLBACK")
            assert waiting.result(10) is None

    # Ten seconds from when the promise was made: a second later it still stands.
    made = NOON + timedelta(seconds=11)
    assert store.state("adsblol") == {"not_before": (made + timedelta(seconds=10)).isoformat()}
    a_second_on = at_time(made + timedelta(seconds=1))
    assert store.reserve("adsblol", clock=a_second_on, seconds=10, longest=WEEK) is not None


def test_a_promise_is_moved_on_and_never_back_and_never_further_than_the_longest(
    store: EvidenceStore,
) -> None:
    until = NOON + timedelta(seconds=180)
    noon = at_time(NOON)
    store.reserve("adsblol", clock=noon, seconds=180, longest=WEEK)

    store.postpone("adsblol", until=NOON + timedelta(seconds=1), clock=noon, longest=WEEK)
    assert store.state("adsblol") == {"not_before": until.isoformat()}
    two_days = NOON + timedelta(days=2)
    store.postpone("adsblol", until=two_days, clock=noon, longest=WEEK)
    assert store.state("adsblol") == {"not_before": two_days.isoformat()}
    store.postpone("adsblol", until=NOON + timedelta(days=400), clock=noon, longest=WEEK)
    assert store.state("adsblol") == {"not_before": (NOON + WEEK).isoformat()}
    # A promise for longer than the longest is made for the longest.
    assert store.reserve("511", clock=noon, seconds=WEEK.total_seconds() * 2, longest=WEEK) is None
    assert store.state("511") == {"not_before": (NOON + WEEK).isoformat()}
    # A poller with no promise yet gets one.
    store.postpone("nws", until=until, clock=noon, longest=WEEK)
    assert store.state("nws") == {"not_before": until.isoformat()}


@pytest.mark.parametrize("look", ["reserve", "postpone"])
def test_a_promise_too_far_off_is_cut_to_the_longest_once_and_ends_then(
    store: EvidenceStore, look: str
) -> None:
    # A year off: from a clock that has since been set back.
    far = NOON + timedelta(days=365)
    store.add([], state=("adsblol", {"not_before": far.isoformat()}))

    def looked(now: datetime) -> datetime | None:
        if look == "reserve":
            return store.reserve("adsblol", clock=at_time(now), seconds=180, longest=WEEK)
        store.postpone("adsblol", until=now, clock=at_time(now), longest=WEEK)
        return datetime.fromisoformat(store.state("adsblol")["not_before"])

    # The longest from the first look, written down, and not from each look after.
    assert looked(NOON) == NOON + WEEK
    assert store.state("adsblol") == {"not_before": (NOON + WEEK).isoformat()}
    assert looked(NOON + timedelta(days=6)) == NOON + WEEK
    # When that week is over the source is asked again.
    assert store.reserve("adsblol", clock=at_time(NOON + WEEK), seconds=180, longest=WEEK) is None


def test_a_closed_store_makes_no_promise(store: EvidenceStore) -> None:
    store.close()

    with pytest.raises(EvidenceStoreError):
        store.reserve("adsblol", clock=at_time(NOON), seconds=180, longest=WEEK)
    with pytest.raises(EvidenceStoreError):
        store.postpone(
            "adsblol", until=NOON + timedelta(seconds=180), clock=at_time(NOON), longest=WEEK
        )
    assert store.state("adsblol") == {}


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
            row("plane-2", kind="aircraft", source="adsblol", occurred_at=minutes(2)),
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
    assert refs(store.search(sources=["adsblol"])) == ["plane-2"]
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


def _while_handling(error: Exception, call: Callable[[], object]) -> None:
    """Make a call the way a caller does who is itself handling an error."""
    try:
        raise error
    except type(error):
        call()


def test_a_refusal_keeps_nothing_of_an_error_the_caller_was_handling(
    store: EvidenceStore,
) -> None:
    store.close()
    for refused_call in (
        lambda: store.add([row("not json", data={"when": NOON})]),
        lambda: store.add([row("invented", origin="mock")]),
        lambda: store.add([row("too late")]),
        lambda: EvidenceStore(store.path, "mock"),
    ):
        with pytest.raises(EvidenceStoreError) as refused:
            _while_handling(ValueError("Jane Doe, 1458 Hayes St"), refused_call)

        assert (refused.value.__cause__, refused.value.__context__) == (None, None)


def test_a_closed_store_takes_no_more_rows_and_still_answers(store: EvidenceStore) -> None:
    store.add([row("before")], state=("nws", {"listed": ["before"]}))
    assert not store.closed

    store.close()

    assert store.closed
    with pytest.raises(EvidenceStoreError, match=r"^The evidence store is closed and takes no"):
        store.add([row("after")], state=("nws", {"listed": ["after"]}))
    assert refs(store.search()) == ["before"]
    assert store.state("nws") == {"listed": ["before"]}
    # Another process's store on the same file is its own, and is not closed by this.
    assert len(EvidenceStore(store.path, "live").add([row("elsewhere")])) == 1


def _write_on_a_thread(store: EvidenceStore, ref: str) -> tuple[threading.Thread, list[object]]:
    outcome: list[object] = []

    def write() -> None:
        try:
            outcome.append(store.add([row(ref)], state=("nws", {"listed": [ref]})))
        except EvidenceStoreError as refused:
            outcome.append(refused)

    writing = threading.Thread(target=write)
    writing.start()
    return writing, outcome


def test_a_write_waits_for_another_writer_to_finish(store: EvidenceStore) -> None:
    other = sqlite3.connect(store.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    writing, outcome = _write_on_a_thread(store, "patient")
    time.sleep(0.3)
    assert writing.is_alive()

    other.execute("COMMIT")
    writing.join(5)

    assert not writing.is_alive()
    assert refs(outcome[0]) == refs(store.search()) == ["patient"]
    other.close()


def test_a_write_that_is_waiting_gives_up_at_once_when_the_store_is_closed(
    store: EvidenceStore,
) -> None:
    other = sqlite3.connect(store.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    writing, outcome = _write_on_a_thread(store, "late")
    time.sleep(0.3)
    assert writing.is_alive()
    began = time.monotonic()

    store.close()
    writing.join(5)

    assert not writing.is_alive()
    assert time.monotonic() - began < 1
    assert [str(refused) for refused in outcome] == [
        "The evidence store is closed and takes no more rows."
    ]
    # The other writer finishes, and the write that gave up does not land behind it.
    other.execute("COMMIT")
    other.close()
    assert store.search() == []
    assert store.state("nws") == {}


def test_a_write_does_not_wait_for_a_reader(store: EvidenceStore) -> None:
    store.add([row("first")])
    reader = sqlite3.connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM evidence").fetchone() == (1,)
    began = time.monotonic()

    store.add([row("second")])

    assert time.monotonic() - began < 1
    assert refs(store.search()) == ["first", "second"]
    # The reader still sees the store as it was when it began to read.
    assert reader.execute("SELECT COUNT(*) FROM evidence").fetchone() == (1,)
    reader.close()


def test_two_openings_of_a_new_file_at_once_begin_one_store(tmp_path: Path) -> None:
    for trial in range(30):
        path = tmp_path / f"evidence-{trial}.sqlite3"
        outcomes: list[object] = []
        together = threading.Barrier(4)

        def open_and_write(
            ref: str,
            path: Path = path,
            together: threading.Barrier = together,
            outcomes: list[object] = outcomes,
        ) -> None:
            together.wait()
            try:
                outcomes.append(EvidenceStore(path, "live").add([row(ref)]))
            except Exception as failure:
                outcomes.append(failure)

        openers = [threading.Thread(target=open_and_write, args=(f"row-{n}",)) for n in range(4)]
        for opener in openers:
            opener.start()
        for opener in openers:
            opener.join()

        assert [type(outcome) for outcome in outcomes] == [list] * 4
        assert len(EvidenceStore(path, "live").search()) == 4


def _old_store(path: Path) -> None:
    """A store file as it was before the store used write-ahead mode."""
    EvidenceStore(path, "live").add([row("old")])
    db = sqlite3.connect(path, isolation_level=None)
    assert db.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
    db.close()


def test_a_file_from_before_write_ahead_mode_is_put_in_it_when_it_is_opened(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.sqlite3"
    _old_store(path)

    store = EvidenceStore(path, "live")

    reader = sqlite3.connect(path, isolation_level=None)
    assert reader.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    # So a write to it does not wait for a reader, or fail on one, either.
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM evidence").fetchone() == (1,)
    store.add([row("new")])
    assert refs(store.search()) == ["old", "new"]
    reader.close()


def test_a_file_that_cannot_be_put_in_write_ahead_mode_is_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.sqlite3"
    _old_store(path)
    # Something else is reading the file, and goes on reading it.
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM evidence").fetchone()
    monkeypatch.setattr("beacon.evidence.store._WRITE_WAIT_SECONDS", 0.3)

    with pytest.raises(EvidenceStoreError, match="could not be put in write-ahead mode"):
        EvidenceStore(path, "live")

    reader.close()
    assert refs(EvidenceStore(path, "live").search()) == ["old"]


def test_a_file_that_is_not_a_store_is_an_error_and_is_left_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    path.write_bytes(b"This is somebody's notes, not a database. " * 100)

    # Not being able to read the file is not the same as there being no store in it.
    with pytest.raises(sqlite3.DatabaseError):
        EvidenceStore(path, "live")

    assert path.read_bytes().startswith(b"This is somebody's notes")


def test_an_item_is_claimed_by_one_caller_only_and_for_good(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(path, "mock")
    # Another process with the same file.
    other = EvidenceStore(path, "mock")

    assert store.claim("openmhz", "call-1") is True
    assert other.claim("openmhz", "call-1") is False
    assert store.claim("openmhz", "call-1") is False
    # A claim is on one source's item: the same reference of another source is free.
    assert other.claim("another", "call-1") is True
    assert other.claim("openmhz", "call-2") is True

    # It outlasts the process that made it.
    assert EvidenceStore(path, "mock").claim("openmhz", "call-1") is False


def test_of_many_callers_claiming_at_once_exactly_one_gets_the_item(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    EvidenceStore(path, "mock")
    start = threading.Barrier(8)

    def claim(_caller: int) -> bool:
        store = EvidenceStore(path, "mock")
        start.wait(10)
        return store.claim("openmhz", "call-1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))

    assert sorted(claims) == [False] * 7 + [True]


def test_a_claim_that_is_given_up_can_be_made_again(store: EvidenceStore) -> None:
    assert store.claim("openmhz", "call-1") is True

    store.release("openmhz", "call-1")

    assert store.taken("openmhz", ["call-1"]) == set()
    assert store.claim("openmhz", "call-1") is True


def test_what_is_taken_is_what_is_claimed_or_stored(store: EvidenceStore) -> None:
    store.claim("openmhz", "claimed")
    store.add([row("stored", source="openmhz")])
    store.add([row("of-another-source")])
    refs = ["claimed", "stored", "of-another-source", "new", *(f"call-{n}" for n in range(1200))]

    assert store.taken("openmhz", refs) == {"claimed", "stored"}
    assert store.taken("openmhz", []) == set()


def test_a_closed_store_takes_no_claim_and_gives_none_up(store: EvidenceStore) -> None:
    store.claim("openmhz", "call-1")
    store.close()

    with pytest.raises(EvidenceStoreError, match="is closed and takes no more rows"):
        store.claim("openmhz", "call-2")
    with pytest.raises(EvidenceStoreError, match="is closed and takes no more rows"):
        store.release("openmhz", "call-1")

    # Reading goes on: the claim that was made is there, and the other is not.
    assert store.taken("openmhz", ["call-1", "call-2"]) == {"call-1"}


def test_a_store_file_from_before_claims_is_given_the_table(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    EvidenceStore(path, "live").add([row("kept")])
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE claims")

    store = EvidenceStore(path, "live")

    assert store.claim("openmhz", "call-1") is True
    assert refs(store.search()) == ["kept"]


def test_a_reader_that_notes_the_last_id_sees_the_same_rows_whatever_is_stored_after(
    store: EvidenceStore,
) -> None:
    assert store.last_id() == 0
    store.add([row("first"), row("second", occurred_at=minutes(5))])
    noted = store.last_id()
    assert noted == 2

    # A row that happened earlier and is stored later is still a later row.
    store.add([row("late", occurred_at=minutes(-5))])

    assert refs(store.search(up_to_id=noted)) == ["first", "second"]
    assert refs(store.search()) == ["late", "first", "second"]
    assert store.last_id() == 3
