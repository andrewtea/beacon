"""The invented story with Muse Ember doing the model's work on it (`beacon.telling`).

No test asks Meta: the paying clients are live ones over `MetaApi`, which answers as
the stand-ins do, so what is tested is who is asked, with what, under which scope,
and what the rows and the answers then say of themselves.
"""

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from beacon import serve, telling
from beacon.ember import EmberClient, SpendScope, Storage
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
    with pytest.raises(IngestConfigError, match="was begun with Muse Ember"):
        build(stand_in, Clock(lambda: NOON))

    # Nor Ember with a stand-in's, a store from before that was written down included.
    other = tmp_path / "other"
    began = build({"BEACON_DATA_DIR": str(other)}, Clock(lambda: NOON))
    state = dict(began.store.state("scenario"))
    assert state.pop("teller") == "stand_in"
    began.store.add([], state=("scenario", state), now=NOON)
    began.close()
    with pytest.raises(IngestConfigError, match="was begun with a stand-in"):
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


def test_speech_is_synthesized_in_a_directory_of_its_own_that_is_gone_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beacon.radio import speech

    seen: list[Path] = []

    def ffmpeg(command: list[str], *, cwd: Path, **_: Any) -> Any:
        # Stands where ffmpeg is: it reads the words and writes the speech, by the
        # names it was given, in the directory it was started in.
        seen.append(cwd)
        assert cwd.stat().st_mode & 0o777 == 0o700
        assert sorted(path.name for path in cwd.iterdir()) == ["words.txt"]
        assert "-y" not in command
        (cwd / command[-1]).write_bytes(_clip((cwd / "words.txt").read_text()))

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(speech.subprocess, "run", ffmpeg)
    monkeypatch.setattr(speech.tempfile, "tempdir", str(tmp_path))
    # Something of somebody else's under the names an earlier Beacon used.
    theirs = tmp_path / ".speech.wav"
    theirs.write_bytes(b"theirs")

    assert speech.synthesize("Engine 36 is on scene.") == plain_wav(_clip("Engine 36 is on scene."))

    (work,) = seen
    assert work.parent == tmp_path
    assert work != tmp_path
    assert not work.exists()
    assert theirs.read_bytes() == b"theirs"


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
    assert by_ember == {"title": words.title, "text": words.text}
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
        pytest.raises(IngestConfigError, match="was begun with Muse Ember"),
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
    with pytest.raises(IngestConfigError, match="was begun with a stand-in"):
        build(env, Clock(lambda: NOON), paid=meta.client)
    # Refused before a paying client was made for it.
    assert meta.scopes == []

    # The pollers a stand-in runs beside it begin the story in that same store.
    began = build({"BEACON_DATA_DIR": str(tmp_path)}, Clock(lambda: NOON))
    assert began.store.state("scenario") == {"teller": "stand_in", "start": START.isoformat()}
    began.close()


def test_two_that_open_a_new_store_together_begin_one_story(tmp_path: Path) -> None:
    # As a server without its pollers and `python -m beacon.ingest` do, sharing
    # the stores: whichever looks second goes on with what the first wrote.
    path = tmp_path / "evidence" / "mock.sqlite3"
    one, two = EvidenceStore(path, "mock"), EvidenceStore(path, "mock")
    first = ingest_setup.keep_teller(one, "stand_in", NOON, begin=True)
    second = ingest_setup.keep_teller(two, "stand_in", NOON + timedelta(minutes=7), begin=True)
    assert first == second == {"teller": "stand_in", "start": START.isoformat()}
    with pytest.raises(IngestConfigError, match="was begun with a stand-in"):
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
