"""Builds the pollers from the environment: real sources, or the mock scenario.

`BEACON_LIVE_SOURCES` decides. Unset or empty, which is the default, every poller
runs against the mock scenario, nothing touches the network, and rows go to the mock
evidence store with the origin `mock`. Set to a list of sources, such as `nws,opensky`,
those sources are polled for real and their rows go to the live store. A source that
is not named does not run in that mode: the live store holds nothing a stand-in
produced, so a mock poller has nowhere to write.

A source needs no code change to go live, only its name in that list and its key:

    nws       no key
    opensky   no key, or OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET for a larger quota
    511       BEACON_511_API_KEY
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx2

from beacon.evidence import LIVE_SOURCES_VARIABLE, EvidenceStore
from beacon.ingest.http import http_client
from beacon.ingest.nws import NwsAlerts
from beacon.ingest.opensky import SIGNED_IN_INTERVAL_SECONDS, OpenSkyAircraft
from beacon.ingest.runner import CATCH_UP_STEP, Clock, Poller, Runner
from beacon.ingest.traffic511 import Traffic511
from beacon.scenario import DURATION, Scenario, scenario_transport

# The sources that can be named in BEACON_LIVE_SOURCES.
SOURCES = ("nws", "opensky", "511")

KEY_511_VARIABLE = "BEACON_511_API_KEY"
OPENSKY_ID_VARIABLE = "OPENSKY_CLIENT_ID"
OPENSKY_SECRET_VARIABLE = "OPENSKY_CLIENT_SECRET"

# A mock store that is begun starts its story this long ago, so the first thing a
# developer sees is a fire in progress and not an empty city.
MOCK_HEAD_START = timedelta(minutes=20)

_SCENARIO_STATE = "scenario"
# Long enough after the story for the pollers to notice what stopped being listed.
_AFTER_THE_STORY = timedelta(minutes=10)


class IngestConfigError(Exception):
    """The environment asks for pollers that cannot be built."""


@dataclass(frozen=True)
class Ingest:
    store: EvidenceStore
    runner: Runner
    clock: Clock
    # The story the pollers are fed, in mock mode.
    scenario: Scenario | None

    def catch_up(self, stop: threading.Event | None = None) -> None:
        """In mock mode, play the part of the story that has passed and has not been
        polled yet. It does nothing in live mode: a real source cannot be asked what
        it said earlier."""
        if self.scenario is None:
            return
        polled = self.store.last_recorded_at()
        start = self.scenario.start if polled is None else polled + CATCH_UP_STEP
        # Nothing changes after the story ends, so a store left for days is not
        # stepped through every moment of them.
        end = min(self.clock.now(), self.scenario.start + DURATION + _AFTER_THE_STORY)
        self.runner.catch_up(start, end, stop)

    def run(self, stop: threading.Event) -> None:
        """Catch up, then poll until `stop` is set."""
        self.catch_up(stop)
        self.runner.run(stop)


def live_sources(env: Mapping[str, str]) -> tuple[str, ...]:
    """The sources `BEACON_LIVE_SOURCES` names, in the order of `SOURCES`.

    Raises IngestConfigError for a name that is not a source."""
    named = {name.strip() for name in env.get(LIVE_SOURCES_VARIABLE, "").split(",")} - {""}
    unknown = sorted(named - set(SOURCES))
    if unknown:
        raise IngestConfigError(
            f"{LIVE_SOURCES_VARIABLE} names {', '.join(unknown)}, which is not a source. "
            f"The sources are {', '.join(SOURCES)}."
        )
    return tuple(source for source in SOURCES if source in named)


def build(
    env: Mapping[str, str],
    clock: Clock | None = None,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> Ingest:
    """The store, the pollers, and the clock they share. `transport` replaces the
    network for the live sources, which is how tests answer for them.

    Raises IngestConfigError when a live source lacks a setting it needs, and
    EvidenceStoreError or EmberConfigError when the store cannot be opened."""
    clock = Clock() if clock is None else clock
    live = live_sources(env)
    store = EvidenceStore.from_env(env)
    if live:
        pollers = _live_pollers(live, env, http_client(transport))
        return Ingest(store, Runner(pollers, store, clock), clock, None)
    scenario = _scenario(store, clock.now())
    http = http_client(scenario_transport(scenario, clock.now))
    pollers: list[Poller] = [
        NwsAlerts(http, origin="mock"),
        OpenSkyAircraft(http, origin="mock", interval_seconds=SIGNED_IN_INTERVAL_SECONDS),
        Traffic511(http, api_key="mock", origin="mock"),
    ]
    return Ingest(store, Runner(pollers, store, clock), clock, scenario)


def _live_pollers(
    live: tuple[str, ...], env: Mapping[str, str], http: httpx2.Client
) -> list[Poller]:
    pollers: list[Poller] = []
    if "nws" in live:
        pollers.append(NwsAlerts(http, origin="live"))
    if "opensky" in live:
        client_id = env.get(OPENSKY_ID_VARIABLE) or None
        client_secret = env.get(OPENSKY_SECRET_VARIABLE) or None
        if (client_id is None) != (client_secret is None):
            raise IngestConfigError(
                f"{OPENSKY_ID_VARIABLE} and {OPENSKY_SECRET_VARIABLE} are set together, "
                "or neither is."
            )
        pollers.append(
            OpenSkyAircraft(http, origin="live", client_id=client_id, client_secret=client_secret)
        )
    if "511" in live:
        api_key = env.get(KEY_511_VARIABLE)
        if not api_key:
            raise IngestConfigError(
                f"{LIVE_SOURCES_VARIABLE} names 511, which needs {KEY_511_VARIABLE}."
            )
        pollers.append(Traffic511(http, api_key=api_key, origin="live"))
    return pollers


def _scenario(store: EvidenceStore, now: datetime) -> Scenario:
    """The story of this mock store. Its start is drawn once and kept in the store,
    so a restart continues the story it was telling and does not begin a second one
    beside it. Deleting the mock store's file begins it again."""
    saved = store.state(_SCENARIO_STATE).get("start")
    if isinstance(saved, str):
        return Scenario(start=datetime.fromisoformat(saved))
    start = now.replace(microsecond=0) - MOCK_HEAD_START
    # Saved as of one step before the start, so the first catch-up, which begins a
    # step after the store's latest moment, begins at the story's first moment.
    store.add([], state=(_SCENARIO_STATE, {"start": start.isoformat()}), now=start - CATCH_UP_STEP)
    return Scenario(start=start)
