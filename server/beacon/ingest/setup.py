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

The mock scenario is played on a clock of its own. Its story begins twenty minutes
before a mock store is first opened and runs in real time from there, in steps of ten
seconds, and how far it has been played is kept in the store, so a restart goes on
from the step after the last one that was finished.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx2

from beacon.ember.client import default_data_dir
from beacon.evidence import EvidenceStore, Mode, Origin
from beacon.ingest.http import http_client
from beacon.ingest.nws import NwsAlerts
from beacon.ingest.opensky import SIGNED_IN_INTERVAL_SECONDS, OpenSkyAircraft
from beacon.ingest.runner import Clock, Poller, Runner
from beacon.ingest.traffic511 import Traffic511
from beacon.scenario import DURATION, Scenario, scenario_transport

# The sources that can be named in BEACON_LIVE_SOURCES.
SOURCES = ("nws", "opensky", "511")

LIVE_SOURCES_VARIABLE = "BEACON_LIVE_SOURCES"
KEY_511_VARIABLE = "BEACON_511_API_KEY"
OPENSKY_PERMITTED_VARIABLE = "BEACON_OPENSKY_PERMITTED"
OPENSKY_ID_VARIABLE = "OPENSKY_CLIENT_ID"
OPENSKY_SECRET_VARIABLE = "OPENSKY_CLIENT_SECRET"

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


class IngestConfigError(Exception):
    """The environment asks for pollers that cannot be built."""


@dataclass(frozen=True)
class Ingest:
    store: EvidenceStore
    runner: Runner
    clock: Clock
    http: httpx2.Client
    # The story the pollers are fed, in mock mode.
    scenario: Scenario | None

    def catch_up(self, stop: threading.Event | None = None) -> None:
        """In mock mode, play the story from the step after the last one that was
        finished up to now. It does nothing in live mode: a real source cannot be
        asked what it said earlier.

        How far the story has been played is saved after each step, apart from
        anything else the store holds. A step that was cut short is played again,
        and playing a step twice stores nothing twice."""
        if self.scenario is None:
            return
        played = self.store.state(_SCENARIO_STATE).get("played_to")
        moment = (
            datetime.fromisoformat(played) + SCENARIO_STEP
            if isinstance(played, str)
            else self.scenario.start
        )
        end = min(self.clock.now(), self.scenario.start + DURATION + _AFTER_THE_STORY)
        try:
            while moment <= end and not (stop is not None and stop.is_set()):
                self.clock.hold(moment)
                self.runner.step(stop)
                if stop is not None and stop.is_set():
                    # The step may not have polled every source, so it is not done.
                    break
                state = {"start": self.scenario.start.isoformat(), "played_to": moment.isoformat()}
                self.store.add([], state=(_SCENARIO_STATE, state), now=moment)
                moment += SCENARIO_STEP
        finally:
            self.clock.hold(None)

    def run(self, stop: threading.Event) -> None:
        """Poll until `stop` is set. In mock mode that is playing the story as the
        time for each of its steps comes."""
        if self.scenario is None:
            self.runner.run(stop)
            return
        while not stop.is_set():
            self.catch_up(stop)
            stop.wait(SCENARIO_STEP.total_seconds())

    def close(self) -> None:
        """Close the HTTP client, which ends any request a poller has out."""
        self.http.close()


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
        http = http_client()
        pollers = source_pollers(live, env, http, origin="live")
        store = open_store(env)
        return Ingest(store, Runner(pollers, store, clock), clock, http, None)
    store = open_store(env)
    scenario = _scenario(store, clock.now())
    http = http_client(scenario_transport(scenario, clock.now))
    pollers = [
        NwsAlerts(http, origin="mock"),
        OpenSkyAircraft(http, origin="mock", interval_seconds=SIGNED_IN_INTERVAL_SECONDS),
        Traffic511(http, api_key="mock", origin="mock"),
    ]
    return Ingest(store, Runner(pollers, store, clock), clock, http, scenario)


def source_pollers(
    sources: tuple[str, ...], env: Mapping[str, str], http: httpx2.Client, *, origin: Origin
) -> list[Poller]:
    """The pollers for these sources, with the settings each needs read from `env`.
    `build` calls it with the real network and the origin `live`. A test hands it a
    stand-in for the network, and then the origin is `mock`, because what a stand-in
    answers is not a real source's however the request was made.

    Raises IngestConfigError when a source lacks a setting it needs."""
    pollers: list[Poller] = []
    if "nws" in sources:
        pollers.append(NwsAlerts(http, origin=origin))
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
        pollers.append(
            OpenSkyAircraft(http, origin=origin, client_id=client_id, client_secret=client_secret)
        )
    if "511" in sources:
        api_key = env.get(KEY_511_VARIABLE)
        if not api_key:
            raise IngestConfigError(
                f"{LIVE_SOURCES_VARIABLE} names 511, which needs {KEY_511_VARIABLE}."
            )
        pollers.append(Traffic511(http, api_key=api_key, origin=origin))
    return pollers


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
