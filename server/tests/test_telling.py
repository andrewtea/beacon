"""The invented story with Muse Ember doing the model's work on it (`beacon.telling`).

No test asks Meta: the paying clients are live ones over `MetaApi`, which answers as
the stand-ins do, so what is tested is who is asked, with what, under which scope,
and what the rows and the answers then say of themselves.
"""

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from beacon import serve, telling
from beacon.ember import EmberClient, EmberConfigError, SpendScope, Storage
from beacon.ember.ledger import FILE_NAME, kept_file
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.incident.build import label_of
from beacon.ingest import Clock, IngestConfigError, build
from beacon.ingest import setup as ingest_setup
from beacon.ingest.http import SourceHttp
from beacon.ingest.notices import NoticeFeed
from beacon.ingest.recordings import RecordedRadio
from beacon.investigate.stand_in import stand_in_investigator
from beacon.main import ServerSetupError, create_app
from beacon.push import notice_of
from beacon.radio.audio import pcm_to_wav, plain_wav
from beacon.scenario import Scenario, scenario_transport
from beacon.scenario import notices as scenario_notices
from beacon.scenario import radio as scenario_radio
from beacon.scenario.radio import CALLS, KNOWN, TALKGROUPS, SpokenShelf, StoryShelf, stand_in_ember
from beacon.story.stand_in import stand_in_writer
from tests.test_ember_client import MetaApi

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
# A mock store begins its story twenty minutes before it is first opened.
START = NOON - timedelta(minutes=20)
# The calls in which something is said, and the wind, which is long enough to be
# heard: every call but the click of a microphone.
HEARD = sum(call.seconds >= 1 for call in CALLS)


def _clip(words: str) -> bytes:
    """Stands in for synthesized speech: a clip of its own for each call's words,
    two seconds long."""
    seed = hashlib.sha256(words.encode()).digest()
    return pcm_to_wav((seed * 2000)[:64_000])


class Meta:
    """Answers as Meta's API would, by rule: a call of the story with the form the
    story has for its words, a notice as the stand-in reads it, and a story and a
    tap as their stand-ins do. It keeps the audio it was sent."""

    def __init__(self, data: Path, start: datetime = START) -> None:
        self.storage = Storage.under(data)
        self.start = start
        self.audio: list[bytes] = []
        self.forms: list[str] = []
        self.scopes: list[str] = []
        self.api = MetaApi(input_tokens=1_000, create=self.create)
        self._heard = {
            hashlib.sha256(plain_wav(_clip(call.said))).hexdigest(): call.heard
            for call in CALLS
            if call.said is not None
        }

    def create(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        form = body.get("text", {}).get("format", {}).get("name")
        self.forms.append(form)
        if form == "transmission":
            (message,) = body["input"]
            audio = base64.b64decode(message["content"][1]["input_audio"]["data"])
            self.audio.append(audio)
            heard = self._heard.get(hashlib.sha256(audio).hexdigest())
            # What is not the story's words is noise to it, as a tone would be.
            answer = dict(stand_in_ember({}) if heard is None else _answer(heard))
        elif form == "notice":
            answer = _answer(scenario_notices.stand_in_form(body, self.start))
        elif form == "incident_story":
            answer = dict(stand_in_writer(body))
        else:
            answer = dict(stand_in_investigator(body))
        # A thousand tokens in and a hundred out: $0.00015 a request.
        answer["usage"] = {
            "input_tokens": 1_000,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 1_100,
        }
        return httpx2.Response(200, json=answer)

    def client(self, scope: SpendScope) -> EmberClient:
        self.scopes.append(scope.label)
        return self.api.client(self.storage, scope=scope)

    def spend(self, label: str) -> tuple[int, int]:
        spent = EmberClient.replay_only(self.storage).scope_spend(label)
        return spent.calls, spent.spent_nanousd


def _answer(form: Any) -> dict[str, Any]:
    from beacon.ember import fake_response

    return dict(fake_response(text=json.dumps(form)))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    # No ffmpeg is run: the story's words become clips made here.
    monkeypatch.setattr(ingest_setup, "can_speak", lambda: True)
    monkeypatch.setattr(scenario_radio, "synthesize", _clip)
    return {
        "BEACON_DATA_DIR": str(tmp_path),
        telling.MOCK_EMBER_VARIABLE: "live",
        telling.BUDGET_VARIABLE: "0.10",
    }


def told(env: dict[str, str], minute: float, meta: Meta) -> EvidenceStore:
    """The story played to `minute` by Muse Ember, as `meta` answers for it."""
    moment = [NOON]
    ingest = build(env, Clock(lambda: moment[0]), paid=meta.client)
    moment[0] = START + timedelta(minutes=minute)
    assert ingest.catch_up()
    ingest.close()
    return EvidenceStore(Path(env["BEACON_DATA_DIR"]) / "evidence" / "mock.sqlite3", "mock")


# Who tells the story.


def test_a_stand_in_tells_the_invented_story_unless_the_setting_says_live() -> None:
    assert telling.teller_of({}) == "stand_in"
    assert telling.teller_of({telling.MOCK_EMBER_VARIABLE: "stand-in"}) == "stand_in"
    assert telling.teller_of({telling.MOCK_EMBER_VARIABLE: "live"}) == "ember"
    for wrong in ("", "1", "ember", "LIVE"):
        with pytest.raises(telling.TellingConfigError, match="stand-in, which is the default"):
            telling.teller_of({telling.MOCK_EMBER_VARIABLE: wrong})


def test_ember_hears_the_storys_calls_as_speech_and_reads_its_notices_and_the_rows_stay_invented(
    env: dict[str, str], tmp_path: Path
) -> None:
    meta = Meta(tmp_path)

    store = told(env, 106, meta)

    radio = store.search(kinds=["radio"], include_unlocated=True)
    notices = store.search(kinds=["notice"], include_unlocated=True)
    # Every call a model heard and every notice it read says a model did, and is a
    # row of the invented scenario all the same: `mock`, and so a stand-in's.
    assert len(radio) == HEARD == 19
    assert len(notices) == 3
    assert {(row.origin, row.model, row.stand_in) for row in [*radio, *notices]} == {
        ("mock", "live", True)
    }
    # What was kept of the first call is what the story has it say.
    first = min(radio, key=lambda row: row.occurred_at)
    assert (first.occurred_at, first.place) == (START, "400 block of Hayes St")
    assert first.data["units"] == ["E36", "E21", "T6", "B2"]
    # Each was paid for once, under the telling's own scopes and no real source's.
    assert meta.spend(telling.RADIO) == (HEARD, HEARD * 150_000)
    assert meta.spend(telling.NOTICES) == (3, 3 * 150_000)
    assert meta.spend("ingest:recordings") == meta.spend("ingest:notices") == (0, 0)
    # No tone was sent: what Ember was sent is the speech, and the wind.
    tones = {plain_wav(_tone) for _tone in _story_tones()}
    assert len(meta.audio) == HEARD
    assert not tones & set(meta.audio)
    said = {plain_wav(_clip(call.said)) for call in CALLS if call.said is not None}
    assert said <= set(meta.audio)
    assert len(set(meta.audio) - said) == 1


def _story_tones() -> list[bytes]:
    from beacon.scenario.radio import _tone

    return [_tone(index) for index in range(len(CALLS))]


def test_the_live_store_refuses_what_ember_made_of_the_invented_story(
    env: dict[str, str], tmp_path: Path
) -> None:
    store = told(env, 7, Meta(tmp_path))
    (row, *_) = store.search(kinds=["radio"], include_unlocated=True)
    live = EvidenceStore(tmp_path / "evidence" / "live.sqlite3", "live")

    with pytest.raises(EvidenceStoreError):
        live.refuse_stand_in(row.origin, row.model)


def test_a_row_ember_made_of_the_invented_story_is_labeled_as_generated_and_as_embers() -> None:
    # A model heard it, and it was no radio; a model read it, and it was no
    # authority's notice.
    heard = label_of("recordings", "live", invented=True)
    read = label_of("notices", "live", invented=True)
    assert "synthesized speech and no radio" in heard
    assert "as Ember heard" in heard
    assert read == "Invented notice, as Ember read it"
    # A stand-in's row is still never said to be Ember's, and a real one is the feed's.
    assert "Ember" not in label_of("recordings", "fake", invented=True)
    assert "Ember" not in label_of("notices", "fake", invented=True)
    assert label_of("recordings", "live") == "Fire and EMS dispatch radio, as Ember heard it"
    assert label_of("notices", "live") == "Official notice, as Ember read it"


def test_a_telling_is_paid_for_once_however_often_the_server_is_started(
    env: dict[str, str], tmp_path: Path
) -> None:
    meta = Meta(tmp_path)
    told(env, 7, meta)
    heard = len(meta.audio)
    assert heard == 5

    # The server is started again later in the same story.
    moment = [START + timedelta(minutes=13)]
    ingest = build(env, Clock(lambda: moment[0]), paid=meta.client)
    assert ingest.catch_up()
    ingest.close()

    # Only the calls made since were heard: none a second time.
    assert len(meta.audio) == len(set(meta.audio)) == heard + 2
    assert meta.spend(telling.RADIO)[0] == heard + 2


def test_a_story_is_told_by_one_or_the_other_from_its_beginning(
    env: dict[str, str], tmp_path: Path
) -> None:
    stand_in = {"BEACON_DATA_DIR": env["BEACON_DATA_DIR"]}
    meta = Meta(tmp_path)
    told(env, 7, meta)

    # A stand-in does not go on with a story Ember began.
    with pytest.raises(IngestConfigError, match="is one Muse Ember worked on"):
        build(stand_in, Clock(lambda: NOON))

    # Nor Ember with a stand-in's, a store from before that was written down included.
    other = tmp_path / "other"
    began = build({"BEACON_DATA_DIR": str(other)}, Clock(lambda: NOON))
    state = dict(began.store.state("scenario"))
    assert state.pop("teller") == "stand_in"
    began.store.add([], state=("scenario", state), now=NOON)
    began.close()
    with pytest.raises(IngestConfigError, match="is one a stand-in for Muse Ember worked on"):
        build(env | {"BEACON_DATA_DIR": str(other)}, Clock(lambda: NOON), paid=Meta(other).client)
    # Asked nothing, and paid nothing.
    assert Meta(other).spend(telling.RADIO) == (0, 0)


def test_ember_is_not_set_to_work_on_the_story_without_a_budget_a_key_or_a_voice(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(lambda: NOON)
    no_budget = {key: value for key, value in env.items() if key != telling.BUDGET_VARIABLE}
    with pytest.raises(telling.TellingConfigError, match="It has no default"):
        build(no_budget, clock, paid=Meta(tmp_path).client)
    for wrong in ("", "ten cents", "-1"):
        with pytest.raises(telling.TellingConfigError, match=telling.BUDGET_VARIABLE):
            build(env | {telling.BUDGET_VARIABLE: wrong}, clock, paid=Meta(tmp_path).client)
    # With no key the client only replays, and a sensitive call cannot be replayed.
    with pytest.raises(telling.TellingConfigError, match="needs a live Ember"):
        build(env, clock)
    # A real source is not the invented story.
    with pytest.raises(IngestConfigError, match="is for the invented story"):
        build(env | {"BEACON_LIVE_SOURCES": "nws"}, clock, paid=Meta(tmp_path).client)
    # A tone says nothing: without a voice Ember is sent no call at all.
    monkeypatch.setattr(ingest_setup, "can_speak", lambda: False)
    with pytest.raises(IngestConfigError, match="flite"):
        build(env, clock, paid=Meta(tmp_path).client)
    # None of these left anything behind: no store, and no ledger either.
    assert list(tmp_path.iterdir()) == []


def _ledger(data: Path) -> str:
    """Begin a ledger under `data`, as a client that came before did, and answer
    the name of its file."""
    return EmberClient.replay_only(Storage.under(data)).ledger_file()


NO_FILE = "0" * 32 + ":1:1"


def test_a_telling_that_names_its_ledger_is_paid_from_that_ledger_or_not_at_all(
    env: dict[str, str], tmp_path: Path
) -> None:
    clock = Clock(lambda: NOON)
    ledger = tmp_path / "ember" / "ledger.sqlite3"
    named = env | {telling.LEDGER_VARIABLE: NO_FILE}

    # No ledger: none is begun, which would count nothing of what was spent.
    with pytest.raises(telling.TellingConfigError, match="no ledger was begun"):
        build(named, clock, paid=Meta(tmp_path).client)
    assert list(tmp_path.iterdir()) == []
    # An empty file where the ledger was is no ledger, and stays empty.
    ledger.parent.mkdir()
    ledger.write_bytes(b"")
    with pytest.raises(telling.TellingConfigError, match="is not that file"):
        build(named, clock, paid=Meta(tmp_path).client)
    assert ledger.read_bytes() == b""
    assert [path.name for path in tmp_path.iterdir()] == ["ember"]
    ledger.unlink()

    # Another ledger than the one named.
    file = _ledger(tmp_path)
    assert file != NO_FILE
    with pytest.raises(telling.TellingConfigError, match="is not that file"):
        build(named, clock, paid=Meta(tmp_path).client)
    assert not (tmp_path / "evidence").exists()
    # A setting that is no ledger file's name is refused as that: a generation
    # alone names a ledger and every copy of it.
    for wrong in ("the one", file.split(":")[0], file + ":1", file + " "):
        with pytest.raises(telling.TellingConfigError, match="is the name of the ledger file"):
            build(env | {telling.LEDGER_VARIABLE: wrong}, clock, paid=Meta(tmp_path).client)

    # The one named: the story is told, and paid for in it.
    meta = Meta(tmp_path)
    told(env | {telling.LEDGER_VARIABLE: file}, 7, meta).close()
    assert meta.spend(telling.RADIO)[0] == 5
    assert _ledger(tmp_path) == file


def test_a_copy_of_the_ledger_put_in_its_place_is_not_the_ledger_that_was_named(
    env: dict[str, str], tmp_path: Path
) -> None:
    # An older copy has the generation and can lack what was paid for since.
    ledger = tmp_path / "ember" / "ledger.sqlite3"
    file = _ledger(tmp_path)
    copy = ledger.with_name("copy")
    copy.write_bytes(ledger.read_bytes())
    for beside in ledger.parent.glob("ledger.sqlite3*"):
        beside.unlink()
    copy.rename(ledger)
    assert _ledger(tmp_path).split(":")[0] == file.split(":")[0]

    with pytest.raises(telling.TellingConfigError, match="a copy that was put in its place"):
        build(
            env | {telling.LEDGER_VARIABLE: file}, Clock(lambda: NOON), paid=Meta(tmp_path).client
        )
    with pytest.raises(telling.TellingConfigError, match="is not that file"):
        telling.preflight(
            env | {"REPLAY": "0", "EMBER_SPENT_BEFORE_USD": "0", telling.LEDGER_VARIABLE: file}
        )
    assert not (tmp_path / "evidence").exists()


def test_a_client_that_is_on_another_ledger_than_the_one_named_pays_nothing(
    env: dict[str, str], tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # The look before a client is made and the client's own file are two looks:
    # the second is of the file the client pays in.
    file = _ledger(tmp_path)
    elsewhere = Meta(tmp_path_factory.mktemp("elsewhere"))

    with pytest.raises(telling.TellingConfigError, match="is not that file"):
        telling.paying(env | {telling.LEDGER_VARIABLE: file}, telling.TAPS, elsewhere.client)

    assert elsewhere.api.requests == []


@pytest.mark.parametrize("then", ["gone", "empty", "another"])
def test_a_ledger_that_goes_between_the_look_and_the_client_is_not_begun_anew(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    # The look before a client is made (`check`) and the making of the client are
    # two moments. A client begins a ledger where it finds none, and a new, empty
    # ledger left at the path would be taken for the ledger by the next look that
    # names none.
    ledger = tmp_path / "ember" / "ledger.sqlite3"
    file = _ledger(tmp_path)
    live = env | {"REPLAY": "0", "MODEL_API_KEY": "test-key", telling.LEDGER_VARIABLE: file}
    look = telling.check
    put: list[bytes] = []

    def looked_and_then(env: dict[str, str], make: object = None) -> None:
        look(env, None)
        for beside in ledger.parent.glob("ledger.sqlite3*"):
            beside.rename(tmp_path / f"moved-{beside.name}")
        if then == "empty":
            ledger.write_bytes(b"")
        if then == "another":
            _ledger(tmp_path)
        if then != "gone":
            put.append(ledger.read_bytes())

    monkeypatch.setattr(telling, "check", looked_and_then)

    with pytest.raises(telling.TellingConfigError, match="no ledger was begun") as refused:
        telling.paying(live, telling.TAPS)

    assert refused.value.__context__ is None
    # What the sentence says is so: nothing was begun where the ledger was, and
    # what was put there was not written to.
    if then == "gone":
        assert not ledger.exists()
        assert [path.name for path in ledger.parent.iterdir()] == []
    else:
        assert ledger.read_bytes() == put[0]
    # So a look that names no ledger finds none that a refused client made.
    if then != "another":
        assert kept_file(ledger) is None
        with pytest.raises(telling.TellingConfigError, match="no ledger that was begun"):
            telling.preflight(env | {"REPLAY": "0", "EMBER_SPENT_BEFORE_USD": "0"})


def test_what_can_be_seen_before_a_server_is_started_is_looked_at_and_nothing_is_made(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = env | {"REPLAY": "0", "EMBER_SPENT_BEFORE_USD": "0"}
    ledger = tmp_path / "ember" / "ledger.sqlite3"
    # A stand-in's telling needs nothing, and has no ledger to name.
    assert telling.preflight({"BEACON_DATA_DIR": str(tmp_path)}) is None
    assert telling.main({"BEACON_DATA_DIR": str(tmp_path)}) == 0
    assert capsys.readouterr().out == ""

    with pytest.raises(telling.TellingConfigError, match="stand-in, which is the default, or live"):
        telling.preflight(live | {telling.MOCK_EMBER_VARIABLE: "yes"})
    # Paid requests are asked for by name.
    for replay in ({}, {"REPLAY": "1"}, {"REPLAY": ""}):
        with pytest.raises(telling.TellingConfigError, match="Set REPLAY=0 beside it"):
            telling.preflight(env | {"EMBER_SPENT_BEFORE_USD": "0"} | replay)
    # A budget the server would refuse is refused here, before anything is moved.
    for wrong in ("", "ten cents", "-1", "0.10 dollars"):
        with pytest.raises(telling.TellingConfigError, match=telling.BUDGET_VARIABLE):
            telling.preflight(live | {telling.BUDGET_VARIABLE: wrong})
    with pytest.raises(EmberConfigError, match="absolute path"):
        telling.preflight(live | {"BEACON_DATA_DIR": "data"})
    # So are the spending settings it is handed, as a client would read them.
    unsaid = {name: value for name, value in live.items() if name != "EMBER_SPENT_BEFORE_USD"}
    with pytest.raises(telling.TellingConfigError, match="needs EMBER_SPENT_BEFORE_USD"):
        telling.preflight(unsaid)
    with pytest.raises(EmberConfigError, match="EMBER_SPENT_BEFORE_USD"):
        telling.preflight(live | {"EMBER_SPENT_BEFORE_USD": "none"})
    with pytest.raises(EmberConfigError, match="EMBER_STOP_USD"):
        telling.preflight(live | {"EMBER_STOP_USD": "three"})
    with pytest.raises(EmberConfigError, match="Lower EMBER_STOP_USD"):
        telling.preflight(live | {"EMBER_SPENT_BEFORE_USD": "9", "EMBER_STOP_USD": "3"})

    # No ledger that was begun: nothing there, an empty file, a link, another file.
    with pytest.raises(telling.TellingConfigError, match="no ledger that was begun"):
        telling.preflight(live)
    assert list(tmp_path.iterdir()) == []
    ledger.parent.mkdir()
    ledger.write_bytes(b"")
    assert telling.main(live) == 2
    assert "A ledger begun anew would count nothing" in capsys.readouterr().err
    assert ledger.read_bytes() == b""
    ledger.unlink()
    EvidenceStore(ledger, "mock").close()
    with pytest.raises(telling.TellingConfigError, match="no ledger that was begun"):
        telling.preflight(live)
    for beside in ledger.parent.iterdir():
        beside.unlink()
    _ledger(tmp_path / "kept")
    ledger.symlink_to(tmp_path / "kept" / "ember" / "ledger.sqlite3")
    with pytest.raises(telling.TellingConfigError, match="no ledger that was begun"):
        telling.preflight(live)
    ledger.unlink()

    # The ledger file that is there is named, to be handed to the server.
    file = _ledger(tmp_path)
    before = ledger.read_bytes()
    assert telling.preflight(live) == file
    assert telling.main(live) == 0
    assert capsys.readouterr().out == file + "\n"
    assert ledger.read_bytes() == before
    # One that is named already has to be that one: it is not replaced by the one
    # that happens to be there.
    assert telling.preflight(live | {telling.LEDGER_VARIABLE: file}) == file
    with pytest.raises(telling.TellingConfigError, match="is not that file"):
        telling.preflight(live | {telling.LEDGER_VARIABLE: NO_FILE})


@pytest.mark.parametrize(
    "generation", ["", "x", "0" * 31, "0" * 33, "A" * 32, "$(touch here)", "0" * 32 + "\n", "a b"]
)
def test_what_is_printed_of_a_ledger_is_a_ledger_files_name_whatever_the_file_holds(
    env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str], generation: str
) -> None:
    # It is put on a command line by whoever asked.
    _ledger(tmp_path)
    ledger = tmp_path / "ember" / "ledger.sqlite3"
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("UPDATE ledger_meta SET value = ? WHERE key = 'generation'", (generation,))
    live = env | {"REPLAY": "0", "EMBER_SPENT_BEFORE_USD": "0"}

    assert kept_file(ledger) is None
    assert telling.main(live) == 2
    said = capsys.readouterr()
    assert said.out == ""
    assert "no ledger that was begun" in said.err


def test_a_ledger_files_name_is_the_one_a_client_gives_the_file_it_is_open_on(
    tmp_path: Path,
) -> None:
    file = _ledger(tmp_path)
    assert kept_file(tmp_path / "ember" / "ledger.sqlite3") == file
    assert FILE_NAME.fullmatch(file)


def test_the_budget_is_split_among_the_four_scopes_and_is_the_most_they_hold_together() -> None:
    env = {telling.BUDGET_VARIABLE: "0.10"}
    budgets = {label: telling.scope_of(env, label).budget_nanousd for label in telling.SCOPES}

    assert sum(telling.SCOPES.values()) == 100
    assert sum(budgets.values()) == 100_000_000
    assert budgets[telling.RADIO] == 15_000_000
    # A share is rounded down, so the four never come to more than the budget.
    odd = {telling.BUDGET_VARIABLE: "0.000000033"}
    assert sum(telling.scope_of(odd, label).budget_nanousd for label in telling.SCOPES) <= 33


def test_hearing_stops_at_its_share_of_the_budget_and_the_rest_of_the_story_goes_on(
    env: dict[str, str], tmp_path: Path
) -> None:
    meta = Meta(tmp_path)
    # Fifteen hundredths of $0.01 is $0.0015. A call is reserved at the most it
    # could cost, $0.000612, before it is sent, and comes to $0.00015: so six are
    # heard, and the seventh, which could take the scope past its share, is not sent.
    env[telling.BUDGET_VARIABLE] = "0.01"

    moment = [NOON]
    ingest = build(env, Clock(lambda: moment[0]), paid=meta.client)
    moment[0] = START + timedelta(minutes=30)
    ingest.catch_up()
    store = ingest.store
    rows = store.search(kinds=["radio"], include_unlocated=True)
    ingest.close()

    assert len(rows) == len(meta.audio) == 6
    assert meta.spend(telling.RADIO) == (6, 900_000)
    # The alert, the closure, and the aircraft are no model's, and were stored.
    assert store.search(kinds=["alert"])
    assert store.search(kinds=["traffic"])


# The calls, as speech.


def shelf(now: datetime, voice: Callable[[str], bytes] = _clip) -> SpokenShelf:
    return SpokenShelf(START, lambda: now, voice)


def test_the_storys_calls_are_speech_each_when_it_is_made_and_nothing_is_written(
    env: dict[str, str], tmp_path: Path
) -> None:
    spoken = shelf(START + timedelta(minutes=3))
    everything = (0, ""), int((START + timedelta(hours=3)).timestamp())

    calls, more = spoken.listed(*everything, TALKGROUPS, 100)

    # The calls made by minute 3, as the shelf of tones lists them.
    tones = StoryShelf(START, lambda: START + timedelta(minutes=3))
    assert (calls, more) == tones.listed(*everything, TALKGROUPS, 100)
    assert len(calls) == 3
    # What is said in each is the story's words, and its JSON says how long that
    # plays: two seconds, where the tone is as long as the story has the call.
    first = calls[0]
    assert spoken.audio(first) == _clip(CALLS[0].said or "")
    assert json.loads(spoken.details(first) or b"")["call_length_ms"] == 2000
    assert spoken.audio(first) != tones.audio(first)
    # A whole telling leaves nothing under the data directory but the evidence
    # store and Ember's own files: no call is a file anywhere.
    told(env, 30, Meta(tmp_path))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["ember", "evidence"]


def test_speech_may_be_heard_by_a_paid_ember_and_a_tone_may_not_and_both_are_mock_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    meta = Meta(tmp_path)

    def radio(calls: StoryShelf) -> RecordedRadio:
        live = meta.client(SpendScope(telling.RADIO, 1_000_000))
        return RecordedRadio(calls, live, talkgroups=TALKGROUPS, known=KNOWN, scope=telling.RADIO)

    # A tone says nothing, and a paid Ember is never handed one.
    with pytest.raises(ValueError, match="a stand-in's calls by a fake one"):
        radio(StoryShelf(START, lambda: NOON))
    # Speech it may hear. Its rows are the invented story's all the same: the
    # shelf is no directory of recordings, and nothing it says of itself makes it one.
    heard = radio(shelf(NOON))
    assert heard.origin == "mock"

    class Claims(SpokenShelf):
        recorded = True
        generated = False

    assert radio(Claims(START, lambda: NOON, _clip)).origin == "mock"


def test_a_call_that_could_not_be_synthesized_is_not_claimed_and_is_heard_later(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = Meta(tmp_path)
    failing = [True]

    def voice(words: str) -> bytes:
        if failing[0]:
            raise OSError("ffmpeg could not synthesize a call")
        return _clip(words)

    monkeypatch.setattr(scenario_radio, "synthesize", voice)
    moment = [NOON]
    ingest = build(env, Clock(lambda: moment[0]), paid=meta.client)
    moment[0] = START + timedelta(minutes=3)
    ingest.catch_up()

    # Nothing was sent, nothing paid, and no call was claimed for nothing.
    assert meta.audio == []
    assert ingest.store.claimed_without_row("recordings") == 0
    assert ingest.store.search(kinds=["radio"], include_unlocated=True) == []

    failing[0] = False
    moment[0] = START + timedelta(minutes=4)
    assert ingest.catch_up()
    assert len(ingest.store.search(kinds=["radio"], include_unlocated=True)) == 3
    assert len(meta.audio) == 3
    ingest.close()


def test_a_call_in_which_nothing_is_said_is_noise_and_is_the_same_every_time() -> None:
    wind = [index for index, call in enumerate(CALLS) if call.said is None and call.seconds >= 1]
    (index,) = wind
    after = START + timedelta(minutes=CALLS[index].minute)

    def noise() -> bytes | None:
        spoken = shelf(after)
        (call,) = (
            filed
            for filed in spoken.listed((0, ""), int(after.timestamp()), TALKGROUPS, 100)[0]
            if filed.stem.endswith(f"call_{index}")
        )
        return spoken.audio(call)

    one, two = noise(), noise()
    assert one is not None
    assert one == two
    assert len(one) > 90_000
    # No voice was asked for it: it is made here.
    assert one != _clip("")


def test_speech_is_synthesized_with_no_file_written_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beacon.radio import speech

    asked: list[tuple[list[str], dict[str, Any]]] = []
    samples = b"\x01\x02" * 8_000

    def ffmpeg(command: list[str], **options: Any) -> Any:
        # Stands where ffmpeg is: the words are in its arguments, and the speech
        # is what it writes to its output.
        asked.append((command, options))

        class Done:
            returncode = 0
            stdout = samples

        return Done()

    monkeypatch.setattr(speech.subprocess, "run", ffmpeg)
    monkeypatch.chdir(tmp_path)

    assert speech.synthesize("Engine 36 is on scene.") == pcm_to_wav(samples)

    ((command, options),) = asked
    # No file is named for the words or for the speech, and ffmpeg is started in
    # no directory of its own to leave one in.
    assert "flite=text=Engine 36 is on scene.:voice=slt" in command
    assert command[-3:] == ["-f", "s16le", "pipe:1"]
    assert "-y" not in command
    assert "textfile" not in " ".join(command)
    assert "cwd" not in options
    assert list(tmp_path.iterdir()) == []
    # Nothing that could not be synthesized is handed back as speech.
    monkeypatch.setattr(
        speech.subprocess, "run", lambda *_, **__: type("D", (), {"returncode": 1, "stdout": b""})()
    )
    with pytest.raises(OSError, match="could not synthesize"):
        speech.synthesize("Engine 36 is on scene.")
    with pytest.raises(ValueError, match="more than one synthesis takes"):
        speech.synthesize("a" * (speech.MAX_WORDS + 1))


def test_words_are_read_by_ffmpeg_as_words_whatever_characters_they_hold() -> None:
    from beacon.radio.speech import _in_a_filtergraph

    # Nothing in the words ends the option, the filter, or the graph: each of the
    # characters ffmpeg reads as its own is escaped, once for each time ffmpeg
    # takes escaping off.
    assert _in_a_filtergraph("Engine 36 is on scene.") == "Engine 36 is on scene."
    assert _in_a_filtergraph("a, b; [c]") == "a\\, b\\; \\[c\\]"
    assert _in_a_filtergraph("one: two") == "one\\\\: two"
    assert _in_a_filtergraph("O'Farrell") == "O\\\\\\'Farrell"
    assert _in_a_filtergraph("a\\b") == "a" + "\\" * 4 + "b"


# A generated notice.


def test_a_live_ember_reads_a_stand_ins_document_only_when_it_was_generated_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    meta = Meta(tmp_path)
    http = SourceHttp(scenario_transport(Scenario(start=START), lambda: NOON))
    live = meta.client(SpendScope(telling.NOTICES, 1_000_000))
    url = scenario_notices.FEED_URL

    with pytest.raises(ValueError, match="a generated one by the Ember it was generated for"):
        NoticeFeed(http, live, feed_url=url, scope=telling.NOTICES)
    # And a stand-in is not what a generated document was generated for.
    fake = EmberClient.fake(meta.storage, scenario_notices.stand_in_reader(START))
    with pytest.raises(ValueError, match="generated"):
        NoticeFeed(http, fake, feed_url=url, generated=True)
    # A real feed's documents were not generated, whatever is said.
    with pytest.raises(ValueError, match="was not generated"):
        NoticeFeed(SourceHttp(), live, feed_url=url, scope=telling.NOTICES, generated=True)

    feed = NoticeFeed(http, live, feed_url=url, scope=telling.NOTICES, generated=True)
    # Its rows are what its client's answers are, which nothing here sets.
    assert feed.origin == "mock"


# The server.


def serving(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, meta: Meta) -> TestClient:
    """A server on `env`, whose paying clients are made over `meta`."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("REPLAY", "0")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    live = EmberClient.live

    def over_meta(storage: Storage, **options: Any) -> EmberClient:
        # As `MetaApi.client` makes one, with whatever `from_env` asked for.
        return live(storage, **options | {"http_client": meta.api.http_client()})

    monkeypatch.setattr(EmberClient, "live", staticmethod(over_meta))
    return TestClient(create_app())


def test_a_server_on_which_ember_works_on_the_story_says_so_and_pays_under_the_tellings_scopes(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = Meta(tmp_path)
    with serving(env, monkeypatch, meta) as client:
        health = client.get("/api/health").json()
        desk = client.app.state.investigations  # type: ignore[attr-defined]
        stories = client.app.state.stories  # type: ignore[attr-defined]
        scopes = (desk._ember.scope.label, stories._ember.scope.label)
        modes = (desk._ember.mode, stories._ember.mode)

    # Still the invented scenario, and it says who works on it.
    assert (health["mode"], health["teller"]) == ("mock", "ember")
    assert health["teller_text"] == telling.TOLD_BY_EMBER
    assert "still made up" in telling.TOLD_BY_EMBER
    assert scopes == (telling.TAPS, telling.STORIES)
    assert modes == ("live", "live")


def test_a_server_on_a_stand_ins_telling_says_that_and_says_nothing_of_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        health = client.get("/api/health").json()
    assert (health["mode"], health["teller"], health["teller_text"]) == ("mock", "stand_in", None)


def test_ember_does_not_work_on_a_story_the_server_does_not_play_or_tells_again_by_itself(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        pytest.raises(ServerSetupError, match="needs the pollers"),
        serving(env | {"BEACON_INGEST": "0"}, monkeypatch, Meta(tmp_path)),
    ):
        pass
    assert not (tmp_path / "evidence").exists()

    # Told again, it would be paid for again with nobody there, and the claims on
    # what was paid for would be deleted between the tellings.
    with pytest.raises(serve.ServeError, match="is not told again by itself"):
        serve.repeats(env | {serve.REPEAT_VARIABLE: "1"})
    assert serve.repeats({serve.REPEAT_VARIABLE: "1"}) is True


def test_the_words_of_a_notification_are_the_same_whoever_tells_the_story(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The words are kept on a device and a push carries nothing, so they do not
    # say who decided: a device that subscribed under one teller shows nothing
    # false when the other decides.
    monkeypatch.setenv("BEACON_VAPID_SUBJECT", "mailto:beacon@example.org")
    with serving(env, monkeypatch, Meta(tmp_path)) as client:
        by_ember = client.get("/api/push").json()["notice"]
    words = notice_of("mock")
    assert by_ember == {"id": words.id, "title": words.title, "text": words.text}
    assert words.text.startswith("Invented scenario, not a real event.")
    assert "which the app names as a model or a stand-in for one" in words.text
    for word in ("stay", "leave", "evacuate", " go "):
        assert word not in words.text.lower()


# One teller for a store, whoever opens it.


def over(env: dict[str, str], tmp_path: Path, meta: "Meta") -> datetime:
    """A story Muse Ember told to its end, and the moment that is over."""
    told(env, 106, meta)
    return START + timedelta(minutes=120)


def files(tmp_path: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        # But the locks, which whoever looks at a directory's stores takes.
        if path.is_file() and not path.name.endswith(".lock")
    }


def test_a_store_ember_was_paid_to_work_on_is_not_deleted_to_tell_the_story_again(
    env: dict[str, str], tmp_path: Path
) -> None:
    from beacon.datadir import DataLock

    meta = Meta(tmp_path)
    now = over(env, tmp_path, meta)
    # The deployment's defaults: a stand-in, and the story told again when it is over.
    stand_in = {"BEACON_DATA_DIR": str(tmp_path), serve.REPEAT_VARIABLE: "1"}
    assert serve.repeats(stand_in) is True
    before = files(tmp_path)
    lock = DataLock(tmp_path, shared=False)
    try:
        with pytest.raises(serve.ServeError, match="one Muse Ember worked on"):
            serve.begin_again(stand_in, now, lock)
    finally:
        lock.close()
    # Nothing was deleted or changed: the claims on what was paid for are there.
    assert files(tmp_path) == before
    store = EvidenceStore(tmp_path / "evidence" / "mock.sqlite3", "mock")
    assert len(store.search(kinds=["radio"], include_unlocated=True)) == HEARD
    store.close()

    # And the server does not run on it: the refusal is no file that is busy.
    made: list[object] = []
    with pytest.raises(serve.ServeError, match="Nothing was deleted"):
        serve.run(stand_in, lambda app: made.append(app), clock=lambda: now)  # type: ignore[arg-type,return-value]
    assert made == []
    assert files(tmp_path) == before


def test_a_server_without_its_pollers_does_not_put_a_stand_ins_work_into_embers_store(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    told(env, 30, Meta(tmp_path))
    before = files(tmp_path)

    # The default teller, and no pollers to refuse it: the store itself does.
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEACON_INGEST", "0")
    with (
        pytest.raises(IngestConfigError, match="is one Muse Ember worked on"),
        TestClient(create_app()),
    ):
        pass

    # No desk was opened on it: no stand-in's story or investigation is beside
    # what Ember made, and nothing of the store changed.
    assert not (tmp_path / "story").exists()
    assert not (tmp_path / "investigate").exists()
    assert files(tmp_path) == before


def test_a_store_a_server_without_pollers_opened_first_is_a_stand_ins_and_ember_is_refused(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BEACON_INGEST", "0")
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["teller"] == "stand_in"

    # No story has begun, and the store is a stand-in's already: its desks ran.
    store = EvidenceStore(tmp_path / "evidence" / "mock.sqlite3", "mock")
    assert store.state("scenario") == {"teller": "stand_in"}
    store.close()
    meta = Meta(tmp_path)
    with pytest.raises(IngestConfigError, match="is one a stand-in for Muse Ember worked on"):
        build(env, Clock(lambda: NOON), paid=meta.client)
    # Refused before a paying client was made for it.
    assert meta.scopes == []

    # The pollers a stand-in runs beside it begin the story in that same store.
    began = build({"BEACON_DATA_DIR": str(tmp_path)}, Clock(lambda: NOON))
    assert began.store.state("scenario") == {"teller": "stand_in", "start": START.isoformat()}
    began.close()


def test_a_store_from_before_tellers_were_kept_is_a_stand_ins_once_anything_was_put_into_it(
    env: dict[str, str], tmp_path: Path
) -> None:
    from beacon.evidence import NewEvidence, Point

    # As a server of an earlier Beacon without its pollers left one: a tap, which
    # its stand-in investigated, and no story begun, so nothing under `scenario`.
    path = tmp_path / "evidence" / "mock.sqlite3"
    store = EvidenceStore(path, "mock")
    assert ingest_setup.whose(store) is None
    tap = NewEvidence(
        kind="tap",
        source="app",
        ref="tap-1",
        occurred_at=NOON,
        confidence="low",
        origin="live",
        point=Point(37.776, -122.424),
    )
    store.add([tap])
    assert store.state("scenario") == {}
    assert ingest_setup.whose(store) == "stand_in"
    store.close()

    meta = Meta(tmp_path)
    with pytest.raises(IngestConfigError, match="is one a stand-in for Muse Ember worked on"):
        build(env, Clock(lambda: NOON), paid=meta.client)
    assert meta.scopes == []
    # A claim alone is enough, too: something was paid for, or was to be.
    other = EvidenceStore(tmp_path / "other.sqlite3", "mock")
    other.claim("recordings", "a-call", now=NOON)
    assert ingest_setup.whose(other) == "stand_in"
    other.close()


def test_a_store_ember_works_on_says_so_in_the_word_an_earlier_beacon_reads_and_refuses(
    env: dict[str, str], tmp_path: Path
) -> None:
    from beacon import files
    from beacon.evidence.store import PAID_MOCK

    path = tmp_path / "evidence" / "mock.sqlite3"
    meta = Meta(tmp_path)
    moment = [NOON]
    ingest = build(env, Clock(lambda: moment[0]), paid=meta.client)
    # Marked before the story is played, and so before anything is paid for.
    assert meta.audio == []
    assert files.look(path, meta="store_meta").mode == PAID_MOCK == "mock-paid"
    ingest.close()

    # A Beacon from before this took a file for the mock store only when its mode
    # said `mock`: its `EvidenceStore` refused any other word as "the other store",
    # and its `begin_again` refused to delete a file that did not say `mock`. So
    # the word is neither `mock` nor `live`, and this Beacon reads it as the mock
    # store, Muse Ember's, whatever its state says.
    store = EvidenceStore(path, "mock")
    assert (store.mode, store.paid) == ("mock", True)
    state = dict(store.state("scenario"))
    del state["teller"]
    store.add([], state=("scenario", state), now=NOON)
    assert ingest_setup.whose(store) == "ember"
    with pytest.raises(IngestConfigError, match="is one Muse Ember worked on"):
        ingest_setup.keep_teller(store, "stand_in", NOON)
    store.close()
    with pytest.raises(EvidenceStoreError, match="one or the other for life"):
        EvidenceStore(path, "live").state("scenario")

    # A stand-in's store says `mock`, as it always did.
    theirs = tmp_path / "theirs"
    began = build({"BEACON_DATA_DIR": str(theirs)}, Clock(lambda: NOON))
    began.close()
    assert files.look(theirs / "evidence" / "mock.sqlite3", meta="store_meta").mode == "mock"
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")
    with pytest.raises(EvidenceStoreError, match="Only a mock store"):
        live.mark_paid()
    live.close()


def test_what_is_said_of_a_store_the_other_worked_on_advises_no_deletion() -> None:
    for began, now in (("ember", "stand_in"), ("stand_in", "ember")):
        said = telling.other_teller(began, now)  # type: ignore[arg-type]
        for word in ("again", "delete", "another data directory", "remove"):
            assert word not in said
    assert "BEACON_MOCK_EMBER=live and its budget" in telling.other_teller("ember", "stand_in")


def test_two_that_open_a_new_store_together_begin_one_story(tmp_path: Path) -> None:
    # As a server without its pollers and `python -m beacon.ingest` do, sharing
    # the stores: whichever looks second goes on with what the first wrote.
    path = tmp_path / "evidence" / "mock.sqlite3"
    one, two = EvidenceStore(path, "mock"), EvidenceStore(path, "mock")
    first = ingest_setup.keep_teller(one, "stand_in", NOON, begin=True)
    second = ingest_setup.keep_teller(two, "stand_in", NOON + timedelta(minutes=7), begin=True)
    assert first == second == {"teller": "stand_in", "start": START.isoformat()}
    with pytest.raises(IngestConfigError, match="is one a stand-in for Muse Ember worked on"):
        ingest_setup.keep_teller(two, "ember", NOON, begin=True)
    assert two.state("scenario") == first
    one.close()
    two.close()


def test_the_pollers_command_does_not_run_a_telling_ember_is_paid_for(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beacon.ingest import __main__ as command

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("REPLAY", "0")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    with pytest.raises(SystemExit, match="is for the server"):
        command._poll(follow=False)
    # It shares the stores, and made none.
    assert list(tmp_path.iterdir()) == []


# A tap's clip and photo.


def test_a_server_says_where_a_clip_or_a_photo_is_sent_only_where_one_is_taken_and_sent(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beacon.investigate.setup import MEDIA_SENT

    # A model investigates and a visitor's own is taken: a clip can go to Meta.
    with serving(env, monkeypatch, Meta(tmp_path / "own")) as client:
        assert client.get("/api/health").json()["media_text"] == MEDIA_SENT
    assert "Meta's Model API" in MEDIA_SENT
    assert "Beacon cannot say what Meta keeps" in MEDIA_SENT
    # Only the invented places are taken: no clip, no photo, and nothing to say.
    invented = env | {"BEACON_FRONT_END": "unverified", "BEACON_DATA_DIR": str(tmp_path / "two")}
    with serving(invented, monkeypatch, Meta(tmp_path / "two")) as client:
        health = client.get("/api/health").json()
        assert (health["input"], health["media_text"]) == ("invented", None)


def test_a_stand_ins_server_sends_no_clip_anywhere_and_says_nothing_of_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["media_text"] is None


def test_who_makes_a_paying_client_is_asked_for_one_scope_at_a_time(
    env: dict[str, str], tmp_path: Path
) -> None:
    meta = Meta(tmp_path)
    make: Callable[[SpendScope], EmberClient] = meta.client

    ingest = build(env, Clock(lambda: NOON), paid=make)
    ingest.close()

    # One client for the radio and one for the notices, each under its own scope.
    assert meta.scopes == [telling.RADIO, telling.NOTICES]
