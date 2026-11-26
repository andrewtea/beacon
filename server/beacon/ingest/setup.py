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
    opensky   BEACON_OPENSKY_PERMITTED=1, which says OpenSky has given Beacon the
              written license its terms require, and optionally OPENSKY_CLIENT_ID
              and OPENSKY_CLIENT_SECRET for a larger quota
    openmhz   BEACON_OPENMHZ_PERMITTED=1, which says OpenMHz has given Beacon
              permission to use its audio; BEACON_OPENMHZ_TALKGROUPS, the channels
              to hear; BEACON_OPENMHZ_BUDGET_USD, the most hearing them may cost;
              a live Ember (REPLAY=0, MODEL_API_KEY, EMBER_SPENT_BEFORE_USD); and
              ffmpeg, on Linux. BEACON_OPENMHZ_EMS_TALKGROUPS and
              BEACON_OPENMHZ_MEDIA_HOSTS are optional.

Radio is the one source that costs money: every call is heard by Ember. In mock mode
the calls are tones and a stand-in for Ember "hears" them, which costs nothing.

The mock scenario is played on a clock of its own. Its story begins twenty minutes
before a mock store is first opened and runs in real time from there, in steps of ten
seconds, and how far it has been played is kept in the store, so a restart goes on
from the step after the last one that was finished. A step in which a poller failed
is not finished: it is played again, ten seconds later by the real clock, and only
the pollers that failed in it poll again.
"""

import logging
import os
import re
import shutil
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from beacon.ember import EmberClient, SpendScope, Storage
from beacon.ember.client import default_data_dir
from beacon.ember.pricing import parse_usd
from beacon.evidence import EvidenceStore, Mode
from beacon.ingest.http import SourceHttp
from beacon.ingest.nws import NwsAlerts
from beacon.ingest.openmhz import OpenMhzRadio
from beacon.ingest.opensky import SIGNED_IN_INTERVAL_SECONDS, OpenSkyAircraft
from beacon.ingest.runner import Clock, Poller, PollerStatus, Runner
from beacon.ingest.traffic511 import Traffic511
from beacon.radio import Ffmpeg
from beacon.scenario import DURATION, Scenario, scenario_transport, stand_in_ember
from beacon.scenario import radio as scenario_radio

# The sources that can be named in BEACON_LIVE_SOURCES.
SOURCES = ("nws", "opensky", "511", "openmhz")

LIVE_SOURCES_VARIABLE = "BEACON_LIVE_SOURCES"
KEY_511_VARIABLE = "BEACON_511_API_KEY"
OPENSKY_PERMITTED_VARIABLE = "BEACON_OPENSKY_PERMITTED"
OPENSKY_ID_VARIABLE = "OPENSKY_CLIENT_ID"
OPENSKY_SECRET_VARIABLE = "OPENSKY_CLIENT_SECRET"
OPENMHZ_PERMITTED_VARIABLE = "BEACON_OPENMHZ_PERMITTED"
OPENMHZ_TALKGROUPS_VARIABLE = "BEACON_OPENMHZ_TALKGROUPS"
OPENMHZ_EMS_TALKGROUPS_VARIABLE = "BEACON_OPENMHZ_EMS_TALKGROUPS"
OPENMHZ_MEDIA_HOSTS_VARIABLE = "BEACON_OPENMHZ_MEDIA_HOSTS"
OPENMHZ_BUDGET_VARIABLE = "BEACON_OPENMHZ_BUDGET_USD"

# The label of the spending scope every live radio call is made under.
RADIO_SCOPE = "ingest:openmhz"

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
# doing takes that long once its client and its store are closed.
STOP_SECONDS = 5.0

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
    ) -> None:
        self.store = store
        self.runner = runner
        self.clock = clock
        self.http = http
        # The story the pollers are fed, in mock mode.
        self.scenario = scenario
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
                results = self.runner.step(stop, failed_stay_due=True)
                if stop is not None and stop.is_set():
                    # The step may not have polled every source, so it is not done.
                    return False
                if any(isinstance(result, Exception) for result in results.values()):
                    return False
                state = {
                    "start": self.scenario.start.isoformat(),
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
        self._thread = threading.Thread(
            target=self.run, args=(self._stop,), name="beacon-ingest", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_SECONDS) -> bool:
        """Stop the pollers, and say whether their thread has ended. It blocks for
        up to `timeout` seconds, so a server calls it off its event loop.

        Whatever the thread is doing is called off, not waited for: a request that
        is out is cancelled, and a write that is waiting for the file gives up. A
        poll is stored whole or not at all, so nothing is left half done, and the
        poll is made again when Beacon next runs. Whether or not the thread has
        ended, it can ask no source and store no row after this returns."""
        self._stop.set()
        self.close()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def close(self) -> None:
        """Close the HTTP client, which ends any request a poller has out, and the
        store, which then takes no more rows."""
        self.http.close()
        self.store.close()


def live_sources(env: Mapping[str, str]) -> tuple[str, ...]:
    """The sources `BEACON_LIVE_SOURCES` names, in the order of `SOURCES`. This is
    the one reading of that setting: it decides which store is opened and which
    pollers are built.

    Raises IngestConfigError for a name that is not a source."""
    named = {name.strip() for name in env.get(LIVE_SOURCES_VARIABLE, "").split(",")} - {""}
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


def build(env: Mapping[str, str], clock: Clock | None = None) -> Ingest:
    """The store, the pollers, and the clock they share.

    Raises IngestConfigError when a live source lacks a setting it needs, and what
    `open_store` raises."""
    clock = Clock() if clock is None else clock
    live = live_sources(env)
    if live:
        # Checked before the store is opened, so a refused setting leaves no file.
        http = SourceHttp()
        pollers = source_pollers(live, env, http)
        store = open_store(env)
        return Ingest(store, Runner(pollers, store, clock), clock, http, None)
    store = open_store(env)
    scenario = _scenario(store, clock.now())
    http = SourceHttp(scenario_transport(scenario, clock.now))
    # Stands in for Ember: it spends nothing, and what it "hears" in the story's
    # tones is the story's own. Its rows say `model == "fake"`.
    ember = EmberClient.fake(Storage.under(default_data_dir(env)), stand_in_ember)
    pollers = [
        NwsAlerts(http),
        OpenSkyAircraft(http, interval_seconds=SIGNED_IN_INTERVAL_SECONDS),
        Traffic511(http, api_key="mock"),
        OpenMhzRadio(
            http,
            ember,
            talkgroups=scenario_radio.TALKGROUPS,
            ems_talkgroups=scenario_radio.EMS_TALKGROUPS,
            system=scenario_radio.SYSTEM,
        ),
    ]
    last = _saved_polls(store.state(_SCENARIO_STATE).get("polls"))
    return Ingest(store, Runner(pollers, store, clock, last=last), clock, http, scenario)


def source_pollers(
    sources: tuple[str, ...], env: Mapping[str, str], http: SourceHttp
) -> list[Poller]:
    """The pollers for these sources, with the settings each needs read from `env`.
    `build` hands it the real network. A test hands it a stand-in for the network,
    and the rows are then `mock` ones, because that is the origin of such a client
    (`beacon.ingest.http.SourceHttp`): what a stand-in answers is not a real
    source's however the request was made, and there is no way to say otherwise.

    Raises IngestConfigError when a source lacks a setting it needs."""
    pollers: list[Poller] = []
    if "nws" in sources:
        pollers.append(NwsAlerts(http))
    if "opensky" in sources:
        if env.get(OPENSKY_PERMITTED_VARIABLE) != "1":
            raise IngestConfigError(
                f"{LIVE_SOURCES_VARIABLE} names opensky, and OpenSky's terms of use "
                "(https://opensky-network.org/about/terms-of-use) require a written "
                "license for using its REST API in a running service. Ask "
                f"contact@opensky-network.org, and set {OPENSKY_PERMITTED_VARIABLE}=1 "
                "once the license is given."
            )
        client_id = env.get(OPENSKY_ID_VARIABLE) or None
        client_secret = env.get(OPENSKY_SECRET_VARIABLE) or None
        if (client_id is None) != (client_secret is None):
            raise IngestConfigError(
                f"{OPENSKY_ID_VARIABLE} and {OPENSKY_SECRET_VARIABLE} are set together, "
                "or neither is."
            )
        pollers.append(OpenSkyAircraft(http, client_id=client_id, client_secret=client_secret))
    if "511" in sources:
        token = env.get(KEY_511_VARIABLE)
        if not token:
            raise IngestConfigError(
                f"{LIVE_SOURCES_VARIABLE} names 511, which needs {KEY_511_VARIABLE}."
            )
        pollers.append(Traffic511(http, api_key=token))
    if "openmhz" in sources:
        pollers.append(_radio_poller(env, http))
    return pollers


def _radio_poller(env: Mapping[str, str], http: SourceHttp) -> OpenMhzRadio:
    """The poller for real radio, heard by a live Ember under a budget of its own.

    Raises IngestConfigError when permission, the talkgroups, the budget, a live
    Ember, or ffmpeg is missing."""
    names = f"{LIVE_SOURCES_VARIABLE} names openmhz"
    if env.get(OPENMHZ_PERMITTED_VARIABLE) != "1":
        raise IngestConfigError(
            f"{names}, and using OpenMHz's audio needs its permission. Ask the contact "
            f"on its San Francisco page, and set {OPENMHZ_PERMITTED_VARIABLE}=1 once "
            "permission is given."
        )
    talkgroups = _talkgroups(env, OPENMHZ_TALKGROUPS_VARIABLE)
    if not talkgroups:
        raise IngestConfigError(
            f"{names}, which needs {OPENMHZ_TALKGROUPS_VARIABLE}: the numbers of the "
            "fire and EMS talkgroups to hear. Every call on them is paid for."
        )
    ems_talkgroups = _talkgroups(env, OPENMHZ_EMS_TALKGROUPS_VARIABLE)
    if not set(ems_talkgroups) <= set(talkgroups):
        raise IngestConfigError(
            f"{OPENMHZ_EMS_TALKGROUPS_VARIABLE} names a talkgroup that "
            f"{OPENMHZ_TALKGROUPS_VARIABLE} does not."
        )
    hosts = [host.strip().lower() for host in env.get(OPENMHZ_MEDIA_HOSTS_VARIABLE, "").split(",")]
    hosts = [host for host in hosts if host]
    if not all(_HOST.fullmatch(host) for host in hosts):
        raise IngestConfigError(
            f"{OPENMHZ_MEDIA_HOSTS_VARIABLE} is a list of host names, such as media.example.org."
        )
    scope = _radio_scope(env)
    if scope is None:
        raise IngestConfigError(
            f"{names}, which needs {OPENMHZ_BUDGET_VARIABLE}: the most, in dollars, that "
            "hearing radio may cost over every run. It has no default, because every "
            "call is paid for."
        )
    if shutil.which("ffmpeg") is None or not hasattr(os, "memfd_create"):
        raise IngestConfigError(
            f"{names}, whose audio is converted by ffmpeg, in memory, which needs ffmpeg "
            "installed and Linux."
        )
    ember = EmberClient.from_env(env, scope=scope)
    if not ember.spends:
        raise IngestConfigError(
            f"{names}, and a radio call is heard by a live Ember and cannot be replayed: "
            "set REPLAY=0, MODEL_API_KEY, and EMBER_SPENT_BEFORE_USD."
        )
    return OpenMhzRadio(
        http,
        ember,
        talkgroups=talkgroups,
        ems_talkgroups=ems_talkgroups,
        media_hosts=hosts,
        transcoder=Ffmpeg(),
    )


def _radio_scope(env: Mapping[str, str]) -> SpendScope | None:
    """The spending scope live radio calls are made under, with the budget the
    environment sets for it, or None when that is not a dollar amount a scope takes."""
    try:
        return SpendScope(RADIO_SCOPE, parse_usd(env.get(OPENMHZ_BUDGET_VARIABLE, "")))
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


def _scenario(store: EvidenceStore, now: datetime) -> Scenario:
    """The story of this mock store. Its start is drawn once and kept in the store,
    so a restart continues the story it was telling and does not begin a second one
    beside it. Deleting the mock store's file begins it again."""
    saved = store.state(_SCENARIO_STATE).get("start")
    if isinstance(saved, str):
        return Scenario(start=datetime.fromisoformat(saved))
    start = now.replace(microsecond=0) - MOCK_HEAD_START
    store.add([], state=(_SCENARIO_STATE, {"start": start.isoformat()}), now=now)
    return Scenario(start=start)
