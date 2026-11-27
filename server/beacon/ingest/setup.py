"""Builds the pollers from the environment: real sources, or the mock scenario.

`BEACON_LIVE_SOURCES` decides. Unset or empty, which is the default, every poller
runs against the mock scenario, nothing touches the network, and rows go to the mock
evidence store with the origin `mock`. Set to a list of sources, such as `nws,511`,
those sources are polled for real and their rows go to the live store. A source that
is not named does not run in that mode: the live store holds nothing a stand-in
produced, so a mock poller has nowhere to write.

A source needs no code change to go live, only its name in that list and what it asks:

    nws       nothing
    511       BEACON_511_API_KEY
    adsblol   nothing; optionally BEACON_ADSBLOL_API_KEY, a key of adsb.lol's, and
              with it BEACON_ADSBLOL_REQUESTS_PER_DAY, what the key allows, without
              which a key is taken to allow the 500 a day that no key does
    recordings  BEACON_RECORDINGS_DIR, a directory of recorded radio calls on this
              machine, as trunk-recorder leaves them; BEACON_RECORDINGS_SYSTEM, the
              short name of the one system in it whose calls are read;
              BEACON_RECORDINGS_PERMITTED=1, which says the operator has the right
              to use the recordings; BEACON_RECORDINGS_TALKGROUPS, the channels to
              hear, by that system's numbers;
              BEACON_RECORDINGS_BUDGET_USD, the most hearing them may cost; a live
              Ember (REPLAY=0, MODEL_API_KEY, EMBER_SPENT_BEFORE_USD); and ffmpeg
              and bubblewrap, on Linux. BEACON_RADIO_STREETS_FILE and
              BEACON_RADIO_UNITS_FILE name the lists of streets and units a row
              may name, and without one a row names none.
              BEACON_RECORDINGS_EMS_TALKGROUPS is optional.
    notices   BEACON_NOTICES_FEED_URL, the https address of a JSON Feed that lists
              an authority's notices as PDF attachments; BEACON_NOTICES_BUDGET_USD,
              the most reading them may cost; and a live Ember (REPLAY=0,
              MODEL_API_KEY, EMBER_SPENT_BEFORE_USD). BEACON_NOTICES_HOSTS names
              hosts a document may be on besides the feed's own. Refused where the
              process cannot be kept from handing a core dump to a collector.

There is no source that asks anybody for radio audio. OpenMHz was asked whether
Beacon may use its API and said not to, so `openmhz` is no source and no setting
makes it one (`live_sources` says so to whoever names it), and no poller is sent to
an address of its, whoever names the address (`beacon.ingest.http.never_asked`).

Radio and notices are the sources that cost money: a call that is ready and worth
hearing is heard by Ember, and a document read by it, each at most once. In mock
mode the calls are tones and the documents are invented, and a stand-in for Ember
"hears" and "reads" them, which costs nothing.

The mock scenario is played on a clock of its own. Its story begins twenty minutes
before a mock store is first opened and runs in real time from there, in steps of ten
seconds, and how far it has been played is kept in the store, so a restart goes on
from the step after the last one that was finished. A step in which a poller failed
is not finished: it is played again, ten seconds later by the real clock, and only
the pollers that failed in it poll again.
"""

import contextlib
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from beacon import telling
from beacon.ember import EmberClient, SpendScope, Storage
from beacon.ember.client import REQUEST_TIMEOUT_SECONDS, default_data_dir
from beacon.ember.pricing import parse_usd
from beacon.evidence import EvidenceStore, Mode
from beacon.ingest.adsblol import MAX_REQUESTS_PER_DAY, MOCK_INTERVAL_SECONDS, AdsbLolAircraft
from beacon.ingest.http import SourceHttp, never_asked
from beacon.ingest.notices import SCOPE as NOTICES_SCOPE
from beacon.ingest.notices import NoticeFeed
from beacon.ingest.nws import NwsAlerts
from beacon.ingest.recordings import SCOPE as RADIO_SCOPE
from beacon.ingest.recordings import Directory, RecordedRadio
from beacon.ingest.runner import Clock, PaidWork, Poller, PollerStatus, Runner
from beacon.ingest.traffic511 import Traffic511
from beacon.radio import Ffmpeg, Known, core_dump_problem, forbid_core_dumps
from beacon.radio.speech import can_speak
from beacon.radio.vocabulary import read_names
from beacon.scenario import DURATION, Scenario, scenario_transport, stand_in_ember
from beacon.scenario import notices as scenario_notices
from beacon.scenario import radio as scenario_radio
from beacon.scenario.recorder import CALLS_DIRECTORY, RecorderError, StoryRecorder
from beacon.telling import Teller

# The sources that can be named in BEACON_LIVE_SOURCES.
SOURCES = ("nws", "adsblol", "511", "recordings", "notices")

LIVE_SOURCES_VARIABLE = "BEACON_LIVE_SOURCES"
KEY_511_VARIABLE = "BEACON_511_API_KEY"
ADSBLOL_KEY_VARIABLE = "BEACON_ADSBLOL_API_KEY"
ADSBLOL_REQUESTS_VARIABLE = "BEACON_ADSBLOL_REQUESTS_PER_DAY"
RECORDINGS_DIR_VARIABLE = "BEACON_RECORDINGS_DIR"
RECORDINGS_SYSTEM_VARIABLE = "BEACON_RECORDINGS_SYSTEM"
RECORDINGS_PERMITTED_VARIABLE = "BEACON_RECORDINGS_PERMITTED"
RECORDINGS_TALKGROUPS_VARIABLE = "BEACON_RECORDINGS_TALKGROUPS"
RECORDINGS_EMS_TALKGROUPS_VARIABLE = "BEACON_RECORDINGS_EMS_TALKGROUPS"
RECORDINGS_BUDGET_VARIABLE = "BEACON_RECORDINGS_BUDGET_USD"
# What is said to whoever names OpenMHz as a source.
NO_OPENMHZ = (
    "OpenMHz is not a source: it was asked whether Beacon may use its API and said not "
    "to, so Beacon has no client for it and requests nothing from it. Radio comes from "
    "recorded calls on this machine (recordings)."
)
RADIO_STREETS_VARIABLE = "BEACON_RADIO_STREETS_FILE"
RADIO_UNITS_VARIABLE = "BEACON_RADIO_UNITS_FILE"
NOTICES_FEED_VARIABLE = "BEACON_NOTICES_FEED_URL"
NOTICES_HOSTS_VARIABLE = "BEACON_NOTICES_HOSTS"
NOTICES_BUDGET_VARIABLE = "BEACON_NOTICES_BUDGET_USD"

# A mock store that is begun starts its story this long ago, so the first thing a
# developer sees is a fire in progress and not an empty city.
MOCK_HEAD_START = timedelta(minutes=20)
# How far apart the moments are at which the story is played. It is no longer than
# the shortest interval a poller has.
SCENARIO_STEP = timedelta(seconds=10)

_SCENARIO_STATE = "scenario"
# Long enough after the story for the pollers to notice what stopped being listed.
# Nothing in the story changes after that, and it is played no further.
_AFTER_THE_STORY = timedelta(minutes=10)

# How long `Ingest.stop` waits for the pollers' thread to end. Nothing it could be
# doing takes that long once its clients are closed, except waiting for the answer
# to a paid request.
STOP_SECONDS = 5.0
# How long `Ingest.stop` waits for a paid request that is out: as long as Ember may
# take to answer one, and a little for settling it.
PAID_SECONDS = REQUEST_TIMEOUT_SECONDS + 5.0

logger = logging.getLogger("beacon.ingest")

_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


class IngestConfigError(Exception):
    """The environment asks for pollers that cannot be built."""


class Ingest:
    """The store, the pollers, and the clock they share, and the thread they run on."""

    def __init__(
        self,
        store: EvidenceStore,
        runner: Runner,
        clock: Clock,
        http: SourceHttp,
        scenario: Scenario | None,
        paid: Sequence[PaidWork] = (),
        *,
        teller: Teller = "stand_in",
        recorder: StoryRecorder | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.clock = clock
        self.http = http
        # The pollers that pay for their work, which shutting down has to tell.
        self.paid = tuple(paid)
        # The story the pollers are fed, in mock mode.
        self.scenario = scenario
        # Who does the model's work on that story (`beacon.telling`), and, when it
        # is Muse Ember, what leaves the story's calls as files for it to hear.
        self.teller: Teller = teller
        self.recorder = recorder
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def catch_up(self, stop: threading.Event | None = None) -> bool:
        """In mock mode, play the story from the step after the last one that was
        finished up to now, and say whether it got there. It does nothing in live
        mode: a real source cannot be asked what it said earlier.

        How far the story has been played is saved after each step, with how each
        poller's last poll went, apart from anything else the store holds. A step
        that was cut short is played again, and playing a step twice stores nothing
        twice. A step in which a poller failed is not saved as played, and the
        playing ends there, with False: the caller plays it again later."""
        if self.scenario is None:
            return True
        played = self.store.state(_SCENARIO_STATE).get("played_to")
        moment = (
            datetime.fromisoformat(played) + SCENARIO_STEP
            if isinstance(played, str)
            else self.scenario.start
        )
        end = min(self.clock.now(), self.scenario.start + DURATION + _AFTER_THE_STORY)
        try:
            while moment <= end:
                if stop is not None and stop.is_set():
                    return False
                self.clock.hold(moment)
                if self.recorder is not None:
                    try:
                        # The calls made by this moment, before the radio is polled.
                        self.recorder.leave(moment)
                    except RecorderError as error:
                        logger.error("%s", error)
                        return False
                results = self.runner.step(stop, failed_stay_due=True)
                if stop is not None and stop.is_set():
                    # The step may not have polled every source, so it is not done.
                    return False
                if any(isinstance(result, Exception) for result in results.values()):
                    return False
                state = {
                    "start": self.scenario.start.isoformat(),
                    "teller": self.teller,
                    "played_to": moment.isoformat(),
                    "polls": {
                        status.name: {
                            "polled_at": status.polled_at.isoformat(),
                            "ok": status.ok,
                            "partial": status.partial,
                        }
                        for status in self.runner.status()
                        if status.polled_at is not None
                    },
                }
                try:
                    self.store.add([], state=(_SCENARIO_STATE, state), now=moment)
                except Exception as error:
                    # The step is played again, which stores nothing twice.
                    if not self.store.closed:
                        logger.error(
                            "A step of the mock scenario was not saved (%s).", type(error).__name__
                        )
                    return False
                moment += SCENARIO_STEP
        finally:
            self.clock.hold(None)
        return True

    def run(self, stop: threading.Event) -> None:
        """Poll until `stop` is set. In mock mode that is playing the story as the
        time for each of its steps comes."""
        if self.scenario is None:
            self.runner.run(stop)
            return
        while not stop.is_set():
            self.catch_up(stop)
            # The next step is this far off, and so is another try at one that failed.
            stop.wait(SCENARIO_STEP.total_seconds())

    def start(self) -> None:
        """Run the pollers on a thread of their own until `stop` is called."""
        if self._thread is not None:
            raise RuntimeError("The pollers were already started.")
        thread = threading.Thread(
            target=self.run, args=(self._stop,), name="beacon-ingest", daemon=True
        )
        thread.start()
        # Kept only once it runs: `stop` joins it, and a thread that never started
        # cannot be joined.
        self._thread = thread

    def stop(self, timeout: float = STOP_SECONDS, paid_timeout: float = PAID_SECONDS) -> bool:
        """Stop the pollers, and say whether they have stopped: no paid work is
        out, and their thread has ended. It blocks, so a server calls it off its
        event loop.

        What costs nothing is called off, not waited for: a request to a source is
        cancelled, a conversion is ended, and a write that is waiting for the file
        gives up. No paid request is sent that was not already out. Work whose paid
        request is out is waited for, for up to `paid_timeout` seconds, because it
        may be paid for whatever is done here and its answer is worth storing. Then
        the store is closed, and the thread is given `timeout` seconds to end. A
        poll is stored whole or not at all, so nothing is left half done.

        Only True is a stop that is complete. Whatever it answers, once this
        returns no source is asked, no new paid work is admitted, and no row is
        stored. When it answers False, work that was admitted before the stop is
        still out: its paid request can still be sent, and paid for, after this
        returns. It is settled in the Ember ledger when it ends, its row is lost,
        and its call stays claimed and is never asked about again."""
        self._stop.set()
        for work in self.paid:
            work.close()
        self.http.close()
        drained = True
        for work in self.paid:
            drained = work.drain(paid_timeout) and drained
        self.store.close()
        ended = True
        if self._thread is not None:
            self._thread.join(timeout)
            ended = not self._thread.is_alive()
        return drained and ended

    def close_paid(self) -> None:
        """Have the pollers that pay for their work begin no more paid requests,
        and wait for nothing. It is the first thing a shutdown does, before it
        waits for anything else; `stop` does it again, which changes nothing."""
        for work in self.paid:
            work.close()

    def close(self) -> None:
        """Close the pollers that pay for their work, which then begin no paid
        request; the HTTP client, which ends any request a poller has out; and the
        store, which then takes no more rows. Nothing is waited for: `stop` is the
        way to end pollers that run on a thread."""
        self.close_paid()
        self.http.close()
        self.store.close()


def live_sources(env: Mapping[str, str]) -> tuple[str, ...]:
    """The sources `BEACON_LIVE_SOURCES` names, in the order of `SOURCES`. This is
    the one reading of that setting: it decides which store is opened and which
    pollers are built.

    Raises IngestConfigError for a name that is not a source."""
    named = {name.strip() for name in env.get(LIVE_SOURCES_VARIABLE, "").split(",")} - {""}
    if "openmhz" in {name.lower() for name in named}:
        raise IngestConfigError(f"{LIVE_SOURCES_VARIABLE} names openmhz. {NO_OPENMHZ}")
    unknown = sorted(named - set(SOURCES))
    if unknown:
        raise IngestConfigError(
            f"{LIVE_SOURCES_VARIABLE} names {', '.join(unknown)}, which is not a source. "
            f"The sources are {', '.join(SOURCES)}."
        )
    return tuple(source for source in SOURCES if source in named)


def open_store(env: Mapping[str, str]) -> EvidenceStore:
    """The live evidence store when `BEACON_LIVE_SOURCES` names a source, else the
    mock one, each in a file of its own under the data directory.

    Raises IngestConfigError as `live_sources` does, EvidenceStoreError when the
    file was begun as the other store, and EmberConfigError for a relative
    `BEACON_DATA_DIR`."""
    mode: Mode = "live" if live_sources(env) else "mock"
    return EvidenceStore(default_data_dir(env) / "evidence" / f"{mode}.sqlite3", mode)


def build(
    env: Mapping[str, str],
    clock: Clock | None = None,
    *,
    paid: telling.PaidClient | None = None,
) -> Ingest:
    """The store, the pollers, and the clock they share. `paid` is how a paying
    client is made for the invented story when Muse Ember works on it
    (`beacon.telling`), which a test hands in.

    Raises IngestConfigError when a live source lacks a setting it needs,
    TellingConfigError for a setting of `beacon.telling` that cannot be had, and
    what `open_store` raises."""
    clock = Clock() if clock is None else clock
    live = live_sources(env)
    teller = telling.teller_of(env)
    if live:
        if teller == "ember":
            raise IngestConfigError(
                f"{telling.MOCK_EMBER_VARIABLE}=live is for the invented story: with a real "
                f"source named ({LIVE_SOURCES_VARIABLE}), Muse Ember works on that source "
                "under the source's own settings."
            )
        # Checked before the store is opened, so a refused setting leaves no file.
        http = SourceHttp()
        pollers = source_pollers(live, env, http)
        store = open_store(env)
        paying = [poller for poller in pollers if isinstance(poller, PaidWork)]
        return Ingest(store, Runner(pollers, store, clock), clock, http, None, paying)
    if teller == "ember":
        return _told_by_ember(env, clock, paid)
    store = open_store(env)
    scenario = _scenario(store, clock.now(), teller)
    http = SourceHttp(scenario_transport(scenario, clock.now))
    # Stands in for Ember: it spends nothing, and what it "hears" in the story's
    # tones is the story's own. Its rows say `model == "fake"`.
    storage = Storage.under(default_data_dir(env))
    ember = EmberClient.fake(storage, stand_in_ember)
    radio = RecordedRadio(
        # The story's calls, where a directory of recordings would be.
        scenario_radio.StoryShelf(scenario.start, clock.now),
        ember,
        talkgroups=scenario_radio.TALKGROUPS,
        ems_talkgroups=scenario_radio.EMS_TALKGROUPS,
        # The story's own streets and units, where real radio has the city's.
        known=scenario_radio.KNOWN,
    )
    # A client of its own, as each paying poller has, which "reads" the story's
    # documents as the other "hears" its tones.
    notices = NoticeFeed(
        http,
        EmberClient.fake(storage, scenario_notices.stand_in_reader(scenario.start)),
        feed_url=scenario_notices.FEED_URL,
    )
    return _mock_ingest(store, clock, http, scenario, radio, notices, teller, None)


def _told_by_ember(env: Mapping[str, str], clock: Clock, paid: telling.PaidClient | None) -> Ingest:
    """The invented story with Muse Ember hearing its calls and reading its
    notices (`beacon.telling`). The pollers are the ones a stand-in's telling has;
    the calls are synthesized speech in a directory marked as generated audio,
    which the reader of recorded calls reads, and both clients are live ones, each
    under a scope of the telling's own.

    Raises IngestConfigError when this host cannot synthesize speech or the story
    was begun with a stand-in, TellingConfigError when the budget or a live Ember
    is missing, and RecorderError when the calls' directory is not Beacon's."""
    if not can_speak():
        raise IngestConfigError(
            f"{telling.MOCK_EMBER_VARIABLE}=live has Muse Ember hear the invented story's "
            "radio calls as synthesized speech, which needs ffmpeg with the flite voice "
            "on this host. A tone says nothing, and is not sent to a paid Ember."
        )
    # Both clients before the store is opened, so a refused setting leaves no file.
    clients: list[EmberClient] = []
    store: EvidenceStore | None = None
    try:
        clients.append(telling.paying(env, telling.RADIO, paid))
        clients.append(telling.paying(env, telling.NOTICES, paid))
        store = open_store(env)
        scenario = _scenario(store, clock.now(), "ember")
        recorder = StoryRecorder(default_data_dir(env).joinpath(*CALLS_DIRECTORY), scenario.start)
        recorder.begin()
        http = SourceHttp(scenario_transport(scenario, clock.now))
        radio = RecordedRadio(
            Directory(recorder.root, scenario_radio.SYSTEM),
            clients[0],
            talkgroups=scenario_radio.TALKGROUPS,
            ems_talkgroups=scenario_radio.EMS_TALKGROUPS,
            known=scenario_radio.KNOWN,
            scope=telling.RADIO,
        )
        notices = NoticeFeed(
            http,
            clients[1],
            feed_url=scenario_notices.FEED_URL,
            scope=telling.NOTICES,
            generated=True,
        )
    except BaseException:
        # What was built is closed when the pollers it was for are not.
        for client in clients:
            client.close()
        if store is not None:
            store.close()
        raise
    return _mock_ingest(store, clock, http, scenario, radio, notices, "ember", recorder)


def _mock_ingest(
    store: EvidenceStore,
    clock: Clock,
    http: SourceHttp,
    scenario: Scenario,
    radio: RecordedRadio,
    notices: NoticeFeed,
    teller: Teller,
    recorder: StoryRecorder | None,
) -> Ingest:
    """The invented story's pollers, whoever hears its calls and reads its notices."""
    pollers = [
        NwsAlerts(http),
        AdsbLolAircraft(http, interval_seconds=MOCK_INTERVAL_SECONDS),
        Traffic511(http, api_key="mock"),
        radio,
        notices,
    ]
    last = _saved_polls(store.state(_SCENARIO_STATE).get("polls"))
    runner = Runner(pollers, store, clock, last=last)
    return Ingest(
        store, runner, clock, http, scenario, [radio, notices], teller=teller, recorder=recorder
    )


def source_pollers(
    sources: tuple[str, ...], env: Mapping[str, str], http: SourceHttp
) -> list[Poller]:
    """The pollers for these sources, with the settings each needs read from `env`.
    `build` hands it the real network. A test hands it a stand-in for the network,
    and the rows are then `mock` ones, because that is the origin of such a client
    (`beacon.ingest.http.SourceHttp`): what a stand-in answers is not a real
    source's however the request was made, and there is no way to say otherwise.

    Raises IngestConfigError when a source lacks a setting it needs."""
    if "notices" in sources:
        # Before any poller is made, and so before a client that can spend opens
        # its ledger: an address Beacon does not ask stops the whole server.
        _refuse_never_asked(env)
    pollers: list[Poller] = []
    if "nws" in sources:
        pollers.append(NwsAlerts(http))
    if "adsblol" in sources:
        pollers.append(_aircraft_poller(env, http))
    if "511" in sources:
        token = env.get(KEY_511_VARIABLE)
        if not token:
            raise IngestConfigError(
                f"{LIVE_SOURCES_VARIABLE} names 511, which needs {KEY_511_VARIABLE}."
            )
        pollers.append(Traffic511(http, api_key=token))
    if "recordings" in sources:
        pollers.append(_radio_poller(env))
    if "notices" in sources:
        pollers.append(_notice_poller(env, http))
    return pollers


def _aircraft_poller(env: Mapping[str, str], http: SourceHttp) -> AdsbLolAircraft:
    """The poller for adsb.lol, which needs no key and takes one.

    Raises IngestConfigError when the key or what it allows is not one."""
    api_key = env.get(ADSBLOL_KEY_VARIABLE) or None
    allowed = env.get(ADSBLOL_REQUESTS_VARIABLE, "").strip()
    requests_per_day = None
    if allowed:
        if api_key is None:
            raise IngestConfigError(
                f"{ADSBLOL_REQUESTS_VARIABLE} says what a key allows, and {ADSBLOL_KEY_VARIABLE} "
                "is not set: with no key adsb.lol allows an address 500 requests a day."
            )
        if not (allowed.isascii() and allowed.isdigit() and len(allowed) <= 6):
            raise IngestConfigError(
                f"{ADSBLOL_REQUESTS_VARIABLE} is how many requests a day the key allows, "
                f"from 1 to {MAX_REQUESTS_PER_DAY}."
            )
        requests_per_day = int(allowed)
    try:
        return AdsbLolAircraft(http, api_key=api_key, requests_per_day=requests_per_day)
    except ValueError:
        # Not the error itself, which would be chained to a setting.
        pass
    raise IngestConfigError(
        f"{ADSBLOL_KEY_VARIABLE} is not a key of adsb.lol's, or {ADSBLOL_REQUESTS_VARIABLE} is "
        f"not a number of requests from 1 to {MAX_REQUESTS_PER_DAY}."
    )


def _radio_poller(env: Mapping[str, str]) -> RecordedRadio:
    """The poller for recorded radio, heard by a live Ember under a budget of its own.

    Raises IngestConfigError when the directory, the operator's word that the
    recordings may be used, the talkgroups, the budget, a live Ember, or a confined
    ffmpeg is missing, when the directory holds generated audio, when the host hands
    core dumps to a collector, or when a list of names cannot be read."""
    names = f"{LIVE_SOURCES_VARIABLE} names recordings"
    where, system = env.get(RECORDINGS_DIR_VARIABLE, ""), env.get(RECORDINGS_SYSTEM_VARIABLE, "")
    shelf = None
    if where and system:
        # Not the error itself, which would be chained to a setting.
        with contextlib.suppress(ValueError, OSError):
            shelf = Directory(Path(where), system)
    if shelf is None:
        raise IngestConfigError(
            f"{names}, which needs {RECORDINGS_DIR_VARIABLE}: the absolute path of a "
            "directory of recorded calls, as trunk-recorder leaves them (the directory "
            f"itself, not a link to it), and {RECORDINGS_SYSTEM_VARIABLE}: the short name "
            "of the one system in it whose calls are read, which is the name of its "
            "directory there. Talkgroup numbers are a system's own. A file named "
            "BEACON-GENERATED at the directory's top marks generated audio, and anything "
            "else of that name is refused."
        )
    if shelf.generated:
        raise IngestConfigError(
            f"{names}, and the directory {RECORDINGS_DIR_VARIABLE} names is marked as "
            "generated audio, which is no radio and no live source."
        )
    if env.get(RECORDINGS_PERMITTED_VARIABLE) != "1":
        raise IngestConfigError(
            f"{names}, and a recording of radio is used only with the right to: set "
            f"{RECORDINGS_PERMITTED_VARIABLE}=1 when the recordings are your own, or you "
            "have the permission of whoever made them. Beacon does not use OpenMHz's "
            "recordings, and has no license from Broadcastify."
        )
    talkgroups = _talkgroups(env, RECORDINGS_TALKGROUPS_VARIABLE)
    if not talkgroups:
        raise IngestConfigError(
            f"{names}, which needs {RECORDINGS_TALKGROUPS_VARIABLE}: the numbers of the "
            "fire and EMS talkgroups to hear. Every call on them is paid for."
        )
    ems_talkgroups = _talkgroups(env, RECORDINGS_EMS_TALKGROUPS_VARIABLE)
    if not set(ems_talkgroups) <= set(talkgroups):
        raise IngestConfigError(
            f"{RECORDINGS_EMS_TALKGROUPS_VARIABLE} names a talkgroup that "
            f"{RECORDINGS_TALKGROUPS_VARIABLE} does not."
        )
    scope = _scope(env, RADIO_SCOPE, RECORDINGS_BUDGET_VARIABLE)
    if scope is None:
        raise IngestConfigError(
            f"{names}, which needs {RECORDINGS_BUDGET_VARIABLE}: the most, in dollars, that "
            "hearing radio may cost over every run. It has no default, because every "
            "call is paid for."
        )
    # ffmpeg is shown the system's programs and nothing of Beacon's, wherever
    # Beacon's data happens to be kept.
    transcoder = Ffmpeg(hidden=[default_data_dir(env)])
    problem = transcoder.problem() or core_dump_problem()
    if problem is not None:
        raise IngestConfigError(f"{names}. {problem}")
    known = read_known(env)
    ember = EmberClient.from_env(env, scope=scope)
    if ember.mode != "live":
        raise IngestConfigError(
            f"{names}, and a radio call is heard by a live Ember and cannot be replayed: "
            "set REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD."
        )
    # This process will hold real radio audio in memory. It writes no core file,
    # and the host was just seen not to hand cores to a collector.
    forbid_core_dumps()
    return RecordedRadio(
        shelf,
        ember,
        talkgroups=talkgroups,
        ems_talkgroups=ems_talkgroups,
        known=known,
        transcoder=transcoder,
    )


def read_known(env: Mapping[str, str]) -> Known:
    """The streets and the units a live radio row may name, from the files the
    environment names: one name to a line. With no file for one of them, a row
    names none of that kind, and the log says so. The redaction pass lets a text
    name the same streets and units (`beacon.incident.setup`).

    Raises IngestConfigError when a file that is named cannot be read."""
    names: dict[str, list[str]] = {}
    for what, variable in (("streets", RADIO_STREETS_VARIABLE), ("units", RADIO_UNITS_VARIABLE)):
        path = env.get(variable)
        text = ""
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise IngestConfigError(
                    f"{variable} names a file that cannot be read as text."
                ) from error
        names[what] = read_names(text)
        if not names[what]:
            logger.warning(
                "No list of %s (%s): a radio row will name none of them.", what, variable
            )
    known, left_out = Known.kept(streets=names["streets"], units=names["units"])
    if left_out:
        logger.warning(
            "%d names in the lists of streets and units are not names Beacon keeps, "
            "and were left out.",
            left_out,
        )
    return known


def _refuse_never_asked(env: Mapping[str, str]) -> None:
    """Raises IngestConfigError when the notices' feed, or a host a document may be
    on, is an address Beacon does not ask (`beacon.ingest.http.never_asked`)."""
    feed_url = env.get(NOTICES_FEED_VARIABLE, "").strip()
    hosts = _hosts(env, NOTICES_HOSTS_VARIABLE)
    if never_asked(feed_url) or any(never_asked(host) for host in hosts):
        raise IngestConfigError(
            f"{LIVE_SOURCES_VARIABLE} names notices, and {NOTICES_FEED_VARIABLE} or "
            f"{NOTICES_HOSTS_VARIABLE} names an address of a service Beacon requests "
            "nothing from (OpenMHz, which said not to use its API, or Broadcastify, "
            "from which Beacon has no license), or a host that cannot be read."
        )


def _notice_poller(env: Mapping[str, str], http: SourceHttp) -> NoticeFeed:
    """The poller for a real feed of notices, read by a live Ember under a budget
    of its own.

    Raises IngestConfigError when the feed, the budget, or a live Ember is missing,
    and when a crash of this process could hand a document to a core collector."""
    names = f"{LIVE_SOURCES_VARIABLE} names notices"
    _refuse_never_asked(env)
    feed_url = env.get(NOTICES_FEED_VARIABLE, "").strip()
    hosts = _hosts(env, NOTICES_HOSTS_VARIABLE)
    scope = _scope(env, NOTICES_SCOPE, NOTICES_BUDGET_VARIABLE)
    if scope is None:
        raise IngestConfigError(
            f"{names}, which needs {NOTICES_BUDGET_VARIABLE}: the most, in dollars, that "
            "reading notices may cost over every run. It has no default, because every "
            "document is paid for."
        )
    # This process will hold each document, and what Ember makes of it, in memory,
    # and a document at an address somebody listed can be anything. So it is made
    # a process of which no core is written, as the server is before it takes a
    # tap, whichever command runs the pollers. No program is started for a notice,
    # so the mark is enough where it can be set. Before a client that can spend
    # is built.
    if not forbid_core_dumps() and core_dump_problem() is not None:
        raise IngestConfigError(
            f"{names}, and this process could not mark itself as one of which no core "
            "dump is made, on a host that hands core dumps to a collector "
            "(kernel.core_pattern begins with | or @, or cannot be read): a crash would "
            "hand over the document being read. Set kernel.core_pattern to a file name, "
            "such as core."
        )
    ember = EmberClient.from_env(env, scope=scope)
    if ember.mode != "live":
        raise IngestConfigError(
            f"{names}, and a notice is read by a live Ember and cannot be replayed: "
            "set REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD."
        )
    try:
        return NoticeFeed(http, ember, feed_url=feed_url, document_hosts=hosts)
    except ValueError:
        # Not the error itself, which would be chained to a setting.
        pass
    raise IngestConfigError(
        f"{names}, which needs {NOTICES_FEED_VARIABLE}: the https address of a JSON Feed "
        "(jsonfeed.org) that lists an authority's notices as PDF attachments."
    )


def _hosts(env: Mapping[str, str], variable: str) -> list[str]:
    """The host names a setting lists. Raises IngestConfigError for what is not one."""
    hosts = [host.strip().lower() for host in env.get(variable, "").split(",")]
    hosts = [host for host in hosts if host]
    if not all(_HOST.fullmatch(host) for host in hosts):
        raise IngestConfigError(f"{variable} is a list of host names, such as media.example.org.")
    return hosts


def _scope(env: Mapping[str, str], label: str, variable: str) -> SpendScope | None:
    """The spending scope a paying poller's calls are made under, with the budget
    the environment sets for it, or None when that is not a dollar amount a scope
    takes."""
    try:
        return SpendScope(label, parse_usd(env.get(variable, "")))
    except ValueError:
        return None


def _talkgroups(env: Mapping[str, str], variable: str) -> tuple[int, ...]:
    numbers = [number.strip() for number in env.get(variable, "").split(",")]
    numbers = [number for number in numbers if number]
    if not all(number.isascii() and number.isdigit() and len(number) <= 7 for number in numbers):
        raise IngestConfigError(f"{variable} is a list of talkgroup numbers, such as 1201,1203.")
    return tuple(dict.fromkeys(int(number) for number in numbers))


def _saved_polls(saved: Any) -> list[PollerStatus]:
    """How each poller's last poll went, as a finished step of the story saved it."""
    polls: list[PollerStatus] = []
    for name, poll in (saved if isinstance(saved, dict) else {}).items():
        polled_at = poll.get("polled_at") if isinstance(poll, dict) else None
        ok = poll.get("ok") if isinstance(poll, dict) else None
        if isinstance(polled_at, str) and isinstance(ok, bool):
            partial = poll.get("partial") is True
            polls.append(PollerStatus(name, datetime.fromisoformat(polled_at), ok, partial))
    return polls


def _scenario(store: EvidenceStore, now: datetime, teller: Teller) -> Scenario:
    """The story of this mock store. Its start is drawn once and kept in the store,
    so a restart continues the story it was telling and does not begin a second one
    beside it. Deleting the mock store's file begins it again.

    Who does the model's work on the story is kept with it (`beacon.telling`): a
    story is told by a stand-in or by Muse Ember from its beginning to its end. A
    store from before that was written down was a stand-in's.

    Raises IngestConfigError when the story was begun by the other."""
    state = store.state(_SCENARIO_STATE)
    saved = state.get("start")
    if isinstance(saved, str):
        began: Teller = "ember" if state.get("teller") == "ember" else "stand_in"
        if began != teller:
            raise IngestConfigError(telling.other_teller(began, teller))
        return Scenario(start=datetime.fromisoformat(saved))
    start = now.replace(microsecond=0) - MOCK_HEAD_START
    begun = {"start": start.isoformat(), "teller": teller}
    store.add([], state=(_SCENARIO_STATE, begun), now=now)
    return Scenario(start=start)
